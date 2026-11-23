import { describe, expect, it } from 'vitest';
import { fallbackExcerpt, rectFromBox, rectsFromBbox } from './bbox';

describe('rectFromBox', () => {
  it('converts a normalized box into page percentages', () => {
    expect(rectFromBox([0.12, 0.34, 0.88, 0.51])).toEqual({
      left: 12,
      top: 34,
      width: 76,
      height: 17,
    });
  });

  it('normalizes inverted corners instead of drawing a negative rect', () => {
    const rect = rectFromBox([0.9, 0.6, 0.1, 0.3]);
    expect(rect).toMatchObject({ left: 10, top: 30, width: 80, height: 30 });
  });

  it('rejects pixel-space coordinates (0..1 only)', () => {
    // 典型事故：解析器把像素坐标直接塞进来，画出来的框会覆盖整页。
    expect(rectFromBox([120, 340, 880, 510])).toBeNull();
  });

  it('rejects zero-area and non-numeric boxes', () => {
    expect(rectFromBox([0.2, 0.2, 0.2, 0.5])).toBeNull();
    expect(rectFromBox([0.1, 'x', 0.5, 0.5])).toBeNull();
    expect(rectFromBox([0.1, 0.2, 0.3])).toBeNull();
  });

  it('clamps tiny floating-point overshoot instead of failing', () => {
    const rect = rectFromBox([-1e-9, 0.1, 1.0000000002, 0.9]);
    expect(rect).toMatchObject({ left: 0, width: 100 });
  });
});

describe('rectsFromBbox', () => {
  it('reports bbox as missing when it is absent', () => {
    expect(rectsFromBbox(undefined)).toEqual({ rects: [], dropped: 0, hasBbox: false });
    expect(rectsFromBbox([])).toEqual({ rects: [], dropped: 0, hasBbox: false });
  });

  it('keeps valid boxes and counts dropped ones', () => {
    const set = rectsFromBbox([
      [0.1, 0.2, 0.5, 0.4],
      [100, 200, 300, 400],
      [0.3, 0.4, 0.3, 0.6],
    ]);
    expect(set.hasBbox).toBe(true);
    expect(set.rects).toHaveLength(1);
    expect(set.dropped).toBe(2);
  });

  it('survives a bbox that is not an array at all', () => {
    expect(rectsFromBbox('oops' as unknown as number[][])).toEqual({
      rects: [],
      dropped: 0,
      hasBbox: false,
    });
  });
});

describe('fallbackExcerpt', () => {
  it('trims and truncates long snippets', () => {
    const long = `  ${'字'.repeat(200)}  `;
    const excerpt = fallbackExcerpt(long, 20);
    expect(excerpt.endsWith('…')).toBe(true);
    expect(excerpt.length).toBeLessThanOrEqual(21);
  });

  it('returns empty string for non-string snippets', () => {
    expect(fallbackExcerpt(null)).toBe('');
    expect(fallbackExcerpt({} as unknown as string)).toBe('');
  });
});
