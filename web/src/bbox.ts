// 归一化 bbox → 覆盖层几何。
//
// 口径（附录 C.2）：`[[x0,y0,x1,y1], ...]`，坐标在 **0..1**，相对页面。
// 「画错框比不画更糟」：坐标越界（比如解析器错把像素坐标塞进来）时**不画**，
// 让界面走「页码 + 片段」降级；一个覆盖半页的错误高亮会让人以为引用就是那一大段。

import type { Bbox } from './types';

export interface RectPercent {
  /** 百分比（0..100），直接喂给 CSS 的 left/top/width/height。 */
  left: number;
  top: number;
  width: number;
  height: number;
}

const EPS = 1e-6;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** 一个 `[x0,y0,x1,y1]` 是否可用：四个有限数、落在 0..1、宽高为正。 */
export function rectFromBox(box: unknown): RectPercent | null {
  if (!Array.isArray(box) || box.length !== 4) return null;
  const [x0, y0, x1, y1] = box;
  if (!isFiniteNumber(x0) || !isFiniteNumber(y0) || !isFiniteNumber(x1) || !isFiniteNumber(y1)) {
    return null;
  }
  for (const v of [x0, y0, x1, y1]) {
    if (v < -EPS || v > 1 + EPS) return null;
  }
  const left = Math.min(x0, x1);
  const top = Math.min(y0, y1);
  const width = Math.abs(x1 - x0);
  const height = Math.abs(y1 - y0);
  if (width <= EPS || height <= EPS) return null;
  return {
    left: clamp01(left) * 100,
    top: clamp01(top) * 100,
    width: clamp01(width) * 100,
    height: clamp01(height) * 100,
  };
}

function clamp01(n: number): number {
  return Math.max(0, Math.min(1, n));
}

export interface RectSet {
  rects: RectPercent[];
  /** 被判为不可用而丢掉的框数（用来在界面上说明「有坐标但画不出来」）。 */
  dropped: number;
  /** 有没有拿到坐标（空/缺失都算没有）。 */
  hasBbox: boolean;
}

/**
 * 把 citations 里的 bbox 变成可渲染的矩形集合。
 * - 缺失/空数组 → `hasBbox=false`（降级：页码 + 片段）。
 * - 存在但全不可用 → `hasBbox=true`、`rects=[]`、`dropped>0`（说明「坐标格式不对」）。
 */
export function rectsFromBbox(bbox: Bbox | undefined | null): RectSet {
  if (!bbox || (Array.isArray(bbox) && bbox.length === 0)) {
    return { rects: [], dropped: 0, hasBbox: false };
  }
  if (!Array.isArray(bbox)) {
    return { rects: [], dropped: 0, hasBbox: false };
  }
  const rects: RectPercent[] = [];
  let dropped = 0;
  for (const box of bbox) {
    const rect = rectFromBox(box);
    if (rect) rects.push(rect);
    else dropped += 1;
  }
  return { rects, dropped, hasBbox: true };
}

/** 降级视图要展示的一段原文：优先用引用自带的 snippet。 */
export function fallbackExcerpt(snippet: unknown, maxChars = 160): string {
  if (typeof snippet !== 'string') return '';
  const trimmed = snippet.trim();
  if (trimmed.length <= maxChars) return trimmed;
  return `${trimmed.slice(0, maxChars)}…`;
}
