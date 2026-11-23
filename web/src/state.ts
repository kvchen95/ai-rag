// 纯状态机（可在 node 环境单测，不 import 任何框架 API）。
//
// 三条不变量（§10.4 + 附录 C.2）：
// 1. 一帧只属于一个 runId；`runId ≠ 活跃 run` 一律丢弃（中断/重发后的旧尾巴）。
// 2. 终态（done / interrupted / error）之后不再接受任何写入——**包括迟到的 done**
//    与迟到的 token。中断的 run 若被迟到的 done 收尾，界面会把「中断」显示成「完成」。
// 3. 乱序/未知/畸形事件不许抛：忽略并计数（`droppedFrames`），业务继续跑。
//
// 另外两条「界面诚实」的口径：
// - 拒答（clarify/fallback）路径没有正文，不算失败：状态是 done，正文用降级说明兜底。
// - 错误只存 UiError（固定文案 + 状态码），服务端响应体的任何正文都不进状态。

import type {
  ChatMessage,
  CitationItem,
  ClarifyEvent,
  DoneEvent,
  FallbackEvent,
  SseEvent,
  StepEvent,
  StreamState,
  UiError,
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
  source: 'live',
  droppedFrames: 0,
};

export type Action =
  | {
      type: 'user_submit';
      question: string;
      runId: string;
      messageId: string;
      scopeNote?: string;
    }
  | { type: 'sse_event'; runId: string; event: SseEvent }
  | { type: 'sse_dropped'; runId: string; count: number }
  | { type: 'sse_error'; runId: string; error: UiError }
  | { type: 'sse_interrupted'; runId: string }
  | { type: 'select_citation'; citation: CitationItem | null }
  | { type: 'set_source'; source: StreamState['source'] }
  | { type: 'reset' };

function isActive(state: StreamState, runId: string): boolean {
  return state.activeRunId === runId && state.activeMessageId !== null;
}

function isTerminalMessage(msg: ChatMessage): boolean {
  return msg.status === 'done' || msg.status === 'interrupted' || msg.status === 'error';
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

/** 终态消息不再接受写入（不变量 2）。返回 null 表示这帧该被丢掉。 */
function updateOpenMessage(
  state: StreamState,
  updater: (msg: ChatMessage) => ChatMessage,
): StreamState | null {
  const id = state.activeMessageId;
  if (!id) return null;
  const current = state.messages.find((m) => m.id === id);
  if (!current || isTerminalMessage(current)) return null;
  return updateActiveMessage(state, updater);
}

/**
 * 正文类事件（token / citations）的写入门槛。
 *
 * 除了「不是终态」，还要满足「这条消息还没有降级/追问结论」：`fallback` 之后
 * 再来 token 就是服务端在自相矛盾（§10.4 的箭头写的是「或」）。不挡的话，
 * 界面上会出现「本次不作结论，附原文」下面又长出一段结论。
 */
function updateTextOpenMessage(
  state: StreamState,
  updater: (msg: ChatMessage) => ChatMessage,
): StreamState | null {
  const id = state.activeMessageId;
  if (!id) return null;
  const current = state.messages.find((m) => m.id === id);
  if (!current || isTerminalMessage(current)) return null;
  if (current.fallback || current.clarify) return null;
  return updateActiveMessage(state, updater);
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function applyStep(state: StreamState, event: StepEvent): StreamState | null {
  const next = updateOpenMessage(state, (m) => ({
    ...m,
    // step 只推进阶段，不改正文：正文只在 token 里来。
    status: m.text ? 'streaming' : 'pending',
  }));
  if (!next) return null;
  return {
    ...next,
    status: next.status === 'streaming' ? 'streaming' : 'retrieving',
    lastStep: {
      type: 'step',
      hop: numberOr(event.hop, 0),
      phase: typeof event.phase === 'string' ? event.phase : 'unknown',
      elapsed_ms: numberOr(event.elapsed_ms, 0),
      note: typeof event.note === 'string' ? event.note : undefined,
    },
    error: null,
  };
}

/**
 * token：正文增量。
 * 合帧（rAF 合并渲染）在 `useChat` 里做，这里只做状态合并——一次 dispatch
 * 一次 React 提交，长答案也不会逐帧重渲染历史消息。
 */
function applyToken(state: StreamState, delta: string): StreamState | null {
  const safeDelta = typeof delta === 'string' ? delta : '';
  if (!safeDelta) return null;
  const next = updateTextOpenMessage(state, (m) => ({
    ...m,
    text: m.text + safeDelta,
    status: 'streaming',
  }));
  if (!next) return null;
  return { ...next, status: 'streaming', error: null };
}

function isCitationItem(value: unknown): value is CitationItem {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.n === 'number' &&
    Number.isFinite(v.n) &&
    typeof v.doc_key === 'string' &&
    v.doc_key.length > 0 &&
    typeof v.doc_name === 'string' &&
    typeof v.page_num === 'number' &&
    Number.isFinite(v.page_num) &&
    typeof v.chunk_id === 'string' &&
    v.chunk_id.length > 0
  );
}

/** bbox 的过滤在 `bbox.ts` 里做（那里要判归一化范围），这里只收合法的 items。 */
function applyCitations(state: StreamState, items: unknown): StreamState | null {
  const safeItems = Array.isArray(items) ? items.filter(isCitationItem) : [];
  const next = updateTextOpenMessage(state, (m) => ({ ...m, citations: safeItems }));
  if (!next) return null;
  return next;
}

function applyClarify(state: StreamState, event: ClarifyEvent): StreamState | null {
  const question = typeof event.question === 'string' ? event.question : '需要补充信息';
  const options = Array.isArray(event.options)
    ? event.options.filter(
        (o): o is ClarifyEvent['options'][number] =>
          !!o &&
          typeof o === 'object' &&
          typeof (o as { label?: unknown }).label === 'string' &&
          typeof (o as { value?: unknown }).value === 'string',
      )
    : [];
  const clarify: ClarifyEvent = { type: 'clarify', question, options };
  const next = updateOpenMessage(state, (m) => ({
    ...m,
    clarify,
    status: 'done',
    // 拒答路径没有正文：把追问的话放进正文，读起来才不是空白气泡。
    text: m.text || question,
  }));
  if (!next) return null;
  return { ...next, status: 'done', error: null };
}

function applyFallback(state: StreamState, event: FallbackEvent): StreamState | null {
  const code = typeof event.code === 'string' ? event.code : 'unknown';
  const fallback: FallbackEvent = {
    type: 'fallback',
    code,
    message: typeof event.message === 'string' ? event.message : '未能生成可验证结论',
    docs: Array.isArray(event.docs)
      ? event.docs.filter(
          (d): d is FallbackEvent['docs'][number] =>
            !!d && typeof d === 'object' && typeof (d as { doc_key?: unknown }).doc_key === 'string',
      )
      : [],
  };
  const next = updateOpenMessage(state, (m) => ({
    ...m,
    fallback,
    code,
    status: 'done',
    text: m.text || fallback.message,
  }));
  if (!next) return null;
  return { ...next, status: 'done', error: null };
}

function applyDone(state: StreamState, event: DoneEvent): StreamState | null {
  const done: DoneEvent = {
    type: 'done',
    latency_ms: numberOr(event.latency_ms, 0),
    hop_count: numberOr(event.hop_count, 0),
    verdict: typeof event.verdict === 'string' ? event.verdict : 'unknown',
    code: typeof event.code === 'string' ? event.code : undefined,
    uncovered: Array.isArray(event.uncovered)
      ? event.uncovered.filter((item): item is string => typeof item === 'string')
      : undefined,
  };
  // done 是唯一能写进「已经有 fallback/clarify 结论」的消息的事件：它不改正文，
  // 只补上 verdict/code/uncovered 并把这次执行收尾。所以这里不走终态守卫，
  // 但仍要求这条 run 是活跃的（迟到/重复的 done 在调用方已被 isActive 挡掉）。
  const next = updateActiveMessage(state, (m) => ({
    ...m,
    status: 'done',
    verdict: done.verdict,
    code: done.code ?? m.code,
    // 一条正文都没有、也没有降级说明：不要留一个空气泡让人以为卡住了。
    text: m.text || m.fallback?.message || m.clarify?.question || '（服务未返回正文）',
  }));
  if (!next) return null;
  return {
    ...next,
    status: 'done',
    lastDone: done,
    error: null,
    // 只有 done 之后才允许点角标（§10.4）；这里顺手把活跃 run 收掉，
    // 迟到帧从此被 isActive 挡在门外。
    activeRunId: null,
    activeMessageId: null,
  };
}

export function reduce(state: StreamState, action: Action): StreamState {
  switch (action.type) {
    case 'reset':
      return { ...initialState, source: state.source };

    case 'set_source':
      return { ...initialState, source: action.source };

    case 'user_submit': {
      const question = action.question.trim();
      if (!question) return state;
      const userMsg: ChatMessage = {
        id: `u-${action.runId}`,
        role: 'user',
        text: question,
        scopeNote: action.scopeNote,
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
      return { ...state, selectedCitation: action.citation };

    case 'sse_dropped': {
      if (!isActive(state, action.runId)) return state;
      const count = Math.max(0, Math.round(numberOr(action.count, 0)));
      return count === 0 ? state : { ...state, droppedFrames: state.droppedFrames + count };
    }

    case 'sse_error': {
      if (!isActive(state, action.runId)) return state;
      const next = updateActiveMessage(state, (m) => ({
        ...m,
        status: 'error',
        text: m.text || action.error.message,
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
      // §10.4「连接中断则显示已中断，可重试，不保留半截答案」。
      const next = updateActiveMessage(state, (m) => ({
        ...m,
        status: 'interrupted',
        text: '已中断，可重试。',
        citations: [],
        clarify: undefined,
        fallback: undefined,
        verdict: undefined,
      }));
      return {
        ...next,
        status: 'failed',
        error: { kind: 'interrupted', message: '已中断，可重试' },
        activeRunId: null,
        activeMessageId: null,
        lastDone: null,
      };
    }

    case 'sse_event': {
      if (!isActive(state, action.runId)) return state;
      const event = action.event;
      if (!event || typeof event !== 'object' || typeof (event as { type?: unknown }).type !== 'string') {
        return { ...state, droppedFrames: state.droppedFrames + 1 };
      }
      switch (event.type) {
        case 'step':
          return applyStep(state, event) ?? state;
        case 'token':
          return applyToken(state, event.delta) ?? state;
        case 'citations':
          return applyCitations(state, event.items) ?? state;
        case 'clarify':
          return applyClarify(state, event) ?? state;
        case 'fallback':
          return applyFallback(state, event) ?? state;
        case 'done':
          return applyDone(state, event) ?? state;
        default:
          // 未知事件类型：服务端加了新事件，老前端不该崩，也不该假装理解。
          return { ...state, droppedFrames: state.droppedFrames + 1 };
      }
    }

    default:
      return state;
  }
}
