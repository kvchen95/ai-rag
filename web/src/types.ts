// SSE event contract aligned to 详细设计.md 附录C.2
// Order: step* -> (token* | clarify | fallback) -> citations? -> done
// Frontend must not assume token exists.

export type SsePhase = 'retrieval' | 'rerank' | 'generate' | 'verify' | string;

export interface StepEvent {
  type: 'step';
  hop: number;
  phase: SsePhase;
  elapsed_ms: number;
}

export interface ClarifyOption {
  label: string;
  value: string;
}

export interface ClarifyEvent {
  type: 'clarify';
  question: string;
  options: ClarifyOption[];
}

export interface TokenEvent {
  type: 'token';
  delta: string;
}

export interface CitationItem {
  n: number;
  doc_key: string;
  doc_name: string;
  page_num: number;
  bbox?: number[][]; // normalized [x0,y0,x1,y1][]
  snippet: string;
  chunk_id: string;
}

export interface CitationsEvent {
  type: 'citations';
  items: CitationItem[];
}

export interface FallbackDoc {
  doc_key: string;
  doc_name: string;
  page_num?: number;
  snippet?: string;
}

export interface FallbackEvent {
  type: 'fallback';
  code: string;
  message: string;
  docs: FallbackDoc[];
}

export type Verdict = 'pass' | 'clarify' | 'reject_none' | 'reject_low' | 'partial' | string;

export interface DoneEvent {
  type: 'done';
  latency_ms: number;
  hop_count: number;
  verdict: Verdict;
}

export type SseEvent =
  | StepEvent
  | ClarifyEvent
  | TokenEvent
  | CitationsEvent
  | FallbackEvent
  | DoneEvent;

export type UiStatus = 'idle' | 'retrieving' | 'streaming' | 'done' | 'failed';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  status?: 'pending' | 'streaming' | 'done' | 'interrupted' | 'error';
  citations?: CitationItem[];
  fallback?: FallbackEvent;
  clarify?: ClarifyEvent;
}

export interface StreamState {
  status: UiStatus;
  messages: ChatMessage[];
  activeMessageId: string | null;
  activeRunId: string | null;
  lastStep: StepEvent | null;
  lastDone: DoneEvent | null;
  error: string | null;
  selectedCitation: CitationItem | null;
}
