"""问答事件流的 Observable 适配（详细设计 §10.5.3）。.

**只包一层**。这里做的事仅限：把「工作线程 + asyncio 队列」这条异步序列包成
`Observable`，好让取消、完成、异常三条路径共用一个 `dispose()`。解析、检索、
数据库访问一律不进 Observable——它们有明确返回值，包成流只会让调试变难。

为什么不用 `Observable.from_iterable`
-------------------------------------
编排工作跑在 `run_in_executor` 的线程里（psycopg 是同步的），产出通过
`loop.call_soon_threadsafe` 投回事件循环。`from_iterable` 是同步迭代，塞进
线程桥会让"谁负责收尾"重新变模糊。所以这里直接建一个 `Observable`，
`subscribe` 时才启动工作线程。

不 cancel 工作线程
------------------
与 §4.1 翻牌事务同一条理由：它正在用 psycopg 连接，强行中断会留下"半借出"
连接。所以 `dispose()` 做的是"通知它停下 + 等它自己收尾"，不是 kill。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from reactivex import Observable
from reactivex.disposable import Disposable

# 队列里表示"结束了"的哨兵。与 `api/routers/qa.py` 里 worker 的约定一致，
# 所以迁移时 worker 本身不用改。
CLOSE: tuple[str, None] = ("close", None)


class AsyncQueueStream:
    """把「工作线程投队列」包成 Observable。.

    用法：

        stream = AsyncQueueStream(
            loop=loop,
            queue=queue,
            start=lambda: loop.run_in_executor(None, worker),
        )disposable = stream.observable.subscribe(on_next=..., on_error=...,
        on_completed=...)...disposable.dispose()   # 唯一收尾入口

    约定：`start()` 返回一个 awaitable（通常是 executor 的 future），队列里投
    `CLOSE` 表示结束。`on_next` 收到的是队列里的 payload 本身，不做解包——
    解包是调用方的事，适配层不该知道业务形状。
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Any],
        start: Callable[[], Any],
        on_dispose: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the instance."""
        self._loop = loop
        self._queue = queue
        self._start = start
        self._on_dispose = on_dispose
        self.observable = Observable(self._subscribe)

    def _subscribe(self, observer: Any, scheduler: Any = None) -> Disposable:
        """Run _subscribe."""
        disposed = threading.Event()
        # 工作线程在 subscribe 时才启动：没订阅就不该有副作用。
        future = self._start()

        async def pump() -> None:
            """把队列内容推给 observer。跑在事件循环里，不占工作线程。."""
            try:
                while True:
                    if disposed.is_set():
                        break
                    # 同时等两件事：队列有新事件，或工作线程已经结束。
                    # 只等队列的话，worker 抛异常又没投 CLOSE 时 pump 会永久挂起
                    # （真实 worker 在 finally 里投 CLOSE，
                    # 但契约不该只靠这一条保证）。
                    get_task = asyncio.ensure_future(self._queue.get())
                    done, _ = await asyncio.wait(
                        {get_task, future}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if get_task not in done:
                        get_task.cancel()
                        # 工作线程先结束：把它剩下的异常交给 observer。
                        await asyncio.shield(future)
                        break
                    kind, payload = get_task.result()
                    if kind == "close":
                        break
                    if disposed.is_set():
                        break
                    observer.on_next(payload)
                if disposed.is_set():
                    return
                # 正常结束：等 executor 收尾（不 cancel，见模块文档）。
                await asyncio.shield(future)
                observer.on_completed()
            except asyncio.CancelledError:
                # 被 dispose 打断：不报错也不 complete，调用方已经知道结束了。
                raise
            except Exception as exc:  # noqa: BLE001 - 交给 observer 决定怎么报
                if not disposed.is_set():
                    observer.on_error(exc)

        task = self._loop.create_task(pump())

        def dispose() -> None:
            """唯一收尾入口。.

            标记停止、叫醒阻塞在 get() 上的 pump、通知外部清理。

            先 set 再 put CLOSE：`pump` 里两处 `disposed.is_set()` 检查保证
            已投递但未消费的事件不会再推给 observer（dispose 之后不该有 next）。
            """
            if disposed.is_set():
                return
            disposed.set()
            # 队列无界时不会发生。
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover
                self._queue.put_nowait(CLOSE)
            task.cancel()
            if self._on_dispose is not None:
                self._on_dispose()

        return Disposable(dispose)


def from_async_queue(
    *,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[Any],
    start: Callable[[], Any],
    on_dispose: Callable[[], None] | None = None,
) -> AsyncQueueStream:
    """`AsyncQueueStream` 的工厂函数（函数式写法，调用处更像 Rx）。."""
    return AsyncQueueStream(
        loop=loop, queue=queue, start=start, on_dispose=on_dispose
    )
