import { useMemo, useState, type FormEvent } from 'react';
import { MessageList } from './components/MessageList';
import { PdfPreview } from './components/PdfPreview';
import { StatusBar } from './components/StatusBar';
import { useChat } from './useChat';

const DEFAULT_QUESTION = '2025年差旅住宿标准是多少？';

export function App() {
  const { state, send, stop, reset, selectCitation, scenarios } = useChat();
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [scenarioId, setScenarioId] = useState(scenarios[0]?.id ?? 'normal');
  const [scope, setScope] = useState('');

  const isBusy = state.status === 'retrieving' || state.status === 'streaming';

  const scenarioOptions = useMemo(
    () => scenarios.map((s) => ({ id: s.id, name: s.name, description: s.description })),
    [scenarios],
  );

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (isBusy) return;
    send(question, scenarioId);
  };

  const onClarifyOption = (value: string, label: string) => {
    // F1: clarify button placeholder behavior - refill input and send as follow-up.
    const next = `${question}（已选择：${label}/${value}）`;
    setQuestion(next);
    if (!isBusy) {
      send(next, scenarioId === 'clarify' ? 'normal' : scenarioId);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>AI-RAG 问答工作台</h1>
          <p className="subtitle">F1 骨架：双栏布局 + Mock SSE（step/clarify/token/citations/fallback/done）</p>
        </div>
        <div className="topbar-actions">
          <button type="button" className="ghost" onClick={reset} disabled={isBusy}>
            清空会话
          </button>
        </div>
      </header>

      <StatusBar
        status={state.status}
        lastStepPhase={state.lastStep?.phase}
        lastStepHop={state.lastStep?.hop}
        latencyMs={state.lastDone?.latency_ms}
        hopCount={state.lastDone?.hop_count}
        verdict={state.lastDone?.verdict}
        error={state.error}
        selectedCitation={state.selectedCitation}
      />

      <main className="workspace">
        <section className="pane pane-chat" aria-label="问答区">
          <div className="pane-header">
            <h2>问答区</h2>
            <div className="scenario-picker">
              <label htmlFor="scenario">Mock 场景</label>
              <select
                id="scenario"
                value={scenarioId}
                onChange={(e) => setScenarioId(e.target.value)}
                disabled={isBusy}
              >
                {scenarioOptions.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="scenario-desc">
            {scenarioOptions.find((s) => s.id === scenarioId)?.description}
          </div>

          <div className="scope-row">
            <label htmlFor="scope">范围筛选（占位）</label>
            <input
              id="scope"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
              placeholder="doc_key / 时间范围（F1 占位，不发请求）"
              disabled={isBusy}
            />
          </div>

          <MessageList
            messages={state.messages}
            onSelectCitation={(c) => selectCitation(c)}
            onClarifyOption={onClarifyOption}
          />

          <form className="composer" onSubmit={onSubmit}>
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="输入问题…"
              rows={3}
              aria-label="问题输入"
            />
            <div className="composer-actions">
              <div className="composer-hint">
                {isBusy ? '流式进行中，可中断。' : 'Enter 提交请用按钮（避免误发）。'}
              </div>
              <div className="composer-buttons">
                {isBusy ? (
                  <button type="button" className="danger" onClick={stop}>
                    中断
                  </button>
                ) : (
                  <button type="submit" className="primary" disabled={!question.trim()}>
                    发送
                  </button>
                )}
              </div>
            </div>
          </form>
        </section>

        <section className="pane pane-preview" aria-label="预览区">
          <div className="pane-header">
            <h2>文档预览区</h2>
            <div className="preview-note">PDF 加载 / 页码跳转 / bbox 高亮占位（F2 接真实）</div>
          </div>
          <PdfPreview selectedCitation={state.selectedCitation} />
        </section>
      </main>
    </div>
  );
}
