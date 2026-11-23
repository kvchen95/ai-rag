import type { DataSource, UiError, UiStatus } from '../types';

interface Props {
  status: UiStatus;
  source: DataSource;
  lastStepPhase?: string;
  lastStepHop?: number;
  latencyMs?: number;
  hopCount?: number;
  verdict?: string;
  error: UiError | null;
  droppedFrames: number;
  /** 本轮回答的角标可点回率（由 citations.ts 的 auditCitations 算出来）。 */
  citationRate?: { resolvable: number; referenced: number; rate: number };
}

const statusLabel: Record<UiStatus, string> = {
  idle: '空闲',
  retrieving: '检索中',
  streaming: '逐字生成中',
  done: '结束',
  failed: '失败',
};

export function StatusBar(props: Props) {
  const {
    status,
    source,
    lastStepPhase,
    lastStepHop,
    latencyMs,
    hopCount,
    verdict,
    error,
    droppedFrames,
    citationRate,
  } = props;
  return (
    <div className="status-bar" aria-live="polite" data-testid="status-bar">
      <span className={`pill pill-${status}`} data-testid="status-pill">
        状态：{statusLabel[status]}
      </span>
      <span className="pill">{source === 'live' ? '数据源：真实 SSE' : '数据源：演示回放'}</span>
      {lastStepPhase && (
        <span className="pill">
          phase: {lastStepPhase}
          {typeof lastStepHop === 'number' ? ` / hop ${lastStepHop}` : ''}
        </span>
      )}
      {typeof latencyMs === 'number' && <span className="pill">latency: {latencyMs}ms</span>}
      {typeof hopCount === 'number' && <span className="pill">hops: {hopCount}</span>}
      {verdict && <span className="pill">verdict: {verdict}</span>}
      {citationRate && (
        <span className="pill" data-testid="citation-rate" title="正文里引了 [n] 且 citations 里能找到的占比">
          角标可点回 {citationRate.resolvable}/{citationRate.referenced}（
          {Math.round(citationRate.rate * 100)}%）
        </span>
      )}
      {droppedFrames > 0 && (
        <span className="pill pill-warn" data-testid="dropped-frames">
          已忽略畸形帧 {droppedFrames}
        </span>
      )}
      {error && (
        <span className="pill pill-error" data-testid="error-pill" data-error-kind={error.kind}>
          错误：{error.message}
          {error.httpStatus ? `（HTTP ${error.httpStatus}）` : ''}
          {error.requestId ? `｜request_id ${error.requestId}` : ''}
        </span>
      )}
    </div>
  );
}
