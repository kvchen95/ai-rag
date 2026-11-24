// 接口层：真实 SSE（POST /api/v1/qa/stream）+ 样本 PDF + 切片图。
//
// 为什么不用 EventSource：它只能 GET、不能带 Authorization 头。问答是 POST（附录 C.1），
// 所以这里用 `fetch` + ReadableStream 自己切帧（切帧逻辑在 sse.ts，纯函数可单测）。
//
// 错误口径（§11.3 + 卡片验收）：401/403/404/503 只按**状态码**映射成固定文案，
// 服务端响应体的任何一个字都不进界面——那里可能有堆栈、SQL、路径。响应体只用来
// 取 `error.code` 与 `request_id`（两个都是机器可读的白名单值）。

import { Observable, type Subscriber } from 'rxjs';

import { SseParser, type RawFrame } from './sse';
import type { ErrorKind, SseEvent, SseEventType, UiError } from './types';
import { SSE_EVENT_TYPES, USER_MESSAGES } from './types';

export const QA_STREAM_PATH = '/api/v1/qa/stream';
export const SAMPLES_PATH = '/api/v1/samples';
export const CHUNK_FIGURE_PATH = '/api/v1/chunks';

export interface QaRequest {
  question: string;
  /** 附录 C.1 的 `scope.doc_keys`；空 = 有权限的全部生效文档。 */
  docKeys?: string[];
  timeRange?: { from: number; to: number };
}

export interface QaStreamHandlers {
  onEvent: (event: SseEvent) => void;
  /** 畸形帧/未知事件：计数，不抛。 */
  onDropped: (count: number) => void;
}

/** 状态码 → 界面错误种类。顺序即优先级：先认 401/403，再认 4xx/5xx。 */
export function kindFromStatus(status: number): ErrorKind {
  if (status === 401) return 'auth_invalid';
  if (status === 403) return 'forbidden';
  if (status === 404) return 'not_found';
  if (status === 422) return 'invalid_request';
  if (status === 409) return 'unavailable';
  if (status === 429) return 'unavailable';
  if (status === 503) return 'unavailable';
  if (status === 504) return 'unavailable';
  if (status >= 500) return 'internal';
  return 'internal';
}

const KNOWN_CODES = new Set([
  'auth_invalid',
  'forbidden',
  'no_evidence',
  'low_confidence',
  'unverifiable',
  'model_timeout',
  'index_stale',
  'internal',
  'invalid_request',
  'not_found',
  'scan_in_progress',
  'samples_missing',
  'version_conflict',
]);

/** 只从响应体里摘 `code` 与 `request_id`；一个字的正文都不外泄。 */
export function pickErrorMeta(body: unknown): { code?: string; requestId?: string } {
  if (!body || typeof body !== 'object') return {};
  const err = (body as { error?: unknown }).error;
  if (!err || typeof err !== 'object') return {};
  const code = (err as { code?: unknown }).code;
  const requestId = (err as { request_id?: unknown }).request_id;
  return {
    code: typeof code === 'string' && KNOWN_CODES.has(code) ? code : undefined,
    requestId: typeof requestId === 'string' ? requestId.slice(0, 64) : undefined,
  };
}

export function buildError(status: number, meta: { code?: string; requestId?: string }): UiError {
  const kind = kindFromStatus(status);
  return {
    kind,
    message: USER_MESSAGES[kind],
    httpStatus: status,
    code: meta.code,
    requestId: meta.requestId,
  };
}

export class QaHttpError extends Error {
  readonly uiError: UiError;

  constructor(uiError: UiError) {
    super(uiError.message);
    this.name = 'QaHttpError';
    this.uiError = uiError;
  }
}

/**
 * 一帧 SSE → 一个业务事件。认不出的一律返回 null（上层计为 dropped）。
 * 事件名以 `event:` 行为准（附录 C.2 的六个），`data.type` 作为兜底——
 * 流被落盘再回放时 `event:` 行会丢，`data.type` 不会。
 */
export function toSseEvent(frame: RawFrame): SseEvent | null {
  if (!frame.data) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
  const data = parsed as Record<string, unknown>;
  const name = frame.event || (typeof data.type === 'string' ? data.type : '');
  if (!SSE_EVENT_TYPES.includes(name as SseEventType)) return null;
  // `type` 一律用事件名覆写：两个真相源打架时以帧头为准（它更接近线缆上的事实）。
  return { ...data, type: name } as SseEvent;
}

function authHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'text/event-stream',
  };
}

/**
 * 开一条问答流。调用方拿到的事件已经过白名单过滤；HTTP 层错误抛 `QaHttpError`。
 * `signal` 取消时抛 `AbortError`（调用方按「中断」处理）。
 */
export async function streamQa(
  request: QaRequest,
  token: string,
  handlers: QaStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const body: Record<string, unknown> = { question: request.question };
  if (request.docKeys && request.docKeys.length > 0) {
    body.scope = { doc_keys: request.docKeys };
  }
  if (request.timeRange) {
    body.time_range = { from: request.timeRange.from, to: request.timeRange.to };
  }

  const response = await fetch(QA_STREAM_PATH, {
    method: 'POST',
    headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
    // 不要把 `Accept-Encoding` 交给浏览器默认以外的值：SSE 被压缩缓冲会变成「一次性吐出」。
    cache: 'no-store',
  });

  if (!response.ok) {
    const meta = await readErrorMeta(response);
    throw new QaHttpError(buildError(response.status, meta));
  }
  if (!response.body) {
    throw new QaHttpError({
      kind: 'protocol',
      message: USER_MESSAGES.protocol,
      httpStatus: response.status,
    });
  }

  const parser = new SseParser();
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const frame of parser.feed(decoder.decode(value, { stream: true }))) {
        const event = toSseEvent(frame);
        if (!event) {
          handlers.onDropped(1);
          continue;
        }
        handlers.onEvent(event);
      }
    }
    const tailFrames = [...parser.flush(), ...parser.feed(decoder.decode())];
    for (const frame of tailFrames) {
      const event = toSseEvent(frame);
      if (!event) {
        handlers.onDropped(1);
        continue;
      }
      handlers.onEvent(event);
    }
    if (parser.dropped > 0) handlers.onDropped(parser.dropped);
  } finally {
    reader.releaseLock();
  }
}

/**
 * `streamQa` 的 Observable 版本（详细设计 §10.5.2）。
 *
 * 为什么不直接把 `streamQa` 改成 Observable：它已经有调用方和测试，改动面越大
 * 回归风险越高。这里加一层适配，让新代码用流、旧代码继续用回调，两条路都留着
 * 直到 §10.5 的增量改造把调用方迁完。
 *
 * 三条语义与回调版一致，但由 Observable 的契约保证而不是靠调用方记得：
 * - **取消**：`unsubscribe()` 会 abort 底层 fetch，不必自己管 AbortController；
 * - **完成**：流结束时 `complete()`，不需要额外的 `onDone` 回调；
 * - **错误**：HTTP 层错误走 `error()`，调用方在 `subscribe` 的错误回调里处理。
 *
 * `signal` 参数保留：调用方若已有 AbortController（比如 hook 里要同时中断多条），
 * 仍然可以传进来，两条取消路径都生效（谁先触发都行）。
 */
export function streamQa$(
  request: QaRequest,
  token: string,
  options: { signal?: AbortSignal } = {},
): Observable<SseEvent> {
  return new Observable<SseEvent>((subscriber: Subscriber<SseEvent>) => {
    // 内部自己的 controller：unsubscribe 时用它 abort，与外部 signal 互不干扰。
    const controller = new AbortController();
    const onExternalAbort = () => controller.abort();
    if (options.signal) {
      if (options.signal.aborted) controller.abort();
      else options.signal.addEventListener('abort', onExternalAbort, { once: true });
    }

    let dropped = 0;
    streamQa(
      request,
      token,
      {
        onEvent: (event) => subscriber.next(event),
        onDropped: (count) => {
          dropped += count;
        },
      },
      controller.signal,
    )
      .then(() => {
        if (dropped > 0) {
          // 丢弃计数只做记录，不作为错误：畸形帧不该让整条流失败（§10.4）。
          console.warn(`[sse] dropped ${dropped} malformed frame(s)`);
        }
        subscriber.complete();
      })
      .catch((error: unknown) => {
        if ((error as { name?: string }).name === 'AbortError') {
          // 主动取消不是错误：完成而不是 error，避免调用方把它当故障上报。
          subscriber.complete();
          return;
        }
        subscriber.error(error);
      })
      .finally(() => {
        options.signal?.removeEventListener('abort', onExternalAbort);
      });

    // 返回的清理函数在 unsubscribe 时执行：这是"收尾只有一个出口"的落点。
    return () => {
      options.signal?.removeEventListener('abort', onExternalAbort);
      controller.abort();
    };
  });
}

async function readErrorMeta(response: Response): Promise<{ code?: string; requestId?: string }> {
  // 只读一小段：错误体本来就是 JSON，限制长度是为了不把异常表单之类的东西读进来。
  try {
    const text = await response.text();
    if (!text || text.length > 4096) return {};
    return pickErrorMeta(JSON.parse(text));
  } catch {
    return {};
  }
}

function encodeDocKey(docKey: string): string {
  // doc_key 自带 `/`（`policy/travel-cn`），逐段编码后保留分隔符。
  return docKey
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/');
}

/** 附录 C.1 的样本直出接口。 */
export function samplePdfUrl(docKey: string): string {
  return `${SAMPLES_PATH}/${encodeDocKey(docKey)}.pdf`;
}

/** 切片图（A2 的 `figures/{chunk_id}.webp`）。图不存在时返回 404，界面走「无切片图」降级。 */
export function chunkFigureUrl(chunkId: string): string {
  return `${CHUNK_FIGURE_PATH}/${encodeURIComponent(chunkId)}/figure`;
}

export type FigureResult =
  | { state: 'ok'; url: string }
  | { state: 'missing' }
  | { state: 'forbidden' }
  | { state: 'error'; error: UiError };

/** 拉一张切片图，成功时返回可回收的 objectURL（组件负责 revoke）。 */
export async function loadChunkFigure(
  chunkId: string,
  token: string,
  signal?: AbortSignal,
): Promise<FigureResult> {
  let response: Response;
  try {
    response = await fetch(chunkFigureUrl(chunkId), {
      headers: { Authorization: `Bearer ${token}` },
      signal,
      cache: 'no-store',
    });
  } catch (error) {
    if ((error as { name?: string }).name === 'AbortError') throw error;
    return { state: 'error', error: { kind: 'network', message: USER_MESSAGES.network } };
  }
  if (response.status === 404) return { state: 'missing' };
  if (response.status === 401 || response.status === 403) return { state: 'forbidden' };
  if (!response.ok) {
    const meta = await readErrorMeta(response);
    return { state: 'error', error: buildError(response.status, meta) };
  }
  const blob = await response.blob();
  return { state: 'ok', url: URL.createObjectURL(blob) };
}
