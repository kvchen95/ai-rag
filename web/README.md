# AI-RAG Web (F1)

F1 交付：React + TypeScript + Vite 双栏问答工作台骨架，使用 Mock SSE 驱动 UI。

## 能力范围（F1）

- 左侧问答区：问题输入、范围筛选占位、消息流、澄清选项按钮
- 右侧预览区：PDF 加载占位、页码跳转占位、引用 bbox 高亮占位
- Mock SSE：可回放 `step / clarify / token / citations / fallback / done`
- 乱序容错：事件顺序错乱不崩溃；未知/畸形事件被忽略；`done` 后迟到帧被 runId 守卫丢弃

## 目录

- `src/types.ts` — SSE 事件与 UI 状态契约（对齐 `详细设计.md` 附录 C.2）
- `src/state.ts` — 纯 reducer 状态机（runId 守卫、乱序容错）
- `src/mockSse.ts` — Mock 场景与回放器
- `src/useChat.ts` — React hook（发送/中断/选择引用）
- `src/components/` — UI 组件
- `src/*.test.ts` — node 环境纯逻辑测试（不依赖浏览器）

## 本地启动

```bash
cd web
pnpm install
pnpm dev
```

默认 http://localhost:5173

## 验证

```bash
cd web
pnpm typecheck
pnpm build
pnpm test
```

## 说明

- F1 不接真实后端 SSE；F2 将 `mockSse.ts` 替换为真实 `/api/v1/qa/stream`。
- PDF 预览为 mock 占位页（用于验证页码跳转与 bbox overlay），F2 接入 PDF.js 与真实样本文件。
- 事件契约与前端状态遵循 `详细设计.md` §10.4、附录 C.2：`done` 后才允许角标作为最终态；`fallback` 不给结论；中断显示“已中断，可重试”，不保留半截答案。
