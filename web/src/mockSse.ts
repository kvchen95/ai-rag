import type { CitationItem, SseEvent } from './types';

export interface MockScenario {
  id: string;
  name: string;
  description: string;
  events: Array<{ delayMs: number; event: SseEvent }>;
}

const baseCitations: CitationItem[] = [
  {
    n: 1,
    doc_key: 'policy/travel-cn',
    doc_name: '差旅管理办法-2025版',
    page_num: 47,
    bbox: [[0.12, 0.34, 0.88, 0.51]],
    snippet: '住宿费标准：每人每天 500 元。',
    chunk_id: 'ch_9f2c31',
  },
  {
    n: 2,
    doc_key: 'policy/travel-cn',
    doc_name: '差旅管理办法-2025版',
    page_num: 48,
    bbox: [[0.1, 0.62, 0.9, 0.74]],
    snippet: '一线城市出差住宿费上限可按 600 元执行。',
    chunk_id: 'ch_7b01aa',
  },
];

export const scenarios: MockScenario[] = [
  {
    id: 'normal',
    name: '正常回答',
    description: 'step -> token -> citations -> done',
    events: [
      { delayMs: 100, event: { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 120 } },
      { delayMs: 350, event: { type: 'step', hop: 0, phase: 'rerank', elapsed_ms: 420 } },
      { delayMs: 550, event: { type: 'step', hop: 0, phase: 'generate', elapsed_ms: 680 } },
      { delayMs: 700, event: { type: 'token', delta: '根据差旅制度，' } },
      { delayMs: 820, event: { type: 'token', delta: '2025年住宿费标准为' } },
      { delayMs: 940, event: { type: 'token', delta: '每人每天 500 元[1]。' } },
      { delayMs: 1080, event: { type: 'token', delta: '一线城市可按 600 元执行[2]。' } },
      { delayMs: 1250, event: { type: 'step', hop: 0, phase: 'verify', elapsed_ms: 1310 } },
      { delayMs: 1400, event: { type: 'citations', items: baseCitations } },
      { delayMs: 1500, event: { type: 'done', latency_ms: 1500, hop_count: 1, verdict: 'pass' } },
    ],
  },
  {
    id: 'clarify',
    name: '澄清追问',
    description: 'step -> clarify -> done',
    events: [
      { delayMs: 120, event: { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 150 } },
      {
        delayMs: 450,
        event: {
          type: 'clarify',
          question: '请确认差旅城市级别，以便给出准确标准：',
          options: [
            { label: '一线城市', value: 'tier1' },
            { label: '二线城市', value: 'tier2' },
            { label: '其他城市', value: 'other' },
          ],
        },
      },
      { delayMs: 520, event: { type: 'done', latency_ms: 520, hop_count: 1, verdict: 'clarify' } },
    ],
  },
  {
    id: 'fallback',
    name: '降级拒答',
    description: 'step -> fallback -> done',
    events: [
      { delayMs: 120, event: { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 130 } },
      { delayMs: 420, event: { type: 'step', hop: 0, phase: 'rerank', elapsed_ms: 460 } },
      {
        delayMs: 700,
        event: {
          type: 'fallback',
          code: 'no_evidence',
          message: '未找到可验证依据，以下为相关原文片段供参考。',
          docs: [
            {
              doc_key: 'policy/travel-cn',
              doc_name: '差旅管理办法-2025版',
              page_num: 12,
              snippet: '本办法适用于公司员工因公出差场景。',
            },
          ],
        },
      },
      { delayMs: 760, event: { type: 'done', latency_ms: 760, hop_count: 1, verdict: 'reject_none' } },
    ],
  },
  {
    id: 'out_of_order',
    name: '乱序容错',
    description: '故意乱序：done/citations/token/step 混杂，不应崩溃',
    events: [
      { delayMs: 80, event: { type: 'step', hop: 0, phase: 'retrieval', elapsed_ms: 90 } },
      { delayMs: 150, event: { type: 'token', delta: '（乱序帧1）' } },
      { delayMs: 220, event: { type: 'citations', items: baseCitations } },
      { delayMs: 300, event: { type: 'step', hop: 0, phase: 'generate', elapsed_ms: 310 } },
      { delayMs: 380, event: { type: 'token', delta: '正常结论：住宿标准 500 元[1]。' } },
      // duplicate/out-of-order done before final citations in weird sequence
      { delayMs: 460, event: { type: 'done', latency_ms: 460, hop_count: 1, verdict: 'pass' } },
      // late frame after done should be ignored by activeRun guard in real stream;
      // here we still emit to exercise reducer tolerance when dispatched incorrectly.
      { delayMs: 560, event: { type: 'token', delta: '（迟到帧应被忽略）' } },
    ],
  },
];

export function getScenario(id: string): MockScenario {
  return scenarios.find((s) => s.id === id) ?? scenarios[0]!;
}

export interface MockStreamHandle {
  cancel: () => void;
  done: Promise<void>;
}

export function playMockSse(
  scenarioId: string,
  handlers: {
    onEvent: (event: SseEvent) => void;
    onError: (error: string) => void;
    onInterrupted?: () => void;
  },
): MockStreamHandle {
  const scenario = getScenario(scenarioId);
  const timers: Array<ReturnType<typeof setTimeout>> = [];
  let cancelled = false;
  let resolveDone!: () => void;
  const done = new Promise<void>((resolve) => {
    resolveDone = resolve;
  });

  const finish = () => {
    if (cancelled) return;
    cancelled = true;
    for (const t of timers) clearTimeout(t);
    resolveDone();
  };

  scenario.events.forEach(({ delayMs, event }, idx) => {
    const timer = setTimeout(() => {
      if (cancelled) return;
      try {
        handlers.onEvent(event);
      } catch (err) {
        handlers.onError(err instanceof Error ? err.message : String(err));
        finish();
        return;
      }
      if (idx === scenario.events.length - 1) {
        finish();
      }
    }, delayMs);
    timers.push(timer);
  });

  return {
    cancel: () => {
      if (cancelled) return;
      finish();
      handlers.onInterrupted?.();
    },
    done,
  };
}
