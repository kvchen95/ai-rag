import { describe, expect, it } from 'vitest';
import { initialState, reduce } from './state';
import type { SseEvent } from './types';

function runEvents(runId: string, messageId: string, events: SseEvent[]) {
  let state = reduce(initialState, {
    type: 'user_submit',
    question: 'Q',
    runId,
    messageId,
  });
  for (const event of events) {
    state = reduce(state, { type: 'sse_event', runId, event });
  }
  return state;
}

describe('stream reducer', () => {
  it('handles normal step/token/citations/done flow', () => {
    const state = runEvents('r1', 'a1', [
      { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 1 },
      { type: 'token', delta: 'hello' },
      { type: 'token', delta: ' world' },
      {
        type: 'citations',
        items: [
          {
            n: 1,
            doc_key: 'd',
            doc_name: 'doc',
            page_num: 1,
            snippet: 's',
            chunk_id: 'c1',
          },
        ],
      },
      { type: 'done', latency_ms: 10, hop_count: 1, verdict: 'pass' },
    ]);
    expect(state.status).toBe('done');
    expect(state.activeRunId).toBeNull();
    const msg = state.messages.find((m) => m.id === 'a1');
    expect(msg?.text).toBe('hello world');
    expect(msg?.citations?.length).toBe(1);
    expect(msg?.status).toBe('done');
    expect(state.lastDone?.verdict).toBe('pass');
  });

  it('handles clarify path without tokens', () => {
    const state = runEvents('r2', 'a2', [
      { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 1 },
      {
        type: 'clarify',
        question: 'which?',
        options: [{ label: 'A', value: 'a' }],
      },
      { type: 'done', latency_ms: 2, hop_count: 1, verdict: 'clarify' },
    ]);
    const msg = state.messages.find((m) => m.id === 'a2');
    expect(state.status).toBe('done');
    expect(msg?.clarify?.options[0]?.value).toBe('a');
    expect(msg?.status).toBe('done');
  });

  it('handles fallback path and preserves fallback', () => {
    const state = runEvents('r3', 'a3', [
      { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 1 },
      {
        type: 'fallback',
        code: 'no_evidence',
        message: 'nope',
        docs: [{ doc_key: 'd', doc_name: 'D', page_num: 2 }],
      },
      { type: 'done', latency_ms: 5, hop_count: 1, verdict: 'reject_none' },
    ]);
    const msg = state.messages.find((m) => m.id === 'a3');
    expect(msg?.fallback?.code).toBe('no_evidence');
    expect(msg?.status).toBe('done');
    expect(state.status).toBe('done');
  });

  it('ignores late events after done (runId guard)', () => {
    let state = runEvents('r4', 'a4', [
      { type: 'step', hop: 0, phase: 'generate', elapsed_ms: 1 },
      { type: 'token', delta: 'ok' },
      { type: 'done', latency_ms: 2, hop_count: 1, verdict: 'pass' },
    ]);
    state = reduce(state, {
      type: 'sse_event',
      runId: 'r4',
      event: { type: 'token', delta: 'LATE' },
    });
    const msg = state.messages.find((m) => m.id === 'a4');
    expect(msg?.text).toBe('ok');
  });

  it('tolerates out-of-order/malformed events without crashing', () => {
    let state = reduce(initialState, {
      type: 'user_submit',
      question: 'Q',
      runId: 'r5',
      messageId: 'a5',
    });
    const weird: unknown[] = [
      { type: 'token', delta: 'A' },
      { type: 'citations', items: 'bad' },
      { type: 'unknown_event', foo: 1 },
      null,
      { type: 'step', hop: 0, phase: 'verify', elapsed_ms: 10 },
      { type: 'token', delta: 'B' },
      {
        type: 'citations',
        items: [
          { n: 1, doc_key: 'd', doc_name: 'n', page_num: 3, chunk_id: 'c', snippet: 's' },
          { bad: true },
        ],
      },
      { type: 'done', latency_ms: 20, hop_count: 1, verdict: 'pass' },
      // late frame after done should be ignored (runId no longer active)
      { type: 'token', delta: 'LATE' },
    ];
    for (const event of weird) {
      state = reduce(state, { type: 'sse_event', runId: 'r5', event: event as SseEvent });
    }
    const msg = state.messages.find((m) => m.id === 'a5');
    expect(msg?.text).toContain('A');
    expect(msg?.text).toContain('B');
    expect(msg?.text).not.toContain('LATE');
    expect(msg?.citations?.length).toBe(1);
    expect(state.status).toBe('done');
  });

  it('interrupt clears partial answer per spec', () => {
    let state = runEvents('r6', 'a6', [
      { type: 'step', hop: 0, phase: 'generate', elapsed_ms: 1 },
      { type: 'token', delta: 'partial' },
    ]);
    state = reduce(state, { type: 'sse_interrupted', runId: 'r6' });
    const msg = state.messages.find((m) => m.id === 'a6');
    expect(state.status).toBe('failed');
    expect(msg?.status).toBe('interrupted');
    expect(msg?.text).toBe('已中断，可重试。');
    expect(msg?.citations).toEqual([]);
  });

  it('ignores events for non-active run', () => {
    let state = reduce(initialState, {
      type: 'user_submit',
      question: 'Q1',
      runId: 'r1',
      messageId: 'a1',
    });
    state = reduce(state, { type: 'sse_event', runId: 'r2', event: { type: 'token', delta: 'x' } });
    const msg = state.messages.find((m) => m.id === 'a1');
    expect(msg?.text).toBe('');
  });

  it('does not accept tokens after a fallback (terminal message guard)', () => {
    let state = runEvents('r7', 'a7', [
      { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 1 },
      { type: 'fallback', code: 'model_timeout', message: '稍后重试', docs: [] },
    ]);
    state = reduce(state, { type: 'sse_event', runId: 'r7', event: { type: 'token', delta: 'LATE' } });
    const msg = state.messages.find((m) => m.id === 'a7');
    expect(msg?.text).toBe('稍后重试');
    expect(msg?.text).not.toContain('LATE');
  });

  it('keeps the first done when a duplicate done arrives', () => {
    let state = runEvents('r8', 'a8', [
      { type: 'token', delta: 'ok' },
      { type: 'done', latency_ms: 100, hop_count: 1, verdict: 'pass' },
    ]);
    const before = state;
    state = reduce(state, {
      type: 'sse_event',
      runId: 'r8',
      event: { type: 'done', latency_ms: 999, hop_count: 3, verdict: 'unverifiable' },
    });
    expect(state).toBe(before);
    expect(state.lastDone?.verdict).toBe('pass');
  });

  it('counts malformed and unknown events instead of crashing', () => {
    let state = reduce(initialState, { type: 'user_submit', question: 'Q', runId: 'r9', messageId: 'a9' });
    for (const event of [null, { type: 'tool_call' }, { type: 'citations', items: 'not-array' }]) {
      state = reduce(state, { type: 'sse_event', runId: 'r9', event: event as unknown as SseEvent });
    }
    expect(state.droppedFrames).toBeGreaterThanOrEqual(2);
    expect(state.status).toBe('retrieving');
  });

  it('shows a notice when the stream ends without any content', () => {
    const state = runEvents('r10', 'a10', [
      { type: 'done', latency_ms: 5, hop_count: 0, verdict: 'pass' },
    ]);
    const msg = state.messages.find((m) => m.id === 'a10');
    expect(msg?.text).toBe('（服务未返回正文）');
    expect(msg?.status).toBe('done');
  });

  it('keeps fallback code and done code apart, plus uncovered list', () => {
    const state = runEvents('r11', 'a11', [
      { type: 'fallback', code: 'unverifiable', message: '本次不作结论，附原文', docs: [] },
      {
        type: 'done',
        latency_ms: 30,
        hop_count: 2,
        verdict: 'partial',
        code: 'low_confidence',
        uncovered: ['二线城市上限'],
      },
    ]);
    const msg = state.messages.find((m) => m.id === 'a11');
    expect(msg?.fallback?.code).toBe('unverifiable');
    expect(msg?.verdict).toBe('partial');
    expect(state.lastDone?.uncovered).toEqual(['二线城市上限']);
  });

  it('keeps clarify options with their refill parameters', () => {
    const state = runEvents('r12', 'a12', [
      {
        type: 'clarify',
        question: '看哪一份？',
        options: [
          { label: '差旅管理办法', value: 'policy/travel-cn', doc_key: 'policy/travel-cn', page_hint: 47 },
        ],
      },
      { type: 'done', latency_ms: 10, hop_count: 1, verdict: 'clarify' },
    ]);
    const msg = state.messages.find((m) => m.id === 'a12');
    expect(msg?.clarify?.options[0]).toMatchObject({ value: 'policy/travel-cn', doc_key: 'policy/travel-cn' });
    expect(msg?.status).toBe('done');
  });

  it('stores only the fixed UiError copy on HTTP failure', () => {
    let state = reduce(initialState, { type: 'user_submit', question: 'Q', runId: 'r13', messageId: 'a13' });
    state = reduce(state, {
      type: 'sse_error',
      runId: 'r13',
      error: { kind: 'forbidden', message: '无权查看该文档', httpStatus: 403 },
    });
    expect(state.status).toBe('failed');
    expect(state.error).toEqual({ kind: 'forbidden', message: '无权查看该文档', httpStatus: 403 });
    expect(state.messages.find((m) => m.id === 'a13')?.text).toBe('无权查看该文档');
  });

  it('does not resurrect an interrupted run with a late done', () => {
    let state = runEvents('r14', 'a14', [
      { type: 'step', hop: 0, phase: 'generate', elapsed_ms: 1 },
      { type: 'token', delta: '半截' },
    ]);
    state = reduce(state, { type: 'sse_interrupted', runId: 'r14' });
    state = reduce(state, {
      type: 'sse_event',
      runId: 'r14',
      event: { type: 'done', latency_ms: 99, hop_count: 0, verdict: 'pass' },
    });
    const msg = state.messages.find((m) => m.id === 'a14');
    expect(msg?.status).toBe('interrupted');
    expect(state.lastDone).toBeNull();
  });
});
