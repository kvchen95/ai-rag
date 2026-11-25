# AI-RAG Web (F2)

F2 交付：在 F1 骨架上接入真实 SSE（B4 `POST /api/v1/qa/stream`），完成引用角标跳页与 bbox 高亮溯源，并覆盖图表切片/错误态降级。

## 能力范围

- **真实 SSE 接入**：`fetch` + `ReadableStream` + `SseParser`（POST 可带 `Authorization`）；处理 `step/clarify/token/citations/fallback/done` 六类事件。
- **clarify 回填**：点选 clarify 选项后，将 `doc_key/value` 回填为 `scope.doc_keys` 重发原问题，并展示范围说明。
- **引用角标溯源**：正文 `[n]` 可点；点击后右侧 PDF 跳到对应页；归一化 bbox（0..1）画高亮框。
- **bbox 降级**：bbox 缺失/无效时不画错框，降级显示“页码 + 原文片段”；越界像素坐标丢弃。
- **图表切片**：展示切片 WebP 与说明；404 显示“无切片图”；401/403 显示无权限；网络/服务错误显示固定文案。
- **错误态**：401/403/404/503/5xx 只按状态码映射固定文案，**不展示服务端响应正文/堆栈**；request_id 仅作排障展示。
- **乱序/畸形容错**：未知事件、畸形 JSON、迟到帧（done 后/非活跃 runId）计数忽略，不崩溃；中断不保留半截答案。

## 目录

- `src/api.ts` — 真实 QA SSE、样本 PDF/切片图 URL、HTTP 错误映射（固定文案）
- `src/sse.ts` — 增量 SSE 帧解析（纯函数，可单测）
- `src/coalesce.ts` — token rAF 合帧（非 token 事件前 flush）
- `src/state.ts` — 纯 reducer 状态机（runId 守卫、终态守卫、降级口径）
- `src/useChat.ts` — 会话 hook（live/mock 双源、abort、中断）
- `src/citations.ts` — `[n]` 解析与角标可点回率审计
- `src/bbox.ts` — 归一化 bbox → 百分比矩形，非法坐标丢弃
- `src/pdf.ts` — PDF.js 加载/渲染、错误归一
- `src/components/` — `MessageList`、`PdfPreview`、`FigurePanel`、`StatusBar`
- `src/*.test.ts` — node 环境纯逻辑测试

## 本地启动

```bash
cd web
pnpm install
pnpm dev
```

默认 http://localhost:5174（Vite 代理 `/api` 到 `VITE_API_TARGET`，默认 `http://127.0.0.1:8000`）。

真实模式已预置一枚开发用 JWT（u_dev / fin,corp / staff，按当前 .env 的密钥签发），打开页面即可发送；
也可用页面输入框、`?token=`、sessionStorage 或构建时 `VITE_DEV_TOKEN` 覆盖。密钥改过时要重新签发内置凭证。无后端时可切换“演示回放”查看事件顺序。

## 验证

```bash
cd web
pnpm typecheck
pnpm test
pnpm build
```

## 契约对齐

- `详细设计.md` §10.4：事件顺序、done 后允许点角标、fallback 不给结论、中断不保留半截答案。
- `详细设计.md` 附录 C.1/C.2：`POST /api/v1/qa/stream` 请求体/六类 SSE 事件、citations bbox 归一化坐标。
- `详细设计.md` §11.3：401/403/404/503 等错误仅显示固定用户文案，不暴露内部细节。
