import { describe, expect, it } from 'vitest';
import { DEFAULT_DEV_TOKEN } from './devToken';

function payloadOf(token: string): Record<string, unknown> {
  const part = token.split('.')[1] ?? '';
  return JSON.parse(
    decodeURIComponent(
      atob(part.replace(/-/g, '+').replace(/_/g, '/'))
        .split('')
        .map((char) => `%${char.charCodeAt(0).toString(16).padStart(2, '0')}`)
        .join(''),
    ),
  ) as Record<string, unknown>;
}

describe('内置开发凭证', () => {
  it('是未过期的、带 dev fixture claims 的 JWT', () => {
    const parts = DEFAULT_DEV_TOKEN.split('.');
    expect(parts).toHaveLength(3);
    const payload = payloadOf(DEFAULT_DEV_TOKEN);
    expect(payload.sub).toBe('u_dev');
    expect(payload.user_depts).toEqual(['fin', 'corp']);
    expect(payload.user_roles).toEqual(['staff']);
    expect(payload.admin).toBe(false);
    expect(typeof payload.exp).toBe('number');
    expect((payload.exp as number) * 1000).toBeGreaterThan(Date.now());
  });
});
