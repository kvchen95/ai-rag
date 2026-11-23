import { describe, expect, it } from 'vitest';
import { SseParser, parseFrameBlock } from './sse';

describe('parseFrameBlock', () => {
  it('parses event name and single-line data', () => {
    const frame = parseFrameBlock('event: step\ndata: {"type":"step","phase":"retrieval"}');
    expect(frame?.event).toBe('step');
    expect(JSON.parse(frame!.data).phase).toBe('retrieval');
  });

  it('joins multi-line data with a newline (spec behaviour)', () => {
    const frame = parseFrameBlock('event: token\ndata: {"delta":"a"\ndata: }');
    expect(frame?.data).toBe('{"delta":"a"\n}');
  });

  it('keeps leading spaces beyond the first one in data', () => {
    const frame = parseFrameBlock('event: token\ndata:  two spaces');
    expect(frame?.data).toBe(' two spaces');
  });

  it('drops comment/heartbeat lines', () => {
    expect(parseFrameBlock(': ping')).toBeNull();
  });

  it('returns null when a block carries no data field', () => {
    expect(parseFrameBlock('event: done')).toBeNull();
  });

  it('reads id and retry', () => {
    const frame = parseFrameBlock('id: run_1:3\nevent: token\ndata: x\nretry: 3000');
    expect(frame?.id).toBe('run_1:3');
    expect(frame?.retry).toBe(3000);
  });
});

describe('SseParser.feed', () => {
  it('splits several frames arriving in one chunk', () => {
    const parser = new SseParser();
    const frames = parser.feed(
      'event: step\ndata: {"type":"step","phase":"retrieval","hop":0,"elapsed_ms":1}\n\n' +
        'event: token\ndata: {"type":"token","delta":"住宿"}\n\n' +
        'event: token\ndata: {"type":"token","delta":"500"}\n\n',
    );
    expect(frames.map((f) => f.event)).toEqual(['step', 'token', 'token']);
  });

  it('handles CRLF line endings', () => {
    const parser = new SseParser();
    const frames = parser.feed('event: token\r\ndata: {"delta":"x"}\r\n\r\n');
    expect(frames).toHaveLength(1);
    expect(frames[0]!.data).toBe('{"delta":"x"}');
  });

  it('holds an incomplete frame until the rest arrives', () => {
    const parser = new SseParser();
    expect(parser.feed('event: token\ndata: {"delta":"住')).toHaveLength(0);
    const frames = parser.feed('宿"}\n\n');
    expect(frames).toHaveLength(1);
    expect(frames[0]!.data).toBe('{"delta":"住宿"}');
  });

  it('ignores heartbeat frames in the middle of a stream', () => {
    const parser = new SseParser();
    const frames = parser.feed(': ping\n\nevent: token\ndata: {"delta":"a"}\n\n: keep-alive\n\n');
    expect(frames).toHaveLength(1);
    expect(parser.dropped).toBe(0);
  });

  it('flush() returns the tail frame when the server never sends the blank line', () => {
    const parser = new SseParser();
    parser.feed('event: done\ndata: {"type":"done","verdict":"pass"}');
    const tail = parser.flush();
    expect(tail).toHaveLength(1);
    expect(tail[0]!.event).toBe('done');
  });

  it('does not throw on garbage bytes', () => {
    const parser = new SseParser();
    const frames = parser.feed('garbage without fields\n\n\u0000\u0001\n\n');
    expect(frames).toHaveLength(0);
  });

  it('drops an unbounded frame instead of growing memory forever', () => {
    const parser = new SseParser(1000);
    parser.feed('x'.repeat(1500));
    expect(parser.dropped).toBe(1);
    // 缓冲区被丢掉之后还能继续工作。
    expect(parser.feed('event: token\ndata: {"delta":"ok"}\n\n')).toHaveLength(1);
  });
});
