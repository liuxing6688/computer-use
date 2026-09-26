"""限速、预算与急停：输入动作慢下来，连续次数到顶要人确认，热键能立刻停。"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from computer_use.action_log import Intercepted
from computer_use.desktop import PaceState, Rect
from computer_use.hook import decide
from computer_use.interception import refer_to_human
from computer_use.observation import Screenshots
from computer_use.pace import INPUT_BUDGET, INPUT_INTERVAL, Pace, Sleeper
from computer_use.scope import TaskScope
from computer_use.server import create_server
from computer_use.tools import (
    click,
    declare_scope,
    get_scope,
    list_windows,
    observe_window,
    resume,
    type_text,
    zoom,
)

from .fake_desktop import FakeDesktop, window


class ManualClock:
    """时间只在限速要求等待时往前走，测试不必真的睡。"""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float, cancel: object) -> None:
        del cancel
        self.now += seconds


def test_相邻输入动作之间至少隔开最小间隔() -> None:
    desktop, screenshots, scope, screenshot_id, pace, clock = _ready()

    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)

    assert clock.now == 0
    assert desktop.clicks == [(10, 10)]

    click(desktop, screenshots, scope, screenshot_id, 20, 20, intent="再点一下", dangerous=False, pace=pace)

    assert clock.now == INPUT_INTERVAL
    assert desktop.clicks == [(10, 10), (20, 20)]


def test_连续输入超过预算时停下_只读工具不把连续计数清零() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()

    for n in range(INPUT_BUDGET):
        click(
            desktop, screenshots, scope, screenshot_id, 10, n,
            intent="点一下", dangerous=False, pace=pace,
        )
        list_windows(desktop)

    with pytest.raises(Intercepted, match="预算"):
        click(
            desktop, screenshots, scope, screenshot_id, 10, 30,
            intent="点一下", dangerous=False, pace=pace,
        )

    assert len(desktop.clicks) == INPUT_BUDGET


def test_把_dangerous_改成_true_不能跳过预算() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    _fill_budget(desktop, screenshots, scope, screenshot_id, pace)

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        click(
            desktop, screenshots, scope, screenshot_id, 10, 30,
            intent="点一下", dangerous=True, pace=pace,
        )

    assert len(desktop.clicks) == INPUT_BUDGET


def test_经人确认后连续计数从一开始重新计() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    _fill_budget(desktop, screenshots, scope, screenshot_id, pace)
    arguments = {
        "screenshot_id": screenshot_id,
        "x": 10,
        "y": 40,
        "intent": "继续点",
        "dangerous": False,
    }

    decide(desktop, _payload(arguments))
    click(desktop, screenshots, scope, screenshot_id, 10, 40, intent="继续点", dangerous=False, pace=pace)
    for n in range(INPUT_BUDGET - 1):
        click(
            desktop, screenshots, scope, screenshot_id, 30, n,
            intent="预算内的点击", dangerous=False, pace=pace,
        )

    with pytest.raises(Intercepted, match="预算"):
        click(
            desktop, screenshots, scope, screenshot_id, 30, 40,
            intent="又到预算了", dangerous=False, pace=pace,
        )

    assert len(desktop.clicks) == INPUT_BUDGET * 2


def test_hook_在连续输入已达预算时把调用交给人_未达预算则不交() -> None:
    desktop = FakeDesktop()
    arguments = {"screenshot_id": "shot-1", "x": 1, "y": 2, "intent": "点一下", "dangerous": False}

    desktop.write_pace(PaceState(streak=INPUT_BUDGET - 1, stopped=False))
    assert decide(desktop, _payload(arguments)) is None
    assert desktop.tickets == {}

    desktop.write_pace(PaceState(streak=INPUT_BUDGET, stopped=False))
    output = decide(desktop, _payload(arguments))

    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "预算" in output["hookSpecificOutput"]["permissionDecisionReason"]
    assert desktop.tickets


def test_文本输入与点击算相邻的输入动作() -> None:
    desktop, screenshots, scope, screenshot_id, pace, clock = _ready()

    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)
    type_text(
        desktop, screenshots, scope, screenshot_id, "你好",
        intent="填写", dangerous=False, pace=pace,
    )

    assert clock.now == INPUT_INTERVAL
    assert desktop.pasted == ["你好"]
    assert desktop.read_pace().streak == 2


def test_重叠的输入仍按最小间隔排开() -> None:
    desktop, screenshots, scope, screenshot_id, pace, clock = _ready()
    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)

    def run(y: int) -> None:
        click(
            desktop, screenshots, scope, screenshot_id, 10, y,
            intent="同时点", dangerous=False, pace=pace,
        )

    first, second = threading.Thread(target=run, args=(20,)), threading.Thread(target=run, args=(30,))
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert len(desktop.clicks) == 3
    assert clock.now == 2 * INPUT_INTERVAL


def test_重叠的两次输入不会一起越过预算() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    _fill_budget(desktop, screenshots, scope, screenshot_id, pace, INPUT_BUDGET - 1)
    errors: list[BaseException] = []

    def run(y: int) -> None:
        try:
            click(
                desktop, screenshots, scope, screenshot_id, 10, y,
                intent="同时点", dangerous=False, pace=pace,
            )
        except BaseException as error:
            errors.append(error)

    first, second = threading.Thread(target=run, args=(40,)), threading.Thread(target=run, args=(50,))
    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert len(desktop.clicks) == INPUT_BUDGET
    assert len(errors) == 1
    assert isinstance(errors[0], Intercepted)
    assert "预算" in str(errors[0])


def test_急停中止正在等待的输入并清空尚未注入的队列() -> None:
    sleeper = _BlockingSleep()
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready(sleep=sleeper)
    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)
    errors: list[BaseException] = []

    def run(y: int) -> None:
        try:
            click(
                desktop, screenshots, scope, screenshot_id, 10, y,
                intent="排队的点击", dangerous=False, pace=pace,
            )
        except BaseException as error:
            errors.append(error)

    first, second = threading.Thread(target=run, args=(20,)), threading.Thread(target=run, args=(30,))
    first.start()
    second.start()
    # 一次只放行一个输入：排在后面的卡在队首锁上，进不了这次等待。
    assert sleeper.arrived.acquire(timeout=2)

    pace.stop()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, Intercepted) and "急停" in str(error) for error in errors)
    assert desktop.clicks == [(10, 10)]


def test_急停会停住还没打完的逐字符输入() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    desktop.clipboard_read_fails = True

    def stop_after_first(character: str) -> None:
        FakeDesktop.type_character(desktop, character)
        pace.stop()

    desktop.type_character = stop_after_first  # type: ignore[method-assign]

    with pytest.raises(Intercepted, match="急停"):
        type_text(
            desktop, screenshots, scope, screenshot_id, "你好",
            intent="填写", dangerous=False, pace=pace,
        )

    assert desktop.characters == ["你"]
    assert desktop.read_pace().streak == 0


def test_急停后输入工具拒绝_只读工具与声明作用域仍可用_裁决凭据也不能把点击放行() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    arguments: dict[str, Any] = {
        "screenshot_id": screenshot_id, "x": 10, "y": 10, "intent": "点一下", "dangerous": True,
    }
    refer_to_human(desktop, "click", arguments)
    pace.stop()

    listed = list_windows(desktop)
    observed = observe_window(desktop, screenshots, 1)
    zoomed = zoom(screenshots, observed.metadata["screenshot_id"], Rect(0, 0, 20, 20))
    assert listed[0]["handle"] == 1
    assert observed.metadata["window"]["handle"] == 1
    assert zoomed.metadata["window"]["handle"] == 1
    assert get_scope(scope)[0]["handle"] == 1
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="急停"):
        click(
            desktop, screenshots, scope, screenshot_id, 10, 10,
            intent="点一下", dangerous=True, pace=pace,
        )
    with pytest.raises(Intercepted, match="急停"):
        type_text(
            desktop, screenshots, scope, screenshot_id, "你好",
            intent="填写", dangerous=False, pace=pace,
        )

    assert desktop.clicks == []
    assert desktop.pasted == []
    assert desktop.tickets


def test_显式恢复须经人确认_确认后输入工具重新可用() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    pace.stop()

    with pytest.raises(Intercepted, match="确认"):
        resume(desktop, pace)
    decide(desktop, _payload({}, tool="resume"))
    assert resume(desktop, pace) == "已恢复，输入工具可以继续使用"

    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)

    assert desktop.clicks == [(10, 10)]


def test_急停后未经人确认就调用_resume_输入工具仍然拒绝() -> None:
    desktop, screenshots, scope, screenshot_id, pace, _clock = _ready()
    pace.stop()

    with pytest.raises(Intercepted, match="确认"):
        resume(desktop, pace)

    with pytest.raises(Intercepted, match="急停"):
        click(
            desktop, screenshots, scope, screenshot_id, 10, 10,
            intent="点一下", dangerous=False, pace=pace,
        )

    assert desktop.clicks == []
    assert desktop.read_pace().stopped


def test_未急停时调用_resume_不改变当前状态() -> None:
    desktop, screenshots, scope, screenshot_id, pace, clock = _ready()
    click(desktop, screenshots, scope, screenshot_id, 10, 10, intent="点一下", dangerous=False, pace=pace)
    before = desktop.read_pace()

    assert resume(desktop, pace) == "没有处于急停"

    assert desktop.read_pace() == before
    click(desktop, screenshots, scope, screenshot_id, 20, 20, intent="再点一下", dangerous=False, pace=pace)
    assert clock.now == INPUT_INTERVAL
    assert desktop.clicks == [(10, 10), (20, 20)]
    assert desktop.read_pace() == PaceState(streak=2, stopped=False)


def test_hook_急停时不把输入工具交给人_恢复才交给人() -> None:
    desktop = FakeDesktop()
    arguments = {"screenshot_id": "shot-1", "x": 1, "y": 2, "intent": "点一下", "dangerous": True}
    desktop.write_pace(PaceState(streak=INPUT_BUDGET, stopped=True))

    assert decide(desktop, _payload(arguments)) is None
    assert desktop.tickets == {}

    output = decide(desktop, _payload({}, tool="resume"))

    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "急停" in output["hookSpecificOutput"]["permissionDecisionReason"]
    desktop.write_pace(PaceState(streak=0, stopped=False))
    assert decide(desktop, _payload({}, tool="resume")) is None


def test_服务注册的全局热键按下后输入被拒绝_确认恢复后才能再点() -> None:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])

    async def call() -> None:
        async with Client(create_server(desktop)) as client:
            desktop.press_stop_hotkey()
            listed = await client.call_tool("list_windows", {})
            assert listed.data[0]["handle"] == 1
            with pytest.raises(ToolError, match="急停"):
                await client.call_tool(
                    "click",
                    {
                        "screenshot_id": "shot-不存在",
                        "x": 1,
                        "y": 1,
                        "intent": "点一下",
                        "dangerous": False,
                    },
                )
            with pytest.raises(ToolError, match="确认"):
                await client.call_tool("resume", {})
            decide(desktop, _payload({}, tool="resume"))
            restored = await client.call_tool("resume", {})
            assert restored.data == "已恢复，输入工具可以继续使用"
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            clicked = await client.call_tool(
                "click",
                {
                    "screenshot_id": observed.data["screenshot_id"],
                    "x": 10,
                    "y": 10,
                    "intent": "点一下",
                    "dangerous": False,
                },
            )
            assert clicked.data["screen_point"] == {"x": 10, "y": 10}

    asyncio.run(call())

    assert desktop.clicks == [(10, 10)]
    assert any(record["tool"] == "click" and record["outcome"] == "not_executed" for record in desktop.action_log())


class _BlockingSleep:
    def __init__(self) -> None:
        self.arrived = threading.Semaphore(0)

    def __call__(self, seconds: float, cancel: threading.Event) -> None:
        del seconds
        self.arrived.release()
        cancel.wait()


def _fill_budget(
    desktop: FakeDesktop,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    pace: Pace,
    count: int = INPUT_BUDGET,
) -> None:
    for n in range(count):
        click(
            desktop, screenshots, scope, screenshot_id, 10, n,
            intent="点一下", dangerous=False, pace=pace,
        )


def _payload(arguments: dict[str, Any], tool: str = "click") -> dict[str, Any]:
    return {"tool_name": f"mcp__computer-use__{tool}", "tool_input": arguments}


def _ready(
    sleep: Sleeper | None = None,
) -> tuple[FakeDesktop, Screenshots, TaskScope, str, Pace, ManualClock]:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    clock = ManualClock()
    pace = Pace(desktop, clock=clock.monotonic, sleep=sleep or clock.sleep)
    return desktop, screenshots, scope, screenshot_id, pace, clock
