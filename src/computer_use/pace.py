"""核心：限速、预算与急停。相邻的输入动作之间强制留出间隔，连续次数到顶就停下等人确认，
热键能立刻停掉还没注入的输入（ADR-0003）。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from computer_use.action_log import Intercepted
from computer_use.danger import INPUT_TOOLS, Verdict
from computer_use.desktop import DesktopPort, PaceState

_STOPPED = "已急停，输入工具拒绝执行，直到调用 resume 并经人确认"

INPUT_INTERVAL = 0.5
"""相邻两次输入动作的最小间隔，秒。

20 次输入至少要 9.5 秒，ADR-0003 里「三秒内点了二十下」那种事故排不开。
"""

INPUT_BUDGET = 20
"""连续输入动作的预算。做满这么多次，下一次须经人确认才能继续。"""

Sleeper = Callable[[float, threading.Event], None]


class Pace:
    """一次服务进程里的限速。同一个 `Pace` 贯穿所有输入工具，间隔才连得起来。"""

    def __init__(
        self,
        desktop: DesktopPort,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper | None = None,
    ) -> None:
        self._desktop = desktop
        self._clock = clock
        self._sleep = sleep or _sleep_until_cancelled
        self._last: float | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._admit = threading.Lock()

    def reject_if_stopped(self) -> None:
        """急停之后输入工具一律拒绝，裁决凭据也不能把这一次放行。"""

        if self._desktop.read_pace().stopped:
            raise Intercepted(_STOPPED)

    def wait_to_inject(self, *, cleared: bool) -> None:
        """排到队首，与上一次已经注入的输入隔开 `INPUT_INTERVAL`，然后占住注入权。

        一次只放行一个输入。等待期间若急停，这一次和排在后面的都不再注入。
        `cleared` 为真表示这一次已经凭裁决越过了预算，轮到它时不再因预算退回。
        注入结束要调用 `mark_injected`；注入没做成要调用 `abandon`。
        """

        self._admit.acquire()
        try:
            while True:
                with self._lock:
                    if self._desktop.read_pace().stopped:
                        raise Intercepted(_STOPPED)
                    if not cleared and self._desktop.read_pace().streak >= INPUT_BUDGET:
                        raise Intercepted(budget_block_message())
                    remaining = self._remaining()
                    if remaining <= 0:
                        return
                self._sleep(remaining, self._cancel)
        except BaseException:
            self._admit.release()
            raise

    def stop(self) -> None:
        """急停：唤醒正在等待的输入，并让之后的输入工具拒绝，直到 `resume`。"""

        with self._lock:
            state = self._desktop.read_pace()
            self._desktop.write_pace(PaceState(streak=state.streak, stopped=True))
            self._cancel.set()

    def resume(self) -> None:
        """解除急停，连续计数清零。调用方须已确认这次恢复经过了人的裁决。"""

        with self._lock:
            self._desktop.write_pace(PaceState(streak=0, stopped=False))
            self._cancel.clear()

    def _remaining(self) -> float:
        if self._last is None:
            return 0
        return INPUT_INTERVAL - (self._clock() - self._last)

    def over_budget(self) -> bool:
        """连续输入已经做满预算，下一次须经人确认。"""

        return self._desktop.read_pace().streak >= INPUT_BUDGET

    def abandon(self) -> None:
        """`wait_to_inject` 之后注入没有做成，把队首让出来。"""

        self._admit.release()

    def mark_injected(self) -> None:
        """这次输入已经注入：记下时刻，连续次数加一，并让出队首。"""

        try:
            self._last = self._clock()
            with self._lock:
                state = self._desktop.read_pace()
                # 这次是凭裁决越过预算的，连续计数从 1 重新计；否则只是又多了一次。
                streak = 1 if state.streak >= INPUT_BUDGET else state.streak + 1
                self._desktop.write_pace(PaceState(streak=streak, stopped=state.stopped))
        finally:
            self._admit.release()


def confirmation_reason(desktop: DesktopPort, tool: str) -> str | None:
    """hook 看得见的那一半。

    连续输入已达预算时，即使模型自报不危险也交给人。
    急停中的恢复交给人；急停中的输入工具不交——服务端会直接拒绝，交了人也执行不了。
    """

    state = desktop.read_pace()
    if tool == "resume" and state.stopped:
        return "急停之后恢复输入须经人确认"
    if state.stopped or tool not in INPUT_TOOLS or state.streak < INPUT_BUDGET:
        return None
    return f"连续输入动作已达预算（{INPUT_BUDGET} 次），继续须经人确认"


def budget_reason() -> str:
    return f"连续输入动作已达预算（{INPUT_BUDGET} 次）"


def budget_verdict() -> Verdict:
    """连续输入已达预算时并进危险判定的那条理由。凭据核验仍走裁决凭据，不另开一条门。"""

    return Verdict((budget_reason(),))


def budget_block_message() -> str:
    """没有裁决凭据、不能继续注入时回给模型的话。"""

    return (
        f"{budget_reason()}，须经人确认才能继续。"
        "请原样重新调用，由人在 Claude Code 中确认；"
        "把 dangerous 改成 true 不会让它自己通过"
    )


def _sleep_until_cancelled(seconds: float, cancel: threading.Event) -> None:
    cancel.wait(seconds)
