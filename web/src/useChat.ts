import { useCallback, useReducer, useRef } from 'react';
import { initialState, reduce } from './state';
import { playMockSse, scenarios } from './mockSse';
import type { CitationItem, SseEvent } from './types';

let runSeq = 0;
function nextRunId(): string {
  runSeq += 1;
  return `run_${Date.now().toString(36)}_${runSeq}`;
}

export function useChat() {
  const [state, dispatch] = useReducer(reduce, initialState);
  const cancelRef = useRef<null | (() => void)>(null);

  const stop = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
  }, []);

  const send = useCallback((question: string, scenarioId: string) => {
    const q = question.trim();
    if (!q) return;
    // Interrupt previous run if any.
    cancelRef.current?.();
    cancelRef.current = null;

    const runId = nextRunId();
    const messageId = `a-${runId}`;
    dispatch({ type: 'user_submit', question: q, runId, messageId });

    const handle = playMockSse(scenarioId, {
      onEvent: (event: SseEvent) => {
        dispatch({ type: 'sse_event', runId, event });
      },
      onError: (error: string) => {
        dispatch({ type: 'sse_error', runId, error });
      },
      onInterrupted: () => {
        dispatch({ type: 'sse_interrupted', runId });
      },
    });
    cancelRef.current = handle.cancel;
    void handle.done.finally(() => {
      if (cancelRef.current === handle.cancel) cancelRef.current = null;
    });
  }, []);

  const selectCitation = useCallback((citation: CitationItem | null) => {
    dispatch({ type: 'select_citation', citation });
  }, []);

  const reset = useCallback(() => {
    cancelRef.current?.();
    cancelRef.current = null;
    dispatch({ type: 'reset' });
  }, []);

  return {
    state,
    send,
    stop,
    reset,
    selectCitation,
    scenarios,
  };
}
