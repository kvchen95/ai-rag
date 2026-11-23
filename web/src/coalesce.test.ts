import { describe, expect, it } from 'vitest';
import { TokenCoalescer, type Scheduler } from './coalesce';

function fakeScheduler() {
  let nextId = 1;
  const queued = new Map<number, () => void>();
  const scheduler: Scheduler = {
    schedule: (cb) => {
      const id = nextId++;
      queued.set(id, cb);
      return id;
    },
    cancel: (id) => queued.delete(id),
  };
  const tick = () => {
    const callbacks = [...queued.values()];
    queued.clear();
    for (const cb of callbacks) cb();
  };
  return { scheduler, tick, queuedCount: () => queued.size };
}

describe('TokenCoalescer', () => {
  it('提交次数 = 绘制帧数，不是 token 帧数', () => {
    const { scheduler, tick } = fakeScheduler();
    const emitted: string[] = [];
    const coalescer = new TokenCoalescer((delta) => emitted.push(delta), scheduler);

    // B4 的逐字粒度是 2 字/帧：12 帧 = 12 次 push，但只该有 1 次提交。
    for (let i = 0; i < 12; i += 1) coalescer.push('住宿');
    expect(emitted).toHaveLength(0);
    tick();
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toBe('住宿'.repeat(12));
  });

  it('flush() 立刻落地并返回字符数（非 token 事件之前必须先 flush）', () => {
    const { scheduler } = fakeScheduler();
    const emitted: string[] = [];
    const coalescer = new TokenCoalescer((delta) => emitted.push(delta), scheduler);
    coalescer.push('abc');
    coalescer.push('defg');
    expect(coalescer.flush()).toBe(7);
    expect(emitted).toEqual(['abcdefg']);
    // 已经 flush 过的缓冲不会再被绘制帧重复提交。
    expect(coalescer.pendingChars()).toBe(0);
  });

  it('dispose() 丢弃未落地的缓冲（中断不保留半截答案）', () => {
    const { scheduler, tick, queuedCount } = fakeScheduler();
    const emitted: string[] = [];
    const coalescer = new TokenCoalescer((delta) => emitted.push(delta), scheduler);
    coalescer.push('半截');
    expect(queuedCount()).toBe(1);
    coalescer.dispose();
    expect(queuedCount()).toBe(0);
    tick();
    expect(emitted).toHaveLength(0);
  });

  it('空的 delta 不排队也不提交', () => {
    const { scheduler, queuedCount } = fakeScheduler();
    const emitted: string[] = [];
    const coalescer = new TokenCoalescer((delta) => emitted.push(delta), scheduler);
    coalescer.push('');
    expect(queuedCount()).toBe(0);
    expect(coalescer.flush()).toBe(0);
    expect(emitted).toHaveLength(0);
  });
});
