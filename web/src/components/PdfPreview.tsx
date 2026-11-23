import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import { samplePdfUrl } from '../api';
import { fallbackExcerpt, rectsFromBbox } from '../bbox';
import type { CitationItem, UiError } from '../types';
import { FigurePanel } from './FigurePanel';

type PdfModule = typeof import('../pdf');

interface Props {
  selectedCitation: CitationItem | null;
  token: string;
}

type DocState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; doc: PDFDocumentProxy; docKey: string }
  | { phase: 'error'; error: UiError; docKey: string };

export function PdfPreview({ selectedCitation, token }: Props) {
  const [docState, setDocState] = useState<DocState>({ phase: 'idle' });
  const [currentPage, setCurrentPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [width, setWidth] = useState(720);
  const [rendering, setRendering] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const pdfModuleRef = useRef<PdfModule | null>(null);

  // PDF.js 只在第一次点角标时才下载（主包 525 kB 里有一大半是它）。
  const getPdfModule = useCallback(async (): Promise<PdfModule> => {
    if (!pdfModuleRef.current) pdfModuleRef.current = await import('../pdf');
    return pdfModuleRef.current;
  }, []);

  // 容器宽度变化（窗口缩放、分栏拖动）要重渲染，否则画布被拉伸变形，高亮框跟着错位。
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next && next > 200) setWidth(Math.floor(next) - 24);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // 点角标：换文档就加载，同文档就直接翻页。
  useEffect(() => {
    if (!selectedCitation) return;
    const page = Math.max(1, Math.round(selectedCitation.page_num));
    setCurrentPage(page);
    setPageInput(String(page));

    const docKey = selectedCitation.doc_key;
    setDocState((prev) => {
      if (prev.phase === 'ready' && prev.docKey === docKey) return prev;
      return { phase: 'loading' };
    });
  }, [selectedCitation]);

  // 加载 PDF（同一份文档只加载一次；gkkey 变了才重新拉）。
  useEffect(() => {
    const docKey = selectedCitation?.doc_key;
    if (!docKey) return;
    if (docState.phase === 'ready' && docState.docKey === docKey) return;
    if (docState.phase === 'error' && docState.docKey === docKey) return;

    const controller = new AbortController();
    let cancelled = false;
    void (async () => {
      const mod = await getPdfModule();
      const result = await mod.openPdf({ url: samplePdfUrl(docKey), token, signal: controller.signal });
      if (cancelled) {
        if (result.ok) void result.doc.destroy();
        return;
      }
      setDocState(
        result.ok
          ? { phase: 'ready', doc: result.doc, docKey }
          : { phase: 'error', error: result.error, docKey },
      );
      // 开发期把已加载的文档挂到 window 上：E2E 排障时可以在 console 里直接
      // re-render 一页到临时 canvas，判断"是渲染没画"还是"画在别处"。
      if (import.meta.env.DEV && result.ok) {
        (window as unknown as { __f2PdfDoc?: unknown }).__f2PdfDoc = result.doc;
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [docState, getPdfModule, selectedCitation?.doc_key, token]);

  const docComponent = docState.phase === 'ready' ? docState.doc : null;

  const renderChainRef = useRef<Promise<unknown>>(Promise.resolve());

  useEffect(() => {
    if (!docComponent) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const controller = new AbortController();
    let cancelled = false;
    setRendering(true);
    // 串行化：上一次渲染必须先结束（被取消也算结束），否则同一个 canvas 上会同时
    // 存在两个 render()，pdf.js 直接抛错并把页面留成空白。
    const run = renderChainRef.current
      .catch(() => undefined)
      .then(() => getPdfModule())
      .then((mod) =>
        mod.renderPageToCanvas(docComponent, currentPage, canvas, Math.max(280, width), {
          signal: controller.signal,
        }),
      );
    renderChainRef.current = run;
    void run
      .then(() => {
        if (!cancelled) setRenderError(null);
      })
      .catch((error: unknown) => {
        // 翻页时上一次渲染会被主动取消，那不是错误。
        const name = (error as { name?: string }).name ?? '';
        if (cancelled || name === 'RenderingCancelledException') return;
        // 其余失败不该把已加载的文档打回错误态（还能翻页），但**必须说出来**：
        // 静默吞掉的表现是"页面一片白"，看起来像渲染成功、只是没内容。
        setRenderError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (!cancelled) setRendering(false);
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [docComponent, currentPage, getPdfModule, width]);

  const jump = useCallback(() => {
    const n = Number(pageInput);
    if (!Number.isFinite(n)) return;
    const maxPage = docComponent?.numPages ?? 1;
    setCurrentPage(Math.max(1, Math.min(maxPage, Math.round(n))));
  }, [docComponent, pageInput]);

  const highlight = useMemo(() => rectsFromBbox(selectedCitation?.bbox), [selectedCitation]);
  const showHighlight = Boolean(
    selectedCitation && highlight.rects.length > 0 && selectedCitation.page_num === currentPage,
  );
  const excerpt = fallbackExcerpt(selectedCitation?.snippet);

  const docLabel = selectedCitation ? `${selectedCitation.doc_key}.pdf` : '（未选择引用）';
  const pageCount = docComponent?.numPages ?? 0;

  return (
    <div className="pdf-preview">
      <div className="pdf-toolbar">
        <div className="pdf-doc">
          <span className="label">PDF</span>
          <span className="pdf-doc-name" title={docLabel} data-testid="pdf-doc-name">
            {docLabel}
          </span>
          {docComponent && (
            <span className="pdf-badge">
              {pageCount} 页
            </span>
          )}
        </div>
        <div className="pdf-jump">
          <label htmlFor="page-input">页码</label>
          <input
            id="page-input"
            value={pageInput}
            onChange={(e) => setPageInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') jump();
            }}
            inputMode="numeric"
            aria-label="跳转页码"
            data-testid="page-input"
          />
          <button type="button" onClick={jump} data-testid="page-jump">
            跳转
          </button>
        </div>
      </div>

      {docState.phase === 'error' && (
        <div className="pdf-error" role="alert" data-testid="pdf-error">
          <div className="pdf-error-title">
            {docState.error.httpStatus ? `${docState.error.httpStatus} · ` : ''}
            {docState.error.message}
          </div>
          <div className="pdf-error-hint">
            文档：{docState.docKey}。界面不展示服务端返回的任何正文（避免泄漏内部细节）。
          </div>
        </div>
      )}

      <div className="pdf-canvas-wrap" ref={wrapRef}>
        {docState.phase === 'idle' && (
          <div className="pdf-placeholder">
            <p>点击左侧引用角标 <strong>[n]</strong>：跳到对应页并高亮引用区域。</p>
            <p className="muted">没有角标的回答不会出现在这里——这里只回答「这句话的原文在哪」。</p>
          </div>
        )}
        {docState.phase === 'loading' && (
          <div className="pdf-placeholder" data-testid="pdf-loading">
            加载 PDF 中…（{selectedCitation?.doc_key}）
          </div>
        )}
        {docState.phase === 'ready' && (
          <div className="pdf-page" data-testid="pdf-page">
            <div className="pdf-page-head">
              <span className="pdf-page-num" data-testid="pdf-page-num">
                p.{currentPage} / {pageCount}
              </span>
              {rendering && <span className="pdf-rendering">渲染中…</span>}
            </div>
            <div className="pdf-page-frame">
              <canvas ref={canvasRef} data-testid="pdf-canvas" aria-label={`PDF 第 ${currentPage} 页`} />
              {showHighlight && (
                <div className="bbox-layer" aria-label="引用高亮层">
                  {highlight.rects.map((rect, i) => (
                    <div
                      key={i}
                      className="bbox-highlight"
                      data-testid="bbox-highlight"
                      style={{
                        left: `${rect.left}%`,
                        top: `${rect.top}%`,
                        width: `${rect.width}%`,
                        height: `${rect.height}%`,
                      }}
                      title={`引用[${selectedCitation?.n}]`}
                    />
                  ))}
                </div>
              )}
            </div>
            {renderError && (
              <div className="pdf-error" role="alert" data-testid="pdf-render-error">
                这一页渲染失败：{renderError}
              </div>
            )}
            {selectedCitation && highlight.rects.length === 0 && (
              <div className="bbox-degraded" data-testid="bbox-degraded">
                <div className="bbox-degraded-title">
                  {highlight.hasBbox
                    ? `引用[${selectedCitation.n}] 的坐标不可用（丢弃 ${highlight.dropped} 个框），按页码定位`
                    : `引用[${selectedCitation.n}] 没有坐标（bbox 缺失），按页码定位`}
                </div>
                <div className="bbox-degraded-body">
                  第 {selectedCitation.page_num} 页 · {selectedCitation.doc_name}
                </div>
                {excerpt && <blockquote className="bbox-degraded-snippet">{excerpt}</blockquote>}
              </div>
            )}
            {selectedCitation && highlight.rects.length > 0 && selectedCitation.page_num !== currentPage && (
              <div className="bbox-degraded" data-testid="bbox-off-page">
                引用[{selectedCitation.n}] 在 p.{selectedCitation.page_num}：点角标可跳回。
              </div>
            )}
          </div>
        )}
      </div>

      <div className="pdf-footer">
        {selectedCitation ? (
          <div className="pdf-selection" data-testid="pdf-selection">
            <strong>已选引用 [{selectedCitation.n}]</strong>
            <div>{selectedCitation.doc_name}</div>
            <div className="muted">
              p.{selectedCitation.page_num} · {selectedCitation.chunk_id}
            </div>
            {excerpt && <div className="pdf-snippet">{excerpt}</div>}
            {
              /* 图表切片：有 WebP 就展示切片 + 说明；没有就明确说「无切片图」。 */
            }
            <FigurePanel citation={selectedCitation} token={token} />
          </div>
        ) : (
          <div className="pdf-selection muted">引用角标 → 页码 → 高亮框；坐标缺失时降级为「页码 + 原文片段」。</div>
        )}
      </div>
    </div>
  );
}
