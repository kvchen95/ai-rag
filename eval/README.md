# 黄金评测集 v1（30 题）

本目录当前只使用 `golden_questions_v1` 这套评测集，面向 `samples/pdf_reports` 下的 6 份报告。

## 文件

- `golden_questions_v1.md`：人读版，30 题。
- `golden_questions_v1.jsonl`：机器评测版，每行一个 JSON 对象。

## 覆盖

| 类型 | 题量 |
|---|---:|
| 单文档事实/理解 | 20 |
| 跨文档比较/归纳 | 6 |
| 应拒答 | 4 |

## 运行

```bash
make eval
```

默认读取 `eval/golden_questions_v1.jsonl`。`key_evidence` 页码和证据锚点后续逐题补齐，当前先用于检索、回答、引用和拒答链路验收。
