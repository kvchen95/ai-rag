// SSE 帧解析（纯逻辑，可在 node 环境单测）。
//
// 为什么不是 EventSource：本项目的问答接口是 `POST /api/v1/qa/stream`
// （附录 C.1），EventSource 只能 GET，且不能带 Authorization 头。所以用
// `fetch` + ReadableStream 自己解析 SSE 帧。
//
// 解析器只做「字节 → 帧」，不懂业务。三条容错口径写在这里：
// 1. 一个 TCP chunk 里可能是 0.3 帧、1 帧或 5 帧；帧边界只认空行。
// 2. 注释行（`: ping`）是心跳，必须吞掉——把它当数据会喂给 JSON.parse 炸掉整条流。
// 3. 畸形帧（没有 data、JSON 坏了）不许抛：丢帧并计数，业务侧继续跑
//    （§10.4 的「事件乱序不崩」同一条口径：前端不能因为一条坏帧停摆）。

export interface RawFrame {
  /** `event:` 行的值；没有则空串。 */
  event: string;
  /** 多行 `data:` 按规范用 \n 拼接。 */
  data: string;
  id?: string;
  /** 重试间隔（服务端没发就 undefined）。 */
  retry?: number;
}

const BLOCK_SEPARATOR = /\r?\n\r?\n/;

/** 把一块完整的 SSE 报文（不含末尾空行）解析成一帧；没有 data 返回 null。 */
export function parseFrameBlock(block: string): RawFrame | null {
  let eventName = '';
  let id: string | undefined;
  let retry: number | undefined;
  const dataLines: string[] = [];

  for (const rawLine of block.split(/\r?\n/)) {
    if (rawLine === '') continue;
    if (rawLine.startsWith(':')) continue; // 注释/心跳
    const colon = rawLine.indexOf(':');
    if (colon === -1) {
      // 无冒号的行按规范当作「字段名 + 空值」，本项目里没有这种字段，跳过。
      continue;
    }
    const field = rawLine.slice(0, colon);
    // 规范：冒号后紧接的一个空格要去掉，其余保留（data 里可能有前导空格）。
    let value = rawLine.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'event':
        eventName = value;
        break;
      case 'data':
        dataLines.push(value);
        break;
      case 'id':
        id = value;
        break;
      case 'retry': {
        const n = Number(value);
        if (Number.isFinite(n)) retry = n;
        break;
      }
      default:
        break; // 未知字段忽略（规范要求）
    }
  }

  if (dataLines.length === 0) return null;
  return { event: eventName, data: dataLines.join('\n'), id, retry };
}

/**
 * 增量解析器：`feed()` 喂任意切片，返回在这一片里**恰好凑齐**的那些帧。
 * 余下的半帧留在 buffer 里，等下一次 feed。
 */
export class SseParser {
  private buffer = '';
  /** 被丢掉的帧数（畸形 JSON 由调用方计，这里的坏块也计）。 */
  dropped = 0;

  constructor(private readonly maxBufferChars = 1_000_000) {}

  feed(chunk: string): RawFrame[] {
    if (!chunk) return [];
    this.buffer += chunk;
    // 防内存膨胀：一个病态的长帧（永不出现空行）不该把浏览器撑爆。
    if (this.buffer.length > this.maxBufferChars) {
      this.buffer = '';
      this.dropped += 1;
      return [];
    }

    const blocks = this.buffer.split(BLOCK_SEPARATOR);
    // 最后一段可能还没写完，留到下一次。
    this.buffer = blocks.pop() ?? '';

    const frames: RawFrame[] = [];
    for (const block of blocks) {
      if (!block.trim()) continue;
      const frame = parseFrameBlock(block);
      if (!frame) {
        // 只有注释/未知字段的块是心跳，不算丢帧；真的是空块也跳过。
        continue;
      }
      frames.push(frame);
    }
    return frames;
  }

  /** 流结束时把残留的最后一块也解析出来（有些服务端不发最后的空行）。 */
  flush(): RawFrame[] {
    const rest = this.buffer;
    this.buffer = '';
    if (!rest.trim()) return [];
    const frame = parseFrameBlock(rest);
    return frame ? [frame] : [];
  }
}
