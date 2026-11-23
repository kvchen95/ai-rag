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
});
