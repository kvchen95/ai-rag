import { describe, expect, it } from 'vitest';
import { buildError, chunkFigureUrl, kindFromStatus, pickErrorMeta, samplePdfUrl, toSseEvent } from './api';
import { USER_MESSAGES } from './types';

describe('HTTP 错误映射', () => {
  it('maps the four card-critical statuses to the spec copy', () => {
    expect(kindFromStatus(401)).toBe('auth_invalid');
    expect(kindFromStatus(403)).toBe('forbidden');
    expect(kindFromStatus(404)).toBe('not_found');
    expect(kindFromStatus(503)).toBe('unavailable');
    expect(kindFromStatus(500)).toBe('internal');
    expect(kindFromStatus(429)).toBe('unavailable');
  });

  it('never lets response-body text reach the UI', () => {
    // 服务端如果（错误地）把堆栈塞进 message，界面也只显示固定文案。
    const body = {
      error: {
        code: 'auth_invalid',
        message: 'Traceback (most recent call last):\n File "/app/api/identity.py", line 171, in _decode',
        request_id: 'req-123',
      },
    };
    const meta = pickErrorMeta(body);
    expect(meta.code).toBe('auth_invalid');
    expect(meta.requestId).toBe('req-123');
    const ui = buildError(401, meta);
    expect(ui.message).toBe(USER_MESSAGES.auth_invalid);
    expect(JSON.stringify(ui)).not.toContain('Traceback');
    expect(JSON.stringify(ui)).not.toContain('/app/api');
  });

  it('ignores unknown error codes (no free-form text from the server)', () => {
    const meta = pickErrorMeta({ error: { code: 'sqlstate_42883', message: 'function rag() does not exist' } });
    expect(meta.code).toBeUndefined();
  });

  it('survives a non-JSON body', () => {
    expect(pickErrorMeta('<html>Bad Gateway</html>')).toEqual({});
    expect(buildError(502, {}).message).toBe(USER_MESSAGES.internal);
  });
});

describe('toSseEvent', () => {
  it('uses the event: line as the event name', () => {
    const event = toSseEvent({ event: 'token', data: '{"delta":"住宿"}' });
    expect(event).toEqual({ type: 'token', delta: '住宿' });
  });

  it('falls back to data.type when the SSE event name is missing (replayed fixtures)', () => {
    const event = toSseEvent({ event: '', data: '{"type":"citations","items":[]}' });
    expect(event?.type).toBe('citations');
  });

  it('rejects unknown event names and broken JSON without throwing', () => {
    expect(toSseEvent({ event: 'tool_call', data: '{"type":"tool_call"}' })).toBeNull();
    expect(toSseEvent({ event: 'token', data: '{"delta":' })).toBeNull();
    expect(toSseEvent({ event: 'token', data: '' })).toBeNull();
    expect(toSseEvent({ event: 'token', data: '[1,2,3]' })).toBeNull();
  });

  it('overrides a lying data.type with the frame name', () => {
    const event = toSseEvent({ event: 'done', data: '{"type":"token","verdict":"pass"}' });
    expect(event?.type).toBe('done');
  });
});

describe('URL 构造', () => {
  it('keeps slashes inside doc_key but encodes each segment', () => {
    expect(samplePdfUrl('policy/travel-cn')).toBe('/api/v1/samples/policy/travel-cn.pdf');
    expect(samplePdfUrl('hr/李四的假条')).toBe(
      `/api/v1/samples/hr/${encodeURIComponent('李四的假条')}.pdf`,
    );
  });

  it('escapes chunk ids in the figure path', () => {
    expect(chunkFigureUrl('ch_9f2c31')).toBe('/api/v1/chunks/ch_9f2c31/figure');
    expect(chunkFigureUrl('a/b')).toBe('/api/v1/chunks/a%2Fb/figure');
  });
});
