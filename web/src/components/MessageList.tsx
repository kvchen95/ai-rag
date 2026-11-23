import type { ReactNode } from 'react';
import { splitCitations } from '../citations';
import type { ChatMessage, CitationItem, ClarifyOption, FallbackDoc } from '../types';

interface Props {
  messages: ChatMessage[];
  /** 流式进行中不给点角标（§10.4「done 之后才允许点角标」）。 */
  interactive: boolean;
  onSelectCitation: (c: CitationItem) => void;
  onClarifyOption: (option: ClarifyOption) => void;
  onSelectFallbackDoc: (doc: FallbackDoc) => void;
}

function renderAnswer(
  msg: ChatMessage,
  interactive: boolean,
  onSelect: (c: CitationItem) => void,
): ReactNode {
  // 正文里的 `[n]` → 可点角标；编号越界的 `[n]` 原样留在文本里（点不动的按钮更糟）。
  const segments = splitCitations(msg.text, msg.citations);
  return segments.map((segment, index) => {
    if (segment.kind === 'text') return <span key={index}>{segment.text}</span>;
    const citation = segment.citation;
    if (!citation) return <span key={index}>[{segment.n}]</span>;
    return (
      <button
        key={index}
        type="button"
        className="citation-ref"
        disabled={!interactive}
        onClick={() => onSelect(citation)}
        title={`${citation.doc_name} p.${citation.page_num}${citation.snippet ? `｜${citation.snippet}` : ''}`}
        aria-label={`引用 ${segment.n}，跳转到 ${citation.doc_name} 第 ${citation.page_num} 页`}
        data-testid="citation-badge"
        data-citation-n={segment.n}
        data-doc-key={citation.doc_key}
        data-page={citation.page_num}
        data-has-bbox={citation.bbox && citation.bbox.length > 0 ? '1' : '0'}
      >
        [{segment.n}]
      </button>
    );
  });
}

const STATUS_LABEL: Record<NonNullable<ChatMessage['status']>, string> = {
  pending: '等待中',
  streaming: '生成中',
  done: '结束',
  interrupted: '已中断',
  error: '失败',
};

export function MessageList({
  messages,
  interactive,
  onSelectCitation,
  onClarifyOption,
  onSelectFallbackDoc,
}: Props) {
  return (
    <div className="message-list" role="log" aria-label="消息流">
      {messages.length === 0 && (
        <div className="empty-hint">
          输入问题开始对话。真实模式走 <code>POST /api/v1/qa/stream</code>；
          「演示回放」是合成帧，只用于没有后端时看事件顺序。
        </div>
      )}
      {messages.map((msg) => (
        <div
          key={msg.id}
          className={`msg msg-${msg.role} msg-${msg.status ?? 'normal'}`}
          data-testid="msg"
          data-role={msg.role}
          data-status={msg.status ?? ''}
        >
          <div className="msg-meta">
            <span className="msg-role">
              {msg.role === 'user' ? '用户' : msg.role === 'assistant' ? '助手' : '系统'}
            </span>
            {msg.status && <span className="msg-status">{STATUS_LABEL[msg.status] ?? msg.status}</span>}
            {msg.verdict && (
              <span className="msg-verdict" title="done.verdict（§8.4 五态 + unverifiable）">
                verdict: {msg.verdict}
              </span>
            )}
          </div>

          <div className="msg-text" data-testid="msg-text">
            {msg.role === 'assistant' ? renderAnswer(msg, interactive, onSelectCitation) : msg.text}
            {msg.status === 'streaming' && <span className="cursor" aria-hidden="true" />}
          </div>

          {msg.scopeNote && <div className="msg-scope-note">已按上一条追问的选择收窄范围：{msg.scopeNote}</div>}

          {msg.clarify && (
            <div className="clarify-box" data-testid="clarify-box">
              <div className="clarify-question">{msg.clarify.question}</div>
              <div className="clarify-options">
                {msg.clarify.options.map((opt, index) => (
                  <button
                    key={`${opt.value}-${index}`}
                    type="button"
                    className="clarify-option"
                    onClick={() => onClarifyOption(opt)}
                    data-testid="clarify-option"
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
              {msg.clarify.options.length === 0 && (
                <div className="muted">服务端没给可选项，请直接补充问题重问。</div>
              )}
            </div>
          )}

          {msg.fallback && (
            <div className="fallback-box" role="note" data-testid="fallback-box">
              <div className="fallback-code">降级码：{msg.fallback.code}</div>
              <div className="fallback-msg">{msg.fallback.message}</div>
              {msg.fallback.docs.length > 0 && (
                <ul className="fallback-docs">
                  {msg.fallback.docs.map((doc, index) => (
                    <li key={`${doc.doc_key}-${index}`}>
                      <button
                        type="button"
                        className="link-button"
                        onClick={() => onSelectFallbackDoc(doc)}
                        data-testid="fallback-doc"
                      >
                        {doc.doc_name}
                        {typeof doc.page_num === 'number' ? ` p.${doc.page_num}` : ''}
                      </button>
                      {doc.snippet ? <span className="muted"> — {doc.snippet}</span> : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {msg.citations && msg.citations.length > 0 && (
            <div className="citations" data-testid="citation-chips">
              {msg.citations.map((citation) => (
                <button
                  key={`${msg.id}-${citation.n}-${citation.chunk_id}`}
                  type="button"
                  className="citation-chip"
                  disabled={!interactive}
                  onClick={() => onSelectCitation(citation)}
                  title={citation.snippet}
                >
                  [{citation.n}] {citation.doc_name} p.{citation.page_num}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
