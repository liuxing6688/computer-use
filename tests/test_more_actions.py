"""双击、右键、拖拽、滚动、组合键，以及启动应用并等待它的窗口。

落点与按键都要过任务作用域和命中测试。作用域内的高危窗口先问人一次。
启动会打开高危窗口的程序同样先问一次；脚本宿主仍然不能启动。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from computer_use.action_log import Intercepted
from computer_use.actions import ActionError
from computer_use.desktop import ForegroundError, LaunchError, Rect
from computer_use.hook import decide
from computer_use.observation import ObservationError, Screenshots
from computer_use.scope import TaskScope
from computer_use.server import create_server
from computer_use.tools import (
    click,
    declare_scope,
    double_click,
    drag,
    get_scope,
    launch_app,
    observe_window,
    press_keys,
    right_click,
    scroll,
)

from .fake_desktop import FakeDesktop, window

_SYSTEM_CHORDS = (
    ["win"],
    ["win", "r"],
    ["alt", "tab"],
    ["alt", "shift", "tab"],
    ["alt", "escape"],
    ["ctrl", "escape"],
    ["ctrl", "shift", "escape"],
    ["ctrl", "alt", "delete"],
)


def _ready(
    desktop: FakeDesktop | None = None,
) -> tuple[FakeDesktop, Screenshots, TaskScope, str]:
    desktop = desktop or FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    return desktop, screenshots, scope, screenshot_id


def _frozen_clock() -> tuple[list[float], Callable[[], float], Callable[[float], None]]:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return now, clock, sleep


def test_双击按截图像素坐标落到屏幕上() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    result = double_click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开文件", dangerous=False
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.double_clicks == [(510, 220)]
    assert result["screen_point"] == {"x": 510, "y": 220}
    assert result["window"]["handle"] == 1


def test_右键按截图像素坐标落到屏幕上() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    right_click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开菜单", dangerous=False
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.right_clicks == [(510, 220)]


def test_拖拽的起点和终点都换算到屏幕() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    result = drag(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        10,
        20,
        30,
        40,
        intent="框选",
        dangerous=False,
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.drags == [(510, 220, 530, 240)]
    assert result["screen_point"] == {"x": 510, "y": 220}
    assert result["to"]["screen_point"] == {"x": 530, "y": 240}


def test_拖拽跨两扇窗口时先把起点窗口带到前台() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="另一扇", rect=Rect(500, 200, 40, 40)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1, 2])
    screenshot_id = _shot(desktop, screenshots, 1)

    drag(
        desktop, screenshots, scope, screenshot_id, 200, 100, 10, 10,
        intent="拖到旁边", dangerous=False,
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.drags == [(700, 300, 510, 210)]


def _单击(
    desktop: FakeDesktop, screenshots: Screenshots, scope: TaskScope, screenshot_id: str
) -> None:
    click(desktop, screenshots, scope, screenshot_id, 10, 20, intent="点一下", dangerous=False)


def _双击(
    desktop: FakeDesktop, screenshots: Screenshots, scope: TaskScope, screenshot_id: str
) -> None:
    double_click(desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开", dangerous=False)


def _右键(
    desktop: FakeDesktop, screenshots: Screenshots, scope: TaskScope, screenshot_id: str
) -> None:
    right_click(desktop, screenshots, scope, screenshot_id, 10, 20, intent="菜单", dangerous=False)


def _拖拽(
    desktop: FakeDesktop, screenshots: Screenshots, scope: TaskScope, screenshot_id: str
) -> None:
    drag(
        desktop, screenshots, scope, screenshot_id, 10, 20, 30, 40, intent="框选", dangerous=False
    )


def _滚动(
    desktop: FakeDesktop, screenshots: Screenshots, scope: TaskScope, screenshot_id: str
) -> None:
    scroll(desktop, screenshots, scope, screenshot_id, 10, 20, -1, intent="翻页", dangerous=False)


@pytest.mark.parametrize(
    "act",
    [_单击, _双击, _右键, _拖拽, _滚动],
    ids=["单击", "双击", "右键", "拖拽", "滚动"],
)
def test_窗口没能来到前台时不注入鼠标(
    act: Callable[[FakeDesktop, Screenshots, TaskScope, str], None],
) -> None:
    desktop, screenshots, scope, screenshot_id = _ready()
    desktop.focus_fails = True

    with pytest.raises(ForegroundError, match="前台"):
        act(desktop, screenshots, scope, screenshot_id)

    assert desktop.trace == [("focus", 1)]
    assert (
        desktop.clicks,
        desktop.double_clicks,
        desktop.right_clicks,
        desktop.drags,
        desktop.scrolls,
    ) == ([], [], [], [], [])


def test_拖拽终点在作用域外时不注入() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="别的窗口", process_name="other.exe", rect=Rect(500, 200, 40, 40)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240)),
        ]
    )
    _, screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="任务作用域之外"):
        drag(
            desktop, screenshots, scope, screenshot_id, 200, 100, 10, 10,
            intent="拖出去", dangerous=False,
        )

    assert desktop.trace == []
    assert desktop.drags == []


def test_落点被挡住时右键被拦截且不注入() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(500, 200, 80, 80)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240)),
        ]
    )
    _, screenshots, scope, screenshot_id = _ready(desktop)

    with pytest.raises(Intercepted, match="弹出的广告"):
        right_click(
            desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开菜单", dangerous=False
        )

    assert desktop.trace == []
    assert desktop.right_clicks == []


def test_高危窗口上的双击未经人确认不执行() -> None:
    desktop = FakeDesktop([window(handle=1, title="管理员: Windows PowerShell", process_name="pwsh.exe")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="终端") as intercepted:
        double_click(
            desktop, screenshots, scope, _shot(desktop, screenshots), 10, 10,
            intent="点进去", dangerous=False,
        )

    assert "dangerous" in str(intercepted.value)
    assert desktop.double_clicks == []


def test_拖到作用域内的高危窗口上时未经人确认不拖() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=2,
                title="文件资源管理器",
                process_name="explorer.exe",
                rect=Rect(500, 200, 40, 40),
            ),
            window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1, 2])
    screenshot_id = _shot(desktop, screenshots, 1)

    with pytest.raises(Intercepted, match="资源管理器") as intercepted:
        drag(
            desktop, screenshots, scope, screenshot_id, 200, 100, 10, 10,
            intent="拖进去", dangerous=False,
        )

    assert "dangerous" in str(intercepted.value)
    assert desktop.drags == []


def test_认不出进程的窗口不接受按键() -> None:
    desktop = FakeDesktop([window(handle=1, title="提权", process_name="")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="无法确认"):
        press_keys(
            desktop, screenshots, scope, _shot(desktop, screenshots, 1), ["enter"],
            intent="执行", dangerous=True,
        )

    assert desktop.chords == []


def test_高危窗口上的按键未经人确认不送出_确认后才按下() -> None:
    desktop = FakeDesktop([window(handle=1, title="命令提示符", process_name="cmd.exe")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _shot(desktop, screenshots, 1)
    arguments = {
        "screenshot_id": screenshot_id,
        "keys": ["enter"],
        "intent": "执行",
        "dangerous": True,
    }

    with pytest.raises(Intercepted, match="终端") as intercepted:
        press_keys(
            desktop, screenshots, scope, screenshot_id, ["enter"],
            intent="执行", dangerous=False,
        )

    assert "dangerous" in str(intercepted.value)
    assert desktop.chords == []

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        press_keys(
            desktop, screenshots, scope, screenshot_id, ["enter"],
            intent="执行", dangerous=True,
        )

    decision = decide(
        desktop, {"tool_name": "mcp__computer-use__press_keys", "tool_input": arguments}
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"

    press_keys(
        desktop, screenshots, scope, screenshot_id, ["enter"],
        intent="执行", dangerous=True,
    )

    assert desktop.chords == [("enter",)]


def test_滚动按凹口数注入_零格被拒绝() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    scroll(
        desktop, screenshots, scope, screenshot_id, 10, 20, -2, intent="往下翻", dangerous=False
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.scrolls == [(510, 220, -2)]
    with pytest.raises(ActionError, match="滚动"):
        scroll(
            desktop, screenshots, scope, screenshot_id, 10, 20, 0, intent="空滚", dangerous=False
        )
    assert desktop.trace == [("focus", 1)]
    assert desktop.scrolls == [(510, 220, -2)]


def test_不在截图内的坐标不注入右键() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    with pytest.raises(ObservationError):
        right_click(
            desktop, screenshots, scope, screenshot_id, 320, 0, intent="点外面", dangerous=False
        )

    assert desktop.right_clicks == []


def test_自报危险的双击未经裁决不执行() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    with pytest.raises(Intercepted, match="裁决"):
        double_click(
            desktop, screenshots, scope, screenshot_id, 10, 20, intent="删除", dangerous=True
        )

    assert desktop.double_clicks == []


def test_组合键先把目标窗口带到前台再按下() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    result = press_keys(
        desktop, screenshots, scope, screenshot_id, ["Ctrl", "S"], intent="保存", dangerous=False
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.chords == [("ctrl", "s")]
    assert result["keys"] == ["ctrl", "s"]
    assert result["window"]["handle"] == 1


def test_功能键可以单独按下() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    press_keys(desktop, screenshots, scope, screenshot_id, ["F5"], intent="刷新", dangerous=False)

    assert desktop.chords == [("f5",)]


@pytest.mark.parametrize("keys", _SYSTEM_CHORDS)
def test_任务作用域内且未判为危险时可以送出这些组合键(keys: list[str]) -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    result = press_keys(
        desktop, screenshots, scope, screenshot_id, keys, intent="切走", dangerous=False
    )

    assert desktop.trace == [("focus", 1)]
    assert desktop.chords == [tuple(keys)]
    assert result["keys"] == keys
    assert result["window"]["handle"] == 1


@pytest.mark.parametrize("keys", _SYSTEM_CHORDS)
def test_落到任务作用域外的这些组合键不执行(keys: list[str]) -> None:
    desktop = FakeDesktop(
        [window(handle=1), window(handle=2, title="计算器", rect=Rect(900, 0, 100, 100))]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [2])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        press_keys(
            desktop, screenshots, scope, _shot(desktop, screenshots, 1), keys,
            intent="切走", dangerous=False,
        )

    assert desktop.trace == []
    assert desktop.chords == []


@pytest.mark.parametrize("keys", _SYSTEM_CHORDS)
def test_自报危险的这些组合键未经裁决不执行(keys: list[str]) -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        press_keys(
            desktop, screenshots, scope, screenshot_id, keys, intent="切走", dangerous=True
        )

    assert desktop.trace == []
    assert desktop.chords == []


def test_不认识的按键被拒绝_不注入() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()

    with pytest.raises(ActionError, match="volume"):
        press_keys(
            desktop, screenshots, scope, screenshot_id, ["volume"], intent="调音量", dangerous=False
        )

    assert desktop.chords == []


def test_作用域外的窗口不接受按键() -> None:
    desktop = FakeDesktop(
        [window(handle=1), window(handle=2, title="计算器", rect=Rect(900, 0, 100, 100))]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [2])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        press_keys(
            desktop, screenshots, scope, _shot(desktop, screenshots, 1), ["enter"],
            intent="确认", dangerous=False,
        )

    assert desktop.chords == []


def test_窗口没能来到前台时不注入按键() -> None:
    desktop, screenshots, scope, screenshot_id = _ready()
    desktop.focus_fails = True

    with pytest.raises(ForegroundError):
        press_keys(
            desktop, screenshots, scope, screenshot_id, ["a"], intent="输入", dangerous=False
        )

    assert desktop.chords == []


def test_启动应用后等到它的新窗口_作用域不变() -> None:
    desktop = FakeDesktop()
    scope = TaskScope()
    desktop.spawn = [
        window(handle=9, title="无标题 - 记事本", process_name="notepad.exe", process_id=4242)
    ]
    _, clock, sleep = _frozen_clock()

    result = launch_app(
        desktop, "notepad", intent="打开记事本", dangerous=False, clock=clock, sleep=sleep
    )

    assert desktop.launched == ["notepad.exe"]
    assert result == {
        "window": {
            "handle": 9,
            "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
            "process_name": "notepad.exe",
        },
        "process_id": 4242,
        "change": {
            "foreground_changed": False,
            "new_windows": [
                {
                    "handle": 9,
                    "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
                    "process_name": "notepad.exe",
                }
            ],
        },
    }
    assert get_scope(scope) == []


def test_新窗口进程号不同但可执行文件名相同也算它出现了() -> None:
    desktop = FakeDesktop()
    desktop.spawn = [
        window(handle=9, title="记事本", process_name="notepad.exe", process_id=7)
    ]
    _, clock, sleep = _frozen_clock()

    result = launch_app(
        desktop, "notepad", intent="打开记事本", dangerous=False, clock=clock, sleep=sleep
    )

    assert result["window"]["handle"] == 9


def test_已经在的窗口不算这次启动出来的() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", process_name="notepad.exe", process_id=7)]
    )
    _, clock, sleep = _frozen_clock()

    with pytest.raises(ActionError, match="超时") as error:
        launch_app(
            desktop, "notepad", intent="再开一个", dangerous=False,
            timeout=1, clock=clock, sleep=sleep,
        )

    assert "已经在运行" in str(error.value)
    assert "句柄 1" not in str(error.value)


def test_超时错误点名期间新出现的不相干窗口() -> None:
    desktop = FakeDesktop()
    now, clock, _sleep = _frozen_clock()

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] == seconds:
            desktop.add_window(
                window(handle=4, title="弹出的广告", process_name="ad.exe", process_id=8)
            )

    with pytest.raises(ActionError, match="超时") as error:
        launch_app(
            desktop, "notepad", intent="打开记事本", dangerous=False,
            timeout=1, clock=clock, sleep=sleep,
        )

    message = str(error.value)
    assert "<untrusted-screen>弹出的广告</untrusted-screen>" in message
    assert "ad.exe" in message
    assert desktop.launched == ["notepad.exe"]


def test_稍晚出现的窗口赶在超时前被等到() -> None:
    desktop = FakeDesktop()
    now, clock, _sleep = _frozen_clock()

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 0.4:
            desktop.add_window(
                window(handle=9, title="无标题 - 记事本", process_name="notepad.exe", process_id=4242)
            )

    result = launch_app(
        desktop, "notepad", intent="打开记事本", dangerous=False,
        timeout=1, clock=clock, sleep=sleep,
    )

    assert result["window"]["handle"] == 9
    assert now[0] < 1


@pytest.mark.parametrize(
    ("app", "reason"),
    [
        ("cmd", "终端"),
        ("powershell", "终端"),
        ("explorer.exe", "资源管理器"),
        ("SystemSettings.exe", "系统设置"),
    ],
)
def test_启动高危程序未经人确认不启动(app: str, reason: str) -> None:
    desktop = FakeDesktop()

    with pytest.raises(Intercepted, match=reason) as intercepted:
        launch_app(desktop, app, intent="跑一下", dangerous=False)

    assert "dangerous" in str(intercepted.value)
    assert desktop.launched == []


def test_人确认后才启动会打开高危窗口的程序() -> None:
    desktop = FakeDesktop()
    desktop.spawn = [
        window(handle=9, title="命令提示符", process_name="cmd.exe", process_id=4242)
    ]
    arguments = {"app": "cmd", "intent": "开一个终端", "dangerous": True}
    _, clock, sleep = _frozen_clock()

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        launch_app(
            desktop, "cmd", intent="开一个终端", dangerous=True, clock=clock, sleep=sleep
        )

    assert desktop.launched == []
    decision = decide(
        desktop, {"tool_name": "mcp__computer-use__launch_app", "tool_input": arguments}
    )
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"

    result = launch_app(
        desktop, "cmd", intent="开一个终端", dangerous=True, clock=clock, sleep=sleep
    )

    assert desktop.launched == ["cmd.exe"]
    assert result["window"]["handle"] == 9
    assert desktop.dialogs == []


@pytest.mark.parametrize("app", ["wscript", "mshta.exe"])
def test_不启动脚本宿主(app: str) -> None:
    desktop = FakeDesktop()
    arguments = {"app": app, "intent": "跑一下", "dangerous": True}
    decide(desktop, {"tool_name": "mcp__computer-use__launch_app", "tool_input": arguments})

    with pytest.raises(Intercepted, match="脚本宿主"):
        launch_app(desktop, app, intent="跑一下", dangerous=True)

    assert desktop.launched == []


@pytest.mark.parametrize("app", ["notepad.bat", "notepad.exe /a", ""])
def test_只能启动不带参数的_exe(app: str) -> None:
    desktop = FakeDesktop()

    with pytest.raises(ActionError):
        launch_app(desktop, app, intent="打开", dangerous=False)

    assert desktop.launched == []


def test_带路径的_exe_按文件名认定() -> None:
    desktop = FakeDesktop()
    desktop.spawn = [
        window(handle=9, title="无标题 - 记事本", process_name="notepad.exe", process_id=4242)
    ]
    _, clock, sleep = _frozen_clock()

    launch_app(
        desktop,
        r"C:\Windows\System32\notepad.exe",
        intent="打开记事本",
        dangerous=False,
        clock=clock,
        sleep=sleep,
    )

    assert desktop.launched == [r"C:\Windows\System32\notepad.exe"]


def test_程序启动失败时把原因交回去_没有进程() -> None:
    desktop = FakeDesktop()
    desktop.launch_error = "系统找不到指定的文件。"

    with pytest.raises(LaunchError, match="找不到"):
        launch_app(desktop, "missing", intent="打开", dangerous=False)

    assert desktop.launched == ["missing.exe"]


def test_急停之后不再启动() -> None:
    from computer_use.desktop import PaceState

    desktop = FakeDesktop()
    desktop.write_pace(PaceState(stopped=True))

    with pytest.raises(Intercepted, match="急停"):
        launch_app(desktop, "notepad", intent="打开记事本", dangerous=False)

    assert desktop.launched == []


def test_hook_把自报危险的双击交给人() -> None:
    desktop = FakeDesktop()
    arguments = {
        "screenshot_id": "shot-1",
        "x": 1,
        "y": 2,
        "intent": "删除",
        "dangerous": True,
    }

    output = decide(desktop, {"tool_name": "mcp__computer-use__double_click", "tool_input": arguments})

    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_双击右键按键与启动经由_MCP_调用_成功拦截都记入日志() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(100, 50, 320, 240))]
    )
    desktop.spawn = [
        window(handle=9, title="计算器", process_name="calc.exe", process_id=4242)
    ]

    async def call() -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:
        async with Client(create_server(desktop)) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {
                "double_click",
                "right_click",
                "drag",
                "scroll",
                "press_keys",
                "launch_app",
            } <= names
            await client.call_tool("declare_scope", {"handles": [1]})
            observed = await client.call_tool("observe_window", {"handle": 1})
            screenshot_id = observed.data["metadata"]["screenshot_id"]
            clicked = await client.call_tool(
                "double_click",
                {"screenshot_id": screenshot_id, "x": 10, "y": 20, "intent": "打开", "dangerous": False},
            )
            pressed = await client.call_tool(
                "press_keys",
                {
                    "screenshot_id": screenshot_id,
                    "keys": ["alt", "tab"],
                    "intent": "切走",
                    "dangerous": False,
                },
            )
            with pytest.raises(ToolError, match="没有经过人的裁决") as refused:
                await client.call_tool(
                    "press_keys",
                    {
                        "screenshot_id": screenshot_id,
                        "keys": ["alt", "tab"],
                        "intent": "切走",
                        "dangerous": True,
                    },
                )
            launched = await client.call_tool(
                "launch_app", {"app": "calc", "intent": "打开计算器", "dangerous": False}
            )
            scope = await client.call_tool("get_scope", {})
            assert scope.data == [
                {
                    "handle": 1,
                    "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
                    "process_name": "notepad.exe",
                }
            ]
            return dict(clicked.data), dict(pressed.data), str(refused.value), dict(launched.data)

    clicked, pressed, refusal, launched = asyncio.run(call())

    assert clicked["screen_point"] == {"x": 110, "y": 70}
    assert desktop.double_clicks == [(110, 70)]
    assert desktop.chords == [("alt", "tab")]
    assert pressed["keys"] == ["alt", "tab"]
    assert "交给系统" not in refusal
    assert "离开任务作用域" not in refusal
    assert launched["window"]["handle"] == 9
    assert desktop.launched == ["calc.exe"]
    by_tool = {record["tool"]: record for record in desktop.action_log()}
    assert (by_tool["double_click"]["verdict"], by_tool["double_click"]["outcome"]) == (
        "allowed",
        "succeeded",
    )
    assert by_tool["double_click"]["intent"] == "打开"
    press_records = [record for record in desktop.action_log() if record["tool"] == "press_keys"]
    assert [(record["verdict"], record["outcome"]) for record in press_records] == [
        ("allowed", "succeeded"),
        ("intercepted", "not_executed"),
    ]
    assert [record["target"]["keys"] for record in press_records] == [
        ["alt", "tab"],
        ["alt", "tab"],
    ]
    assert (by_tool["launch_app"]["verdict"], by_tool["launch_app"]["outcome"]) == (
        "allowed",
        "succeeded",
    )
    assert by_tool["launch_app"]["evidence"] is None


def _shot(desktop: FakeDesktop, screenshots: Screenshots, handle: int = 1) -> str:
    screenshot_id: str = observe_window(desktop, screenshots, handle).metadata["screenshot_id"]
    return screenshot_id
