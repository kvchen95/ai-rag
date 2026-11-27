# 架构不变量与失败契约

这里放项目级硬约束，不是设计建议。

## 文件

- `ENGINEERING_CONSTITUTION.md`：原则与允许的失败出口
- `invariants.yaml`：不变量编号、范围、检测器、负责人
- `scripts/check_invariants.py`：AST 检查器（失败契约/领域不变量）
- `scripts/check_source_language.py`：跨仓库中文源码检查器
- `ruff.toml`：Google Python Style 规则（80 列、pydocstyle google、naming/import）
- `api/common/result.py`：`Ok/Fail/Degraded` 与 `degrade()` 网关

## 命令

```bash
make invariants       # 全仓全量检查
make invariants-all   # 同上，兼容旧命令
make lint-google      # Google Style 全仓检查
make lint-google-all  # 同上，兼容旧命令
```

GitHub Actions 在 PR 上执行 `--changed --base origin/<base>` 做领域不变量和中文源码检查；Google Style 则对全仓执行，不用 diff 过滤。

## 规则

1. 失败不能返回 `[]`、`{}`、`""`、`None`、`0`；
2. 显式降级必须走 `degrade()`，带 `code`、`reason`、`audit`；
3. 确定性模块不得 import 生成模型；
4. 缺失 metadata 必须 fail-closed；
5. 关键词特判、LLM 兜底、放宽检索范围均视为违规；
6. 生产代码不得出现中文字符串；prompt/词表/单位/文案只能放 `api/prompts/` 或 `api/resources/`。

## 例外

只允许 SSE/进程边界统一捕获异常，但必须转成显式错误事件。例外写在
`ruff.toml` 的 `per-file-ignores`，并在 PR 中说明理由。
