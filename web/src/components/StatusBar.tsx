import type { CitationItem, UiStatus } from '../types';

interface Props {
  status: UiStatus;
  lastStepPhase?: string;
  lastStepHop?: number;
  latencyMs?: number;
  hopCount?: number;
  verdict?: string;
  error: string | null;
  selectedCitation: CitationItem | null;
}

const statusLabel: Record<UiStatus, string> = {
  idle: '空闲',
  retrieving: '检索中',
  streaming: '逐字生成中',
  done: '结束',
  failed: '失败',
};

export function StatusBar(props: Props) {
  const { status, lastStepPhase, lastStepHop, latencyMs, hopCount, verdict, error, selectedCitation } =
    props;
  return (
    <div className="status-bar" aria-live="polite">
      <span className={`pill pill-${status}`}>状态：{statusLabel[status]}</span>
      {lastStepPhase && (
        <span className="pill">
          phase: {lastStepPhase}
          {typeof lastStepHop === 'number' ? ` / hop ${lastStepHop}` : ''}
        </span>
      )}
      {typeof latencyMs === 'number' && <span className="pill">latency: {latencyMs}ms</span>}
      {typeof hopCount === 'number' && <span className="pill">hops: {hopCount}</span>}
      {verdict && <span className="pill">verdict: {verdict}</span>}
      {selectedCitation && (
        <span className="pill pill-accent">
          引用[{selectedCitation.n}] p.{selectedCitation.page_num}
        </span>
      )}
      {error && <span className="pill pill-error">错误: {error}</span>}
    </div>
  );
}
