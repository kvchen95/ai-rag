import { useEffect, useMemo, useRef, useState } from 'react';
import type { CitationItem } from '../types';

interface Props {
  selectedCitation: CitationItem | null;
}

interface MockPage {
  page: number;
  title: string;
  lines: string[];
}

const MOCK_PAGES: MockPage[] = [
  {
    page: 47,
    title: '差旅管理办法-2025版 · 第47页',
    lines: [
      '第四章 住宿标准',
      '1. 国内出差住宿费标准：每人每天 500 元。',
      '2. 住宿费凭票据实报实销，不得超过标准上限。',
      '3. 因特殊情况超标需事前审批。',
    ],
  },
  {
    page: 48,
    title: '差旅管理办法-2025版 · 第48页',
    lines: [
      '4. 一线城市（北上广深）出差住宿费上限可按 600 元执行。',
      '5. 旺季价格波动需附说明并由部门负责人审批。',
      '6. 超出部分原则上自理。',
    ],
  },
  {
    page: 12,
    title: '差旅管理办法-2025版 · 第12页',
    lines: [
      '第一章 总则',
      '本办法适用于公司员工因公出差场景。',
      '差旅申请应在出发前通过 OA 提交。',
    ],
  },
];

function clamp(n: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, n));
}

export function PdfPreview({ selectedCitation }: Props) {
  const [pageInput, setPageInput] = useState('47');
  const [currentPage, setCurrentPage] = useState(47);
  const [loadedDoc, setLoadedDoc] = useState('policy/travel-cn.pdf');
  const pageRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!selectedCitation) return;
    setCurrentPage(selectedCitation.page_num);
    setPageInput(String(selectedCitation.page_num));
    setLoadedDoc(`${selectedCitation.doc_key}.pdf`);
  }, [selectedCitation]);

  const page = useMemo(
    () => MOCK_PAGES.find((p) => p.page === currentPage) ?? null,
    [currentPage],
  );

  const jumpToPage = () => {
    const n = Number(pageInput);
    if (!Number.isFinite(n)) return;
    setCurrentPage(clamp(Math.round(n), 1, 999));
  };

  const highlightBoxes = selectedCitation?.bbox ?? [];

  return (
    <div className="pdf-preview">
      <div className="pdf-toolbar">
        <div className="pdf-doc">
          <span className="label">PDF</span>
          <span className="pdf-doc-name" title={loadedDoc}>
            {loadedDoc}
          </span>
          <span className="pdf-badge">mock 占位</span>
        </div>
        <div className="pdf-jump">
          <label htmlFor="page-input">页码</label>
          <input
            id="page-input"
            value={pageInput}
            onChange={(e) => setPageInput(e.target.value)}
            inputMode="numeric"
            aria-label="跳转页码"
          />
          <button type="button" onClick={jumpToPage}>
            跳转
          </button>
        </div>
      </div>

      <div className="pdf-canvas-wrap">
        <div className="pdf-page" ref={pageRef}>
          <div className="pdf-page-header">
            <span>{page?.title ?? `第 ${currentPage} 页`}</span>
            <span className="pdf-page-num">p.{currentPage}</span>
          </div>

          <div className="pdf-page-body">
            {page ? (
              page.lines.map((line, i) => (
                <p key={i} className="pdf-line">
                  {line}
                </p>
              ))
            ) : (
              <p className="pdf-line muted">
                当前为 PDF 预览占位。已跳转到第 {currentPage} 页（mock 页面未内置内容）。
              </p>
            )}

            {selectedCitation && selectedCitation.page_num === currentPage && highlightBoxes.length > 0 && (
              <div className="bbox-layer" aria-label="引用高亮层">
                {highlightBoxes.map((b, i) => {
                  const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = b;
                  const left = clamp(x0, 0, 1) * 100;
                  const top = clamp(y0, 0, 1) * 100;
                  const width = clamp(x1 - x0, 0, 1) * 100;
                  const height = clamp(y1 - y0, 0, 1) * 100;
                  return (
                    <div
                      key={i}
                      className="bbox-highlight"
                      style={{ left: `${left}%`, top: `${top}%`, width: `${width}%`, height: `${height}%` }}
                      title={`引用[${selectedCitation.n}] 高亮占位`}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="pdf-footer">
        {selectedCitation ? (
          <div className="pdf-selection">
            <strong>已选引用 [{selectedCitation.n}]</strong>
            <div>{selectedCitation.doc_name}</div>
            <div>
              p.{selectedCitation.page_num} · {selectedCitation.chunk_id}
            </div>
            <div className="pdf-snippet">{selectedCitation.snippet}</div>
            {selectedCitation.bbox && selectedCitation.bbox.length > 0 ? (
              <div className="pdf-bbox-note">bbox: {JSON.stringify(selectedCitation.bbox)}</div>
            ) : (
              <div className="pdf-bbox-note muted">该引用无 bbox，F2 将降级展示页码+片段。</div>
            )}
          </div>
        ) : (
          <div className="pdf-selection muted">点击左侧引用角标可定位页码并预留高亮框。</div>
        )}
      </div>
    </div>
  );
}
