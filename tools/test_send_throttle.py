"""发送节流自检：最小间隔 + 滑动窗口硬上限。

两个账号共用一个进程，所以这把锁是全局的。
运行：.venv/bin/python tools/test_send_throttle.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app" / "src"))
from tg_game.telegram import send_utils as su


async def main() -> None:
    # 把窗口缩小，免得测试要跑十分钟
    su.SEND_MIN_INTERVAL_SECONDS = 0.05
    su.SEND_WINDOW_SECONDS = 1.0
    su.SEND_MAX_PER_WINDOW = 5
    su._last_send_at = 0.0
    su._recent_send_times.clear()

    # 最小间隔：连续两次至少隔 SEND_MIN_INTERVAL_SECONDS
    await su._throttle_outgoing_send()
    t0 = time.monotonic()
    await su._throttle_outgoing_send()
    gap = time.monotonic() - t0
    assert gap >= 0.045, gap

    # 硬上限：窗口内第 6 条必须被压到窗口滚动之后
    su._last_send_at = 0.0
    su._recent_send_times.clear()
    start = time.monotonic()
    for _ in range(su.SEND_MAX_PER_WINDOW):
        await su._throttle_outgoing_send()
    filled = time.monotonic() - start
    assert filled < su.SEND_WINDOW_SECONDS, filled

    t1 = time.monotonic()
    await su._throttle_outgoing_send()
    held = time.monotonic() - t1
    assert held > 0.3, f"第 {su.SEND_MAX_PER_WINDOW + 1} 条没有被上限拦住: {held}"
    assert len(su._recent_send_times) <= su.SEND_MAX_PER_WINDOW + 1

    print("ok")


asyncio.run(main())
