"""doc_key 归一化 / 指纹 / version_id（纯函数，不需要数据库）。.

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
    (
        "policy/差旅管理办法-2025版.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "段尾年份+版（中文转拼音）",
    ),
    (
        "policy/差旅管理办法-2025版-第2稿.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "叠加修饰剥到稳定",
    ),
    (
        "policy/差旅管理办法（2）.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "全角括号序号",
    ),
    (
        "policy/差旅管理办法(2).pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "半角括号序号",
    ),
    (
        "policy/差旅管理办法_v2.1.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "v+次版本号",
    ),
    (
        "policy/差旅管理办法-修订.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "修订后缀",
    ),
    (
        "policy/差旅管理办法-定稿.pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "定稿后缀",
    ),
    (
        "policy/差旅管理办法(副本).pdf",
        "policy/cha-lv-guan-li-ban-fa",
        "括号副本",
    ),
    ("hr/leave-2025.pdf", "hr/leave", "年份"),
    ("x/差旅-20250301.pdf", "x/cha-lv", "紧凑日期"),
    ("x/差旅-2025-03-01.pdf", "x/cha-lv", "带横线日期"),
    ("x/差旅-2025年3月.pdf", "x/cha-lv", "中文年月"),
    ("Travel-CN.PDF", "travel-cn", "大小写归一（标识符不是展示名）"),
    ("a/b/c/deep name.pdf", "a/b/c/deep-name", "空白收连字符、多级目录"),
    # --- 反例：这些看起来像修饰，其实是主体的一部分，剥掉就合并了两份不同材料
    # ---
    (
        "x/2024-差旅.pdf",
        "x/2024-cha-lv",
        "日期在中间是主体（2024 年度差旅办法）",
    ),
    (
        "x/2025.pdf",
        "x/2025",
        "整名都是数字时回退原名，不产出空 key（父目录保留）",
    ),
    ("x/第3版.pdf", "x/di-3-ban", "整名都是修饰时同样回退原名"),
]


@pytest.mark.parametrize(("rel_path", "want", "why"), DOC_KEY_CASES)
def test_doc_key_normalisation(rel_path: str, want: str, why: str) -> None:
    """Run test_doc_key_normalisation."""
    assert doc_key_for(rel_path) == want, why


def test_doc_key_is_stable_under_repeated_calls() -> None:
    # 同一个路径两次调用必须一致：key 抖一下，判重就失效。
    """Run test_doc_key_is_stable_under_repeated_calls."""
    path = "policy/差旅管理办法-2025版-第2稿.pdf"
    assert doc_key_for(path) == doc_key_for(path)


def test_doc_key_rejects_paths_escaping_the_root() -> None:
    """Run test_doc_key_rejects_paths_escaping_the_root."""
    for bad in ("../secret.pdf", "a/../../b.pdf", ""):
        with pytest.raises(BadSamplePath):
            doc_key_for(bad)


def test_different_materials_never_share_a_key() -> None:
    """两份不同材料不能撞同一个 key。.

    撞了会被判成“同一份文档的改版”，于是后进来的那份把前一份顶掉——这是
    最不能静默发生的一类错。
    """
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
    """Run test_file_hash_is_lowercase_sha256_of_the_bytes."""
    path = tmp_path / "a.pdf"
    payload = b"%PDF-1.4\nhello\n"
    path.write_bytes(payload)
    got = file_hash(path)
    assert got == hashlib.sha256(payload).hexdigest()
    # 库里有 CHECK 强制这个形状（ck_doc_versions_hash_sha256），短码会被拒。
    assert is_sha256(got) and len(got) == 64
    assert got == got.lower()


def test_file_hash_changes_with_one_byte(tmp_path) -> None:
    """Run test_file_hash_changes_with_one_byte."""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"%PDF-1.4\nAAAA\n")
    b.write_bytes(b"%PDF-1.4\nAAAB\n")
    assert file_hash(a) != file_hash(b)


def test_file_hash_is_chunked_but_equivalent(tmp_path) -> None:
    """分块读不能改变结果。.

    样本里有几十 MB 的扫描件，一次性 read 会把内存翻倍。
    """
    path = tmp_path / "big.bin"
    payload = bytes(range(256)) * 5000  # ~1.2 MB，跨过 1 MiB 的分块边界
    path.write_bytes(payload)
    assert file_hash(path, chunk=1024) == hashlib.sha256(payload).hexdigest()


def test_version_id_shape_and_collision_room() -> None:
    """Run test_version_id_shape_and_collision_room."""
    digest = "a" * 64
    version_id = version_id_for("policy/travel-cn", digest)
    assert version_id == f"policy/travel-cn#{'a' * 12}"
    # 12 位短码：4 位只有 65536 个取值，一个 doc_key 攒几十个版本就会撞主键。
    assert len(version_id.split("#", 1)[1]) == 12


def test_version_id_rejects_non_sha256() -> None:
    """Run test_version_id_rejects_non_sha256."""
    with pytest.raises(ValueError):
        version_id_for("policy/x", "abc123")


def test_list_samples_only_picks_pdfs_and_is_ordered(tmp_path) -> None:
    """Run test_list_samples_only_picks_pdfs_and_is_ordered."""
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "two.pdf").write_bytes(b"%PDF-1.4\n%2\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n%1\n")
    (tmp_path / "notes.txt").write_text("not a sample")
    (tmp_path / "c.PDF").write_bytes(b"%PDF-1.4\n%3\n")

    samples = list_samples(tmp_path)
    # 排序稳定：同一批样本每次扫描顺序一致，报告 diff 才有意义。
    assert [s.rel_path for s in samples] == ["a.pdf", "b/two.pdf", "c.PDF"]
    assert [s.doc_key for s in samples] == ["a", "b/two", "c"]


def test_doc_key_transliterates_cjk_to_pinyin() -> None:
    """中文文件名要变成 ASCII 标识符，否则 URL 里全是百分号编码。."""
    key = doc_key_for("policy/差旅管理办法.pdf")
    assert key == "policy/cha-lv-guan-li-ban-fa"
    assert key.isascii(), "doc_key 必须是 ASCII，才能直接进 URL 路径参数"


def test_doc_key_extracts_org_prefix() -> None:
    """`【上海证券】…` 的机构名要单独成段，一眼能看出是哪家的报告。."""
    key = doc_key_for("reports/【上海证券】中芯国际深度研究报告.pdf")
    assert key.startswith("reports/shang-hai-zheng-quan-")
    assert key.isascii()


def test_doc_key_strips_punctuation_before_transliteration() -> None:
    """标点不是音节：`…提升，持续…` 不能产出 `…ti-sheng-，` 这种尾巴。."""
    key = doc_key_for(
        "reports/【东方证券】产能利用率提升，持续推进工艺迭代.pdf"
    )
    assert "，" not in key and "：" not in key
    assert key.isascii()
    assert not key.endswith("-")


def test_doc_key_pinyin_is_bounded() -> None:
    """长标题不能产出无限长的 key——`make doc DOC_KEY=...` 要能敲进去。."""
    key = doc_key_for(
        "reports/中芯国际集成电路制造有限公司2024年年度报告全文.pdf"
    )
    leaf = key.split("/")[-1]
    assert len(leaf.split("-")) <= 12, f"音节日数过多: {leaf}"


def test_iter_sample_paths_skips_underscore_dirs(tmp_path) -> None:
    """`_` 开头的目录整棵跳过：把文件挪进去就是"移出扫描"的全部动作。."""
    from api.ingest.fingerprint import iter_sample_paths

    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "_excluded").mkdir()
    (tmp_path / "_excluded" / "broken.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "_excluded" / "nested").mkdir()
    (tmp_path / "_excluded" / "nested" / "deep.pdf").write_bytes(b"%PDF-1.4")

    got = [
        p.relative_to(tmp_path).as_posix() for p in iter_sample_paths(tmp_path)
    ]
    assert got == ["ok/a.pdf"]


def test_iter_sample_paths_keeps_underscore_files(tmp_path) -> None:
    """只排除目录，不排除文件：`_draft.pdf` 是正常样本，只是名字带下划线。."""
    from api.ingest.fingerprint import iter_sample_paths

    (tmp_path / "_draft.pdf").write_bytes(b"%PDF-1.4")
    got = [p.name for p in iter_sample_paths(tmp_path)]
    assert got == ["_draft.pdf"]
