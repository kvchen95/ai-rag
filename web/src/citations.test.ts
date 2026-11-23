import { describe, expect, it } from 'vitest';
import { auditCitations, splitCitations } from './citations';
import type { CitationItem } from './types';

const cite = (n: number, chunk = `ch_${n}`): CitationItem => ({
  n,
  doc_key: 'policy/travel-cn',
  doc_name: '差旅管理办法-2025版',
  page_num: 40 + n,
  snippet: `片段 ${n}`,
  chunk_id: chunk,
});

describe('splitCitations', () => {
  it('returns one text segment when there is no citation', () => {
    const segments = splitCitations('没有任何引用的句子。', []);
    expect(segments).toEqual([{ kind: 'text', text: '没有任何引用的句子。' }]);
  });

  it('turns known [n] into clickable segments and keeps surrounding text', () => {
    const segments = splitCitations('住宿费 500 元[1]。', [cite(1)]);
    expect(segments.map((s) => s.kind)).toEqual(['text', 'cite', 'text']);
    expect(segments[1]).toMatchObject({ kind: 'cite', n: 1, citation: { chunk_id: 'ch_1' } });
  });

  it('expands a combined [1,2] into two badges', () => {
    const segments = splitCitations('两条证据[1,2]。', [cite(1), cite(2)]);
    expect(segments.filter((s) => s.kind === 'cite')).toHaveLength(2);
  });

  it('leaves an orphan [9] as plain text (no dead button)', () => {
    const segments = splitCitations('编造依据[9]。', [cite(1)]);
    const cites = segments.filter((s) => s.kind === 'cite');
    expect(cites).toHaveLength(0);
    expect(segments.map((s) => (s.kind === 'text' ? s.text : '')).join('')).toContain('[9]');
  });

  it('does not treat array brackets or [0] as citations', () => {
    const text = 'a[0] b[1,2] c[abc]';
    const segments = splitCitations(text, [cite(1), cite(2)]);
    // [0] 不是合法编号（编号从 1 开始），[abc] 不是编号：[0] 原样留着。
    expect(segments.filter((s) => s.kind === 'text').map((s) => (s as { text: string }).text).join('')).toContain('[0]');
  });
});

describe('auditCitations', () => {
  it('computes the resolvable rate from the answer text', () => {
    const audit = auditCitations('甲[1]，乙[2]，丙[9]。', [cite(1), cite(2)]);
    expect(audit.referenced).toEqual([1, 2, 9]);
    expect(audit.resolvable).toEqual([1, 2]);
    expect(audit.orphans).toEqual([9]);
    expect(audit.resolvableRate).toBeCloseTo(2 / 3, 5);
  });

  it('lists citations that never appear in the text', () => {
    const audit = auditCitations('只有一句[1]。', [cite(1), cite(2)]);
    expect(audit.uncited).toEqual([2]);
  });

  it('returns rate 1 when nothing is referenced (not applicable)', () => {
    const audit = auditCitations('没有角标。', []);
    expect(audit.referenced).toEqual([]);
    expect(audit.resolvableRate).toBe(1);
  });
});
