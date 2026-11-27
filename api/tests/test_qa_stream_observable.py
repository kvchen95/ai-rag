"""`AsyncQueueStream` 的三条路径：正常完成、主动 dispose、工作线程抛异常。

这组测试是 §10.5.3 的可执行判据——"收尾只有一个出口"如果不能被测试证明，
就只是一句注释。
"""

from __future__ import annotations

import asyncio

from api.qa.stream import CLOSE, from_async_queue


async def _drain(stream, timeout: float = 2.0) -> tuple[list, list, bool]:
    """订阅并收集事件，返回 (收到的 payload, 错误, 是否完成)。"""
    got: list = []
    errors: list = []
    completed = asyncio.Event()

    def on_next(item: object) -> None:
        got.append(item)

    def on_error(exc: Exception) -> None:
        errors.append(exc)
        completed.set()

    def on_completed() -> None:
        completed.set()

    disposable = stream.observable.subscribe(
        on_next=on_next, on_error=on_error, on_completed=on_completed
    )
    await asyncio.wait_for(completed.wait(), timeout)
    disposable.dispose()
    return got, errors, not errors


def test_stream_completes_after_close_sentinel() -> None:
    """队列投 CLOSE 后应 complete，且之前的事件按序到达。"""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def worker() -> None:
            for item in ("step", "token", "done"):
                queue.put_nowait(("event", item))
            queue.put_nowait(CLOSE)

        stream = from_async_queue(
            loop=loop, queue=queue, start=lambda: loop.create_task(worker())
        )
        got, errors, completed = await _drain(stream)
        assert got == ["step", "token", "done"]
        assert errors == []
        assert completed

    asyncio.run(scenario())


def test_dispose_stops_delivery_and_calls_on_dispose_once() -> None:
    """Dispose 之后不再有 next，`on_dispose` 恰好调用一次。"""
    called: list[int] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        got: list = []

        async def worker() -> None:
            queue.put_nowait(("event", "first"))
            await asyncio.sleep(10)  # 模拟"连接还开着"

        stream = from_async_queue(
            loop=loop,
            queue=queue,
            start=lambda: loop.create_task(worker()),
            on_dispose=lambda: called.append(1),
        )
        disposable = stream.observable.subscribe(
            on_next=lambda x: got.append(x)
        )
        await asyncio.sleep(0.05)
        disposable.dispose()
        disposable.dispose()  # 幂等：第二次不该再调 on_dispose
        await asyncio.sleep(0.05)

        assert got == ["first"], "dispose 之后不该再投递"
        assert len(called) == 1, "on_dispose 必须恰好一次"

    asyncio.run(scenario())


def test_worker_exception_goes_to_on_error() -> None:
    """工作线程抛异常时走 on_error，而不是静默 complete。"""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        async def worker() -> None:
            queue.put_nowait(("event", "step"))
            raise RuntimeError("pipeline exploded")

        stream = from_async_queue(
            loop=loop, queue=queue, start=lambda: loop.create_task(worker())
        )
        got, errors, _ = await _drain(stream)
        assert got == ["step"]
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)

    asyncio.run(scenario())
