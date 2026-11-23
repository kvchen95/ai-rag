// token 合帧器。
//
// B4 的逐字粒度是 2 字/帧（`SSE_TOKEN_CHUNK_CHARS`），一次 300 字答案 ≈ 150 帧。
// 逐帧 dispatch 会把「整条消息重新渲染」跑到每秒几十次；合帧后每个绘制帧只提交一次。
//
// 调度器必须可注入（浏览器 rAF / 测试假时钟），否则「10 帧只提交 1 次」这条
// 结论没法断言（见 `coalesce.test.ts`）。
//
// 铁律：**任何非 token 事件（citations / fallback / clarify / done）落地前先 flush()**，
// 否则顺序会反：正文比引用晚到，角标先出现再补字。

export interface Scheduler {
  schedule: (cb: () => void) => number;
  cancel: (id: number) => void;
}

export const rafScheduler: Scheduler = {
  schedule: (cb) => window.requestAnimationFrame(cb),
  cancel: (id) => window.cancelAnimationFrame(id),
};

export class TokenCoalescer {
  private buffer = '';
  private handle: number | null = null;

  constructor(
    private readonly emit: (delta: string) => void,
    private readonly scheduler: Scheduler = rafScheduler,
  ) {}

  push(delta: string): void {
    if (!delta) return;
    this.buffer += delta;
    if (this.handle === null) {
      this.handle = this.scheduler.schedule(() => {
        this.handle = null;
        this.flush();
      });
    }
  }

  /** 立即落地缓冲。返回本次 flush 的字符数（0 表示没有待落地内容）。 */
  flush(): number {
    if (this.handle !== null) {
      this.scheduler.cancel(this.handle);
      this.handle = null;
    }
    const pending = this.buffer;
    if (!pending) return 0;
    this.buffer = '';
    this.emit(pending);
    return pending.length;
  }

  /** 卸载/中断时调用：取消排队的绘制帧，缓冲**不落地**（中断不保留半截答案）。 */
  dispose(): void {
    if (this.handle !== null) {
      this.scheduler.cancel(this.handle);
      this.handle = null;
    }
    this.buffer = '';
  }

  pendingChars(): number {
    return this.buffer.length;
  }
}
