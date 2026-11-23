import { useCallback, useEffect, useReducer, useRef } from 'react';
import { initialState, reduce } from './state';
import { playMockSse, scenarios } from './mockSse';
import { QaHttpError, streamQa } from './api';
import { TokenCoalescer } from './coalesce';
import type { CitationItem, DataSource, SseEvent, UiError } from './types';
import { USER_MESSAGES } from './types';

let runSeq = 0;
function nextRunId(): string {
  runSeq += 1;
  return `run_${Date.now().toString(36)}_${runSeq}`;
}

export interface SendOptions {
  /** clarify 回填：把用户点选的范围带进下一次请求（附录 C.1 的 scope.doc_keys）。 */
  docKeys?: string[];
  /** 界面上写一句「已按选择收窄范围」——用户点完按钮得看见自己点了什么。 */
  scopeNote?: string;
  /** 演示回放用哪个场景（mock 模式）；live 模式忽略。 */
  scenarioId?: string;
}

interface UseChatOptions {
  /** 开发期凭证（Bearer JWT）。live 模式下必需；mock 模式不用。 */
  token: string;
}

/**
 * 问答会话 hook。
 *
 * 两条数据源：
 * - `live`：真实 `POST /api/v1/qa/stream`（SSE over fetch）；
 * - `mock`：F1 的合成回放，只用于没有后端时演示事件顺序（真实路径的证据不靠它）。
 *
 * 合帧器（`TokenCoalescer`）在这里而不是组件里：React 层没有 rAF，漏写 cancel 的风险
 * 从结构上消失；任何非 token 事件落地前先 flush，保证「正文先于引用」的顺序。
 */
export function useChat({ token }: UseChatOptions) {
  const [state, dispatch] = useReducer(reduce, initialState);
  const abortRef = useRef<AbortController | null>(null);
  const coalescerRef = useRef<TokenCoalescer | null>(null);

  const getCoalescer = useCallback((runId: string) => {
    if (!coalescerRef.current) {
      coalescerRef.current = new TokenCoalescer((delta) => {
        dispatch({ type: 'sse_event', runId, event: { type: 'token', delta } });
      });
    }
    return coalescerRef.current;
  }, []);

  const resetCoalescer = useCallback(() => {
    coalescerRef.current?.dispose();
    coalescerRef.current = null;
  }, []);

  const dispatchEvent = useCallback(
    (runId: string, event: SseEvent) => {
      if (event.type === 'token') {
        getCoalescer(runId).push(event.delta);
        return;
      }
      // 非 token 事件必须先落地缓冲：否则 citations/done 会先于正文渲染。
      getCoalescer(runId).flush();
      dispatch({ type: 'sse_event', runId, event });
    },
    [getCoalescer],
  );

  const stop = useCallback(() => {
    resetCoalescer();
    abortRef.current?.abort();
    abortRef.current = null;
  }, [resetCoalescer]);

  const send = useCallback(
    (question: string, options: SendOptions = {}) => {
      const q = question.trim();
      if (!q) return;
      // 打断上一条（未完成的那条会被中断态收尾）。
      stop();
      resetCoalescer();

      const runId = nextRunId();
      const messageId = `a-${runId}`;
      dispatch({
        type: 'user_submit',
        question: q,
        runId,
        messageId,
        scopeNote: options.scopeNote,
      });

      if (state.source === 'mock') {
        const handle = playMockSse(options.scenarioId ?? 'normal', {
          onEvent: (event: SseEvent) => dispatchEvent(runId, event),
          onError: (error: UiError) => {
            resetCoalescer();
            dispatch({ type: 'sse_error', runId, error });
          },
          onInterrupted: () => {
            resetCoalescer();
            dispatch({ type: 'sse_interrupted', runId });
          },
        });
        abortRef.current = null;
        void handle.done.finally(() => resetCoalescer());
        return;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      void streamQa(
        { question: q, docKeys: options.docKeys },
        token,
        {
          onEvent: (event) => dispatchEvent(runId, event),
          onDropped: (count) => dispatch({ type: 'sse_dropped', runId, count }),
        },
        controller.signal,
      )
        .catch((error: unknown) => {
          // 中断不当作失败：AbortError 走「已中断，可重试」。
          if ((error as { name?: string }).name === 'AbortError') {
            resetCoalescer();
            dispatch({ type: 'sse_interrupted', runId });
            return;
          }
          // 业务错误只带固定文案；QaHttpError 里没有服务端正文。
          const uiError: UiError =
            error instanceof QaHttpError
              ? error.uiError
              : { kind: 'network', message: USER_MESSAGES.network };
          coalescerRef.current?.flush();
          resetCoalescer();
          dispatch({ type: 'sse_error', runId, error: uiError });
        })
        .finally(() => {
          if (abortRef.current === controller) abortRef.current = null;
        });
      // 兜底：做完最后一帧后把残留缓冲落地（服务端可能不发 done）。
      void Promise.resolve().then(() => coalescerRef.current?.flush());
    },
    [dispatchEvent, resetCoalescer, state.source, stop, token],
  );

  const selectCitation = useCallback((citation: CitationItem | null) => {
    dispatch({ type: 'select_citation', citation });
  }, []);

  const setSource = useCallback((source: DataSource) => {
    dispatch({ type: 'set_source', source });
  }, []);

  const reset = useCallback(() => {
    stop();
    dispatch({ type: 'reset' });
  }, [stop]);

  useEffect(() => () => {
    abortRef.current?.abort();
    coalescerRef.current?.dispose();
  }, []);

  return {
    state,
    send,
    stop,
    reset,
    selectCitation,
    setSource,
    scenarios,
  };
}
