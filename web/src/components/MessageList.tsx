import type { ReactNode } from 'react';
import type { ChatMessage, CitationItem } from '../types';

interface Props {
  messages: ChatMessage[];
  onSelectCitation: (c: CitationItem) => void;
  onClarifyOption: (value: string, label: string) => void;
}

function renderTextWithCitations(
  text: string,
  citations: CitationItem[] | undefined,
  onSelect: (c: CitationItem) => void,
): ReactNode {
  if (!citations || citations.length === 0) return text;
  const byN = new Map(citations.map((c) => [c.n, c]));
  const parts: ReactNode[] = [];
  const re = /\[(\d+)\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const idx = m.index;
    if (idx > last) parts.push(text.slice(last, idx));
    const n = Number(m[1]);
    const c = byN.get(n);
    if (c) {
      parts.push(
        <button
          key={`${idx}-${n}`}
          className="citation-ref"
          onClick={() => onSelect(c)}
          title={`${c.doc_name} p.${c.page_num}`}
          type="button"
        >
          [{n}]
        </button>,
      );
    } else {
      parts.push(m[0]);
    }
    last = idx + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts.length > 0 ? parts : text;
}

export function MessageList({ messages, onSelectCitation, onClarifyOption }: Props) {
  return (
    <div className="message-list" role="log" aria-label="消息流">
      {messages.length === 0 && (
        <div className="empty-hint">
          输入问题开始对话。可切换上方 Mock 场景验证 step/clarify/token/citations/fallback/done。
        </div>
      )}
      {messages.map((msg) => (
        <div key={msg.id} className={`msg msg-${msg.role} msg-${msg.status ?? 'normal'}`}>
          <div className="msg-meta">
            <span className="msg-role">
              {msg.role === 'user' ? '用户' : msg.role === 'assistant' ? '助手' : '系统'}
            </span>
            {msg.status && <span className="msg-status">{msg.status}</span>}
          </div>
          <div className="msg-text">
            {msg.role === 'assistant'
              ? renderTextWithCitations(msg.text, msg.citations, onSelectCitation)
              : msg.text}
            {msg.status === 'streaming' && <span className="cursor" aria-hidden="true" />}
          </div>

          {msg.clarify && (
            <div className="clarify-box">
              <div className="clarify-question">{msg.clarify.question}</div>
              <div className="clarify-options">
                {msg.clarify.options.map((opt) => (
                  <button
                    key={opt.value}
                    type="button"
                    className="clarify-option"
                    onClick={() => onClarifyOption(opt.value, opt.label)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {msg.fallback && (
            <div className="fallback-box" role="note">
              <div className="fallback-code">降级: {msg.fallback.code}</div>
              <div className="fallback-msg">{msg.fallback.message}</div>
              {msg.fallback.docs.length > 0 && (
                <ul className="fallback-docs">
                  {msg.fallback.docs.map((d, i) => (
                    <li key={`${d.doc_key}-${i}`}>
                      {d.doc_name}
                      {typeof d.page_num === 'number' ? ` p.${d.page_num}` : ''}
                      {d.snippet ? ` — ${d.snippet}` : ''}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {msg.citations && msg.citations.length > 0 && (
            <div className="citations">
              {msg.citations.map((c) => (
                <button
                  key={`${msg.id}-${c.n}-${c.chunk_id}`}
                  type="button"
                  className="citation-chip"
                  onClick={() => onSelectCitation(c)}
                  title={c.snippet}
                >
                  [{c.n}] {c.doc_name} p.{c.page_num}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
