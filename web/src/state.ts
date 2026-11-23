import type {
  ChatMessage,
  CitationItem,
  ClarifyEvent,
  DoneEvent,
  FallbackEvent,
  SseEvent,
  StepEvent,
  StreamState,
} from './types';

export const initialState: StreamState = {
  status: 'idle',
  messages: [],
  activeMessageId: null,
  activeRunId: null,
  lastStep: null,
  lastDone: null,
  error: null,
  selectedCitation: null,
};

export type Action =
  | { type: 'user_submit'; question: string; runId: string; messageId: string }
  | { type: 'sse_event'; runId: string; event: SseEvent }
  | { type: 'sse_error'; runId: string; error: string }
  | { type: 'sse_interrupted'; runId: string }
  | { type: 'select_citation'; citation: CitationItem | null }
  | { type: 'reset' };

function isActive(state: StreamState, runId: string): boolean {
  return state.activeRunId === runId && state.activeMessageId !== null;
}

function updateActiveMessage(
  state: StreamState,
  updater: (msg: ChatMessage) => ChatMessage,
): StreamState {
  const id = state.activeMessageId;
  if (!id) return state;
  let changed = false;
  const messages = state.messages.map((m) => {
    if (m.id !== id) return m;
    changed = true;
    return updater(m);
  });
  return changed ? { ...state, messages } : state;
}

function applyStep(state: StreamState, event: StepEvent): StreamState {
  return {
    ...updateActiveMessage(state, (m) => ({ ...m, status: 'streaming' })),
    status: state.status === 'streaming' ? 'streaming' : 'retrieving',
    lastStep: event,
    error: null,
  };
}

function applyToken(state: StreamState, delta: string): StreamState {
  const safeDelta = typeof delta === 'string' ? delta : '';
  if (!safeDelta) return state;
  return {
    ...updateActiveMessage(state, (m) => ({
      ...m,
      text: m.text + safeDelta,
      status: 'streaming',
    })),
    status: 'streaming',
    error: null,
  };
}

function applyCitations(state: StreamState, items: CitationItem[]): StreamState {
  const safeItems = Array.isArray(items) ? items.filter(isCitationItem) : [];
  return updateActiveMessage(state, (m) => ({
    ...m,
    citations: safeItems,
  }));
}

function isCitationItem(value: unknown): value is CitationItem {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.n === 'number' &&
    typeof v.doc_key === 'string' &&
    typeof v.doc_name === 'string' &&
    typeof v.page_num === 'number' &&
    typeof v.chunk_id === 'string'
  );
}

function applyClarify(state: StreamState, event: ClarifyEvent): StreamState {
  const question = typeof event.question === 'string' ? event.question : '需要补充信息';
  const options = Array.isArray(event.options)
    ? event.options.filter(
        (o): o is { label: string; value: string } =>
          !!o && typeof o.label === 'string' && typeof o.value === 'string',
      )
    : [];
  const clarify: ClarifyEvent = { type: 'clarify', question, options };
  return updateActiveMessage(state, (m) => ({
    ...m,
    clarify,
    status: 'done',
    text: m.text || question,
  }));
}

function applyFallback(state: StreamState, event: FallbackEvent): StreamState {
  const fallback: FallbackEvent = {
    type: 'fallback',
    code: typeof event.code === 'string' ? event.code : 'unknown',
    message: typeof event.message === 'string' ? event.message : '未能生成可验证结论',
    docs: Array.isArray(event.docs) ? event.docs : [],
  };
  return updateActiveMessage(state, (m) => ({
    ...m,
    fallback,
    status: 'done',
    text: m.text || fallback.message,
  }));
}

function applyDone(state: StreamState, event: DoneEvent): StreamState {
  const done: DoneEvent = {
    type: 'done',
    latency_ms: typeof event.latency_ms === 'number' ? event.latency_ms : 0,
    hop_count: typeof event.hop_count === 'number' ? event.hop_count : 0,
    verdict: typeof event.verdict === 'string' ? event.verdict : 'unknown',
  };
  const next = updateActiveMessage(state, (m) => {
    // fallback/clarify already set terminal text/status semantics; keep them.
    if (m.fallback || m.clarify) return { ...m, status: 'done' };
    // Do not preserve partial answer as normal success if never got content;
    // but keep whatever arrived, marking done.
    return { ...m, status: 'done' };
  });
  return {
    ...next,
    status: 'done',
    lastDone: done,
    activeRunId: null,
    activeMessageId: null,
  };
}

export function reduce(state: StreamState, action: Action): StreamState {
  switch (action.type) {
    case 'reset':
      return initialState;

    case 'user_submit': {
      const question = action.question.trim();
      if (!question) return state;
      const userMsg: ChatMessage = {
        id: `u-${action.runId}`,
        role: 'user',
        text: question,
      };
      const assistantMsg: ChatMessage = {
        id: action.messageId,
        role: 'assistant',
        text: '',
        status: 'pending',
        citations: [],
      };
      return {
        ...state,
        status: 'retrieving',
        messages: [...state.messages, userMsg, assistantMsg],
        activeRunId: action.runId,
        activeMessageId: action.messageId,
        lastStep: null,
        lastDone: null,
        error: null,
      };
    }

    case 'select_citation':
      // Only allow citation selection after done/fallback terminal states; during
      // streaming we keep it harmless but allow preview for mock usability.
      return { ...state, selectedCitation: action.citation };

    case 'sse_error': {
      if (!isActive(state, action.runId)) return state;
      const next = updateActiveMessage(state, (m) => ({
        ...m,
        status: 'error',
        text: m.text || '请求失败，可重试。',
      }));
      return {
        ...next,
        status: 'failed',
        error: action.error,
        activeRunId: null,
        activeMessageId: null,
      };
    }

    case 'sse_interrupted': {
      if (!isActive(state, action.runId)) return state;
      // Spec: "连接中断则显示已中断，可重试，不保留半截答案"
      const next = updateActiveMessage(state, (m) => ({
        ...m,
        status: 'interrupted',
        text: '已中断，可重试。',
        citations: [],
        clarify: undefined,
        fallback: undefined,
      }));
      return {
        ...next,
        status: 'failed',
        error: 'stream_interrupted',
        activeRunId: null,
        activeMessageId: null,
        lastDone: null,
      };
    }

    case 'sse_event': {
      if (!isActive(state, action.runId)) return state;
      const event = action.event;
      if (!event || typeof event !== 'object' || typeof (event as { type?: unknown }).type !== 'string') {
        return state;
      }
      // Defensive: out-of-order or unknown events must not crash.
      switch (event.type) {
        case 'step':
          return applyStep(state, event);
        case 'token':
          // token before step is tolerated; move to streaming.
          return applyToken(state, event.delta);
        case 'citations':
          return applyCitations(state, event.items);
        case 'clarify':
          return applyClarify(state, event);
        case 'fallback':
          return applyFallback(state, event);
        case 'done':
          return applyDone(state, event);
        default:
          return state;
      }
    }

    default:
      return state;
  }
}
