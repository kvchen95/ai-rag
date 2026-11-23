// SSE / UI 契约（对齐 详细设计.md §10.4、附录 C.1/C.2）
// 顺序：step* → (token* | clarify | fallback) → citations? → done
// 前端不得假设一定有 token（拒答路径没有逐字）。

export type SsePhase = 'retrieval' | 'rerank' | 'generate' | 'verify' | string;

export interface StepEvent {
  type: 'step';
  hop: number;
  phase: SsePhase;
  elapsed_ms: number;
  note?: string;
}

/** clarify.options[]：label 给人看，value 是点选后回填的东西（B4 里 = doc_key）。 */
export interface ClarifyOption {
  label: string;
  value: string;
  doc_key?: string;
  /** 未来的附加回填参数（时间范围等）：前端不认识就原样带给下一次请求。 */
  [key: string]: unknown;
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

/** 归一化 bbox：`[[x0,y0,x1,y1], ...]`，坐标落在 0..1。 */
export type Bbox = number[][];

export interface CitationItem {
  n: number;
  doc_key: string;
  doc_name: string;
  page_num: number;
  bbox?: Bbox;
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

/**
 * done.verdict 六态：门控五态 + `unverifiable`（门控放行但校验不过）。
 * 系统档直接用错误码本身（`model_timeout` / `internal` / `index_stale`），
 * 这样「模型超时」与「代码炸了」在界面上分得开（§11.3 的告警口径不同）。
 */
export type Verdict =
  | 'pass'
  | 'clarify'
  | 'reject_none'
  | 'reject_low'
  | 'partial'
  | 'unverifiable'
  | 'model_timeout'
  | 'internal'
  | 'index_stale'
  | (string & {});

export interface DoneEvent {
  type: 'done';
  latency_ms: number;
  hop_count: number;
  verdict: Verdict;
  code?: string;
  uncovered?: string[];
}

export type SseEvent =
  | StepEvent
  | ClarifyEvent
  | TokenEvent
  | CitationsEvent
  | FallbackEvent
  | DoneEvent;

export const SSE_EVENT_TYPES = [
  'step',
  'clarify',
  'token',
  'citations',
  'fallback',
  'done',
] as const;

export type SseEventType = (typeof SSE_EVENT_TYPES)[number];

export type UiStatus = 'idle' | 'retrieving' | 'streaming' | 'done' | 'failed';

/**
 * 界面上能看到的错误种类。只认这一组：401/403/404/503 只按状态码映射成固定文案，
 * 服务端响应体里的任何正文（堆栈、SQL、路径）都不进这里（§11.3 的用户文案表）。
 */
export type ErrorKind =
  | 'auth_invalid'
  | 'forbidden'
  | 'not_found'
  | 'invalid_request'
  | 'unavailable'
  | 'internal'
  | 'network'
  | 'interrupted'
  | 'protocol';

export interface UiError {
  kind: ErrorKind;
  /** 固定文案，来自本文件 USER_MESSAGES，不来自服务端正文。 */
  message: string;
  httpStatus?: number;
  /** 服务端错误码（白名单映射用，不直接展示正文）。 */
  code?: string;
  /** 排障用：request_id 是随机串，可以给运维看。 */
  requestId?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  status?: 'pending' | 'streaming' | 'done' | 'interrupted' | 'error';
  citations?: CitationItem[];
  fallback?: FallbackEvent;
  clarify?: ClarifyEvent;
  verdict?: Verdict;
  /** 服务端降级码（fallback.code）：一个说「为什么没答案」，一个说「最后出口是哪档」。 */
  code?: string;
  /** clarify 点选后填进来的范围说明（只用于展示）。 */
  scopeNote?: string;
}

export type DataSource = 'live' | 'mock';

export interface StreamState {
  status: UiStatus;
  messages: ChatMessage[];
  activeMessageId: string | null;
  activeRunId: string | null;
  lastStep: StepEvent | null;
  lastDone: DoneEvent | null;
  error: UiError | null;
  selectedCitation: CitationItem | null;
  source: DataSource;
  /** 被丢弃的畸形/未知帧数（只用于排障，不参与业务判断）。 */
  droppedFrames: number;
}

/** §11.3 的用户文案：只写这些，正文永远不来自服务端响应体。 */
export const USER_MESSAGES: Record<ErrorKind, string> = {
  auth_invalid: '登录已失效，请重新登录',
  forbidden: '无权查看该文档',
  not_found: '未找到该文档或样本文件缺失',
  invalid_request: '请求格式不正确',
  unavailable: '服务暂时不可用，请稍后重试',
  internal: '服务出错了，请稍后重试',
  network: '网络中断，可重试',
  interrupted: '已中断，可重试',
  protocol: '服务返回了无法识别的事件，已忽略',
};
