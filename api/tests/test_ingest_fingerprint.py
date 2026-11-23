"""doc_key 归一化 / 指纹 / version_id（纯函数，不需要数据库）。

这一组判据的作用是**锁住命名规则**：`doc_key` 一旦变了，库里已有的文档就全部
断开版本线，而这件事在运行时不报错、不告警，只会表现为"怎么又新建了一份文档"。
所以每条规则都配了反例（不该被剥的修饰不许剥），只测正向会让"多剥一刀"的
改动照样通过。
"""

from __future__ import annotations

import hashlib

import pytest

from api.ingest.fingerprint import (
    BadSamplePath,
    doc_key_for,
    file_hash,
    is_sha256,
    list_samples,
    version_id_for,
)

# (相对路径, 期望 doc_key, 理由)
DOC_KEY_CASES = [
    ("policy/travel-cn.pdf", "policy/travel-cn", "子目录保留"),
    ("policy/差旅管理办法-2025版.pdf", "policy/差旅管理办法", "段尾年份+版"),
    ("policy/差旅管理办法-2025版-第2稿.pdf", "policy/差旅管理办法", "叠加修饰剥到稳定"),
    ("policy/差旅管理办法（2）.pdf", "policy/差旅管理办法", "全角括号序号"),
    ("policy/差旅管理办法(2).pdf", "policy/差旅管理办法", "半角括号序号"),
    ("policy/差旅管理办法_v2.1.pdf", "policy/差旅管理办法", "v+次版本号"),
    ("policy/差旅管理办法-修订.pdf", "policy/差旅管理办法", "修订后缀"),
    ("policy/差旅管理办法-定稿.pdf", "policy/差旅管理办法", "定稿后缀"),
    ("policy/差旅管理办法(副本).pdf", "policy/差旅管理办法", "括号副本"),
    ("hr/leave-2025.pdf", "hr/leave", "年份"),
    ("x/差旅-20250301.pdf", "x/差旅", "紧凑日期"),
    ("x/差旅-2025-03-01.pdf", "x/差旅", "带横线日期"),
    ("x/差旅-2025年3月.pdf", "x/差旅", "中文年月"),
    ("Travel-CN.PDF", "travel-cn", "大小写归一（标识符不是展示名）"),
    ("a/b/c/deep name.pdf", "a/b/c/deep-name", "空白收连字符、多级目录"),
    # --- 反例：这些看起来像修饰，其实是主体的一部分，剥掉就合并了两份不同材料 ---
    ("x/2024-差旅.pdf", "x/2024-差旅", "日期在中间是主体（2024 年度差旅办法）"),
    ("x/2025.pdf", "x/2025", "整名都是数字时回退原名，不产出空 key（父目录保留）"),
    ("x/第3版.pdf", "x/第3版", "整名都是修饰时同样回退原名"),
]


@pytest.mark.parametrize(("rel_path", "want", "why"), DOC_KEY_CASES)
def test_doc_key_normalisation(rel_path: str, want: str, why: str) -> None:
    assert doc_key_for(rel_path) == want, why


def test_doc_key_is_stable_under_repeated_calls() -> None:
    # 同一个路径两次调用必须一致：key 抖一下，判重就失效。
    path = "policy/差旅管理办法-2025版-第2稿.pdf"
    assert doc_key_for(path) == doc_key_for(path)


def test_doc_key_rejects_paths_escaping_the_root() -> None:
    for bad in ("../secret.pdf", "a/../../b.pdf", ""):
        with pytest.raises(BadSamplePath):
            doc_key_for(bad)


def test_different_materials_never_share_a_key() -> None:
    """两份不同材料撞同一个 key，会被判成"同一份文档的改版"，
    于是后进来的那份把前一份顶掉——这是最不能静默发生的一类错。"""
    keys = {
        doc_key_for(p)
        for p in (
            "x/差旅管理办法.pdf",
            "x/费用报销办法.pdf",
            "y/差旅管理办法.pdf",
        )
    }
    assert len(keys) == 3


def test_file_hash_is_lowercase_sha256_of_the_bytes(tmp_path) -> None:
    path = tmp_path / "a.pdf"
    payload = b"%PDF-1.4\nhello\n"
    path.write_bytes(payload)
    got = file_hash(path)
    assert got == hashlib.sha256(payload).hexdigest()
    # 库里有 CHECK 强制这个形状（ck_doc_versions_hash_sha256），短码会被拒。
    assert is_sha256(got) and len(got) == 64
    assert got == got.lower()


def test_file_hash_changes_with_one_byte(tmp_path) -> None:
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-1.4\nAAAA\n")
    b.write_bytes(b"%PDF-1.4\nAAAB\n")
    assert file_hash(a) != file_hash(b)


def test_file_hash_is_chunked_but_equivalent(tmp_path) -> None:
    """分块读不能改变结果——样本里有几十 MB 的扫描件，一次性 read 会把内存翻倍。"""
    path = tmp_path / "big.bin"
    payload = bytes(range(256)) * 5000  # ~1.2 MB，跨过 1 MiB 的分块边界
    path.write_bytes(payload)
    assert file_hash(path, chunk=1024) == hashlib.sha256(payload).hexdigest()


def test_version_id_shape_and_collision_room() -> None:
    digest = "a" * 64
    version_id = version_id_for("policy/travel-cn", digest)
    assert version_id == f"policy/travel-cn#{'a' * 12}"
    # 12 位短码：4 位只有 65536 个取值，一个 doc_key 攒几十个版本就会撞主键。
    assert len(version_id.split("#", 1)[1]) == 12


def test_version_id_rejects_non_sha256() -> None:
    with pytest.raises(ValueError):
        version_id_for("policy/x", "abc123")


def test_list_samples_only_picks_pdfs_and_is_ordered(tmp_path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "two.pdf").write_bytes(b"%PDF-1.4\n%2\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n%1\n")
    (tmp_path / "notes.txt").write_text("not a sample")
    (tmp_path / "c.PDF").write_bytes(b"%PDF-1.4\n%3\n")

    samples = list_samples(tmp_path)
    # 排序稳定：同一批样本每次扫描顺序一致，报告 diff 才有意义。
    assert [s.rel_path for s in samples] == ["a.pdf", "b/two.pdf", "c.PDF"]
    assert [s.doc_key for s in samples] == ["a", "b/two", "c"]
