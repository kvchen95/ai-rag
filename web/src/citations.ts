// 引用角标解析：把正文里的 `[n]` 切成「文本片段 + 角标」。
//
// 三条口径：
// 1. **只认 citations 里有的编号**。`[9]` 在 items 里找不到就按纯文本渲染——
//    渲染一个点不动的角标比不渲染更糟（用户以为能跳，点了没反应）。
// 2. `[1,2]` / `[1, 2]` 这种合写要拆开：模型的输出不可信，不能只认 `[1][2]`。
// 3. 角标支持的最大编号是个数字；正文里 `[0]` 不合法（编号从 1 开始，附录 C.2）。

export interface TextSegment {
  kind: 'text';
  text: string;
}

export interface CiteSegment {
  kind: 'cite';
  n: number;
  /** 命中的引用；`undefined` 表示编号越界（渲染成纯文本）。 */
  citation?: import('./types').CitationItem;
}

export type Segment = TextSegment | CiteSegment;

const CITE_RE = /\[(\d+(?:\s*,\s*\d+)*)\]/g;

/**
 * 切分正文。返回的片段按出现顺序排列，`text` 片段与 `cite` 片段交替。
 * 无名引用（越界编号）会被合进相邻的文本片段，保证片段数与角标数一致。
 */
export function splitCitations(
  text: string,
  citations: readonly import('./types').CitationItem[] | undefined,
): Segment[] {
  if (!text) return [];
  if (!citations || citations.length === 0) return [{ kind: 'text', text }];

  const byN = new Map<number, import('./types').CitationItem>();
  for (const item of citations) byN.set(item.n, item);

  const segments: Segment[] = [];
  let cursor = 0;
  let pendingText = '';

  const flushText = () => {
    if (pendingText) {
      segments.push({ kind: 'text', text: pendingText });
      pendingText = '';
    }
  };

  let match: RegExpExecArray | null;
  CITE_RE.lastIndex = 0;
  while ((match = CITE_RE.exec(text)) !== null) {
    if (match.index > cursor) pendingText += text.slice(cursor, match.index);
    cursor = match.index + match[0].length;

    const numbers = (match[1] ?? "")
      .split(',')
      .map((part) => Number(part.trim()))
      .filter((n) => Number.isFinite(n) && n > 0);

    const known = numbers.map((n) => byN.get(n)).filter((c) => c !== undefined);
    if (known.length === 0) {
      // 越界编号：原样留在文本里，不制造点不动的按钮。
      pendingText += match[0];
      continue;
    }
    flushText();
    // `[1,2]` 展开成两个角标（中间的逗号视觉上不重要，跳转能力才重要）。
    for (const citation of known) {
      segments.push({ kind: 'cite', n: citation.n, citation });
    }
  }
  if (cursor < text.length) pendingText += text.slice(cursor);
  flushText();
  return segments;
}

export interface CitationAudit {
  /** 正文里出现的编号（含越界）。 */
  referenced: number[];
  /** 能点回原文的编号（items 里存在）。 */
  resolvable: number[];
  /** 正文引了但 items 里没有的编号——点不动，属于服务端的不一致。 */
  orphans: number[];
  /** items 里有但正文没引的编号（列表里仍展示，方便对照）。 */
  uncited: number[];
  /** 可点回率 = resolvable / referenced；正文没有引用时为 1（不适用）。 */
  resolvableRate: number;
}

/** 演示样本上的「角标可点回率」就是这里算出来的，别用肉眼数。 */
export function auditCitations(
  text: string,
  citations: readonly import('./types').CitationItem[] | undefined,
): CitationAudit {
  const all = new Set<number>((citations ?? []).map((c) => c.n));
  const referenced: number[] = [];
  const CITE_NUMS = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
  let match: RegExpExecArray | null;
  while ((match = CITE_NUMS.exec(text)) !== null) {
    for (const part of (match[1] ?? "").split(',')) {
      const n = Number(part.trim());
      if (Number.isFinite(n) && n > 0) referenced.push(n);
    }
  }
  const unique = Array.from(new Set(referenced));
  const resolvable = unique.filter((n) => all.has(n));
  const orphans = unique.filter((n) => !all.has(n));
  const uncited = Array.from(all).filter((n) => !unique.includes(n));
  return {
    referenced: unique,
    resolvable,
    orphans,
    uncited,
    resolvableRate: unique.length === 0 ? 1 : resolvable.length / unique.length,
  };
}
