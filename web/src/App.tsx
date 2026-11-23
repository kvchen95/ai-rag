import { useCallback, useMemo, useState, type ChangeEvent, type FormEvent } from 'react';
import { MessageList } from './components/MessageList';
import { PdfPreview } from './components/PdfPreview';
import { StatusBar } from './components/StatusBar';
import { useChat } from './useChat';
import { auditCitations } from './citations';
import type { CitationItem, ClarifyOption, DataSource, FallbackDoc } from './types';

const DEFAULT_QUESTION = '2025年差旅住宿标准是多少？';
const TOKEN_KEY = 'ai-rag.dev-token';

/** 开发期凭证：`?token=` 优先（方便截图/演示），其次 sessionStorage（刷新不丢）。 */
function readInitialToken(): string {
  if (typeof window === 'undefined') return '';
  const fromUrl = new URLSearchParams(window.location.search).get('token');
  if (fromUrl) {
    window.sessionStorage.setItem(TOKEN_KEY, fromUrl);
    return fromUrl;
  }
  return window.sessionStorage.getItem(TOKEN_KEY) ?? '';
}

export function App() {
  const [token, setToken] = useState<string>(readInitialToken);
  const [draftToken, setDraftToken] = useState<string>('');
  const [source, setSource] = useState<DataSource>('live');
  const { state, send, stop, reset, selectCitation, setSource: setChatSource, scenarios } = useChat({
    token,
  });
  const [question, setQuestion] = useState(DEFAULT_QUESTION);
  const [scenarioId, setScenarioId] = useState(scenarios[0]?.id ?? 'normal');
  const [scope, setScope] = useState('');

  const isBusy = state.status === 'retrieving' || state.status === 'streaming';

  const scenarioOptions = useMemo(
    () => scenarios.map((item) => ({ id: item.id, name: item.name, description: item.description })),
    [scenarios],
  );

  const docKeys = useMemo(
    () =>
      scope
        .split(/[,，\s]+/)
        .map((part) => part.trim())
        .filter(Boolean),
    [scope],
  );

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (isBusy) return;
    send(question, { docKeys, scenarioId });
  };

  const onClarifyOption = (option: ClarifyOption) => {
    // 回填：点选项 → 用原问题 + 收敛后的 scope 重发（附录 C.1 的 scope.doc_keys）。
    const docKey =
      typeof option.doc_key === 'string' && option.doc_key ? option.doc_key : option.value;
    const nextScope = docKey ? [docKey] : undefined;
    setScope(docKey ?? '');
    if (isBusy) return;
    send(question, { docKeys: nextScope, scopeNote: option.label });
  };

  const onSelectFallbackDoc = (doc: FallbackDoc) => {
    // 降级的原文清单没有编号也没有坐标：用 n=0 + 无 bbox 的引用形状去预览，
    // 预览区会走「页码 + 片段」降级视图。
    selectCitation({
      n: 0,
      doc_key: doc.doc_key,
      doc_name: doc.doc_name,
      page_num: typeof doc.page_num === 'number' ? doc.page_num : 1,
      snippet: doc.snippet ?? '',
      chunk_id: '',
    });
  };

  const onSelect = useCallback((citation: CitationItem) => selectCitation(citation), [selectCitation]);

  const citationRate = useMemo(() => {
    const lastAssistant = [...state.messages].reverse().find((m) => m.role === 'assistant');
    if (!lastAssistant) return undefined;
    const audit = auditCitations(lastAssistant.text, lastAssistant.citations);
    return { resolvable: audit.resolvable.length, referenced: audit.referenced.length, rate: audit.resolvableRate };
  }, [state.messages]);

  const changeSource = (next: DataSource) => {
    setSource(next);
    setChatSource(next);
  };

  const saveToken = () => {
    const value = draftToken.trim();
    setToken(value);
    setDraftToken('');
    if (typeof window !== 'undefined') window.sessionStorage.setItem(TOKEN_KEY, value);
  };

  const onScopeChange = (event: ChangeEvent<HTMLInputElement>) => setScope(event.target.value);

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <h1>AI-RAG 问答工作台</h1>
          <p className="subtitle">
            F2：真实 SSE（<code>POST /api/v1/qa/stream</code>）+ 引用角标 → PDF 跳页 → 归一化 bbox 高亮
          </p>
        </div>
        <div className="topbar-actions">
          <button type="button" className="ghost" onClick={reset} disabled={isBusy} data-testid="reset">
            清空会话
          </button>
        </div>
      </header>

      <StatusBar
        status={state.status}
        source={state.source}
        lastStepPhase={state.lastStep?.phase}
        lastStepHop={state.lastStep?.hop}
        latencyMs={state.lastDone?.latency_ms}
        hopCount={state.lastDone?.hop_count}
        verdict={state.lastDone?.verdict}
        error={state.error}
        droppedFrames={state.droppedFrames}
        citationRate={citationRate}
      />

      <div className="settings-bar">
        <div className="field">
          <label htmlFor="source">数据源</label>
          <select
            id="source"
            value={source}
            onChange={(event) => changeSource(event.target.value as DataSource)}
            disabled={isBusy}
            data-testid="source-select"
          >
            <option value="live">真实 SSE（后端 /api/v1/qa/stream）</option>
            <option value="mock">演示回放（合成帧）</option>
          </select>
        </div>

        {source === 'mock' && (
          <div className="field">
            <label htmlFor="scenario">回放场景</label>
            <select
              id="scenario"
              value={scenarioId}
              onChange={(event) => setScenarioId(event.target.value)}
              disabled={isBusy}
            >
              {scenarioOptions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {source === 'live' && (
          <div className="field field-token">
            <label htmlFor="token">凭证（Bearer JWT）</label>
            <input
              id="token"
              type="text"
              className="mono"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="粘贴开发用 JWT（或 ?token=…）"
              data-testid="token-input"
            />
            <input
              type="text"
              className="mono token-paste"
              value={draftToken}
              onChange={(event) => setDraftToken(event.target.value)}
              placeholder="粘贴后点「换凭证」"
            />
            <button type="button" className="ghost" onClick={saveToken} disabled={!draftToken.trim()}>
              换凭证
            </button>
            <span className="token-state">{token ? '已配置' : '未配置（请求会 401）'}</span>
          </div>
        )}

        <div className="field field-scope">
          <label htmlFor="scope">范围（scope.doc_keys，逗号分隔）</label>
          <input
            id="scope"
            value={scope}
            onChange={onScopeChange}
            placeholder="policy/travel-cn（留空 = 全部有权限文档）"
            disabled={isBusy}
            data-testid="scope-input"
          />
        </div>
      </div>

      <main className="workspace">
        <section className="pane pane-chat" aria-label="问答区">
          <div className="pane-header">
            <h2>问答区</h2>
            {source === 'mock' && (
              <div className="scenario-desc">
                {scenarioOptions.find((item) => item.id === scenarioId)?.description}
              </div>
            )}
          </div>

          <MessageList
            messages={state.messages}
            interactive={!isBusy}
            onSelectCitation={onSelect}
            onClarifyOption={onClarifyOption}
            onSelectFallbackDoc={onSelectFallbackDoc}
          />

          <form className="composer" onSubmit={onSubmit}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="输入问题…"
              rows={3}
              aria-label="问题输入"
              data-testid="question-input"
            />
            <div className="composer-actions">
              <div className="composer-hint">
                {isBusy ? '流式进行中，可中断。' : 'Enter 提交请用按钮（避免误发）。'}
              </div>
              <div className="composer-buttons">
                {isBusy ? (
                  <button type="button" className="danger" onClick={stop} data-testid="stop">
                    中断
                  </button>
                ) : (
                  <button
                    type="submit"
                    className="primary"
                    disabled={!question.trim()}
                    data-testid="send"
                  >
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
            <div className="preview-note">PDF.js 渲染 · 页码跳转 · 归一化 bbox 高亮 · 坐标缺失降级</div>
          </div>
          <PdfPreview selectedCitation={state.selectedCitation} token={token} />
        </section>
      </main>
    </div>
  );
}
