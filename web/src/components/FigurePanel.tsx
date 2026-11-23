// 图表切片面板：A2 的 `figures/{chunk_id}.webp` + 说明（引用的 snippet）。
//
// 三种出口都要有话说（卡片验收「无权限/样本缺失」）：
// - 200 → WebP + 说明；
// - 404 → 「该切片没有图」——不是错误，多数切片本来就是纯文字；
// - 401/403 → 「无权查看该切片图」（§11.1 闸二）。
import { useEffect, useRef, useState } from 'react';
import { loadChunkFigure, type FigureResult } from '../api';
import type { CitationItem } from '../types';

interface Props {
  citation: CitationItem;
  token: string;
}

export function FigurePanel({ citation, token }: Props) {
  const [result, setResult] = useState<FigureResult | null>(null);
  const [attempt, setAttempt] = useState(0);
  const urlRef = useRef<string | null>(null);

  useEffect(() => {
    if (!citation.chunk_id) {
      setResult({ state: 'missing' });
      return;
    }
    let cancelled = false;
    const controller = new AbortController();
    setResult(null);
    void loadChunkFigure(citation.chunk_id, token, controller.signal)
      .then((value) => {
        if (cancelled) {
          if (value.state === 'ok') URL.revokeObjectURL(value.url);
          return;
        }
        if (value.state === 'ok') urlRef.current = value.url;
        setResult(value);
      })
      .catch(() => {
        // AbortError：换了下一条引用，旧结果丢掉即可。
        if (!cancelled) setResult({ state: 'missing' });
      });

    return () => {
      cancelled = true;
      controller.abort();
      // objectURL 不 revoke 会一直占着内存（点几十条引用就是几十张图）。
      if (urlRef.current) {
        URL.revokeObjectURL(urlRef.current);
        urlRef.current = null;
      }
    };
  }, [citation.chunk_id, token, attempt]);

  if (!result) return <div className="figure-panel muted">切片图：查询中…</div>;

  if (result.state === 'ok') {
    return (
      <div className="figure-panel" data-testid="figure-panel">
        <div className="figure-title">图表切片（WebP）</div>
        <img className="figure-image" src={result.url} alt={`切片 ${citation.chunk_id} 的图解`} />
        {citation.snippet && <div className="figure-caption">{citation.snippet}</div>}
      </div>
    );
  }

  if (result.state === 'forbidden') {
    return (
      <div className="figure-panel figure-forbidden" data-testid="figure-forbidden">
        <span>401/403 · 无权查看该切片图（引用本身仍按页码定位）</span>
        <button type="button" className="ghost" onClick={() => setAttempt((n) => n + 1)}>
          重新获取
        </button>
      </div>
    );
  }

  if (result.state === 'error') {
    return (
      <div className="figure-panel figure-forbidden" data-testid="figure-error">
        <span>切片图加载失败：{result.error.message}</span>
        <button type="button" className="ghost" onClick={() => setAttempt((n) => n + 1)}>
          重新获取
        </button>
      </div>
    );
  }

  return (
    <div className="figure-panel muted" data-testid="figure-missing">
      该切片没有图表（纯文字切片）；以上片段就是它的全部内容。
    </div>
  );
}
