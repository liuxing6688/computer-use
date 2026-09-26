"""输入工具成功之后返回变化说明：前台窗口有没有变、有没有新窗口。

被拦截或失败时不返回这份说明，结果也不会被说成执行成功。
"""

from __future__ import annotations

from typing import Callable

import pytest

from computer_use.action_log import Intercepted
from computer_use.actions import ActionError
from computer_use.desktop import ForegroundError, Rect
from computer_use.observation import Screenshots
from computer_use.scope import TaskScope
from computer_use.tools import (
    click,
    declare_scope,
    double_click,
    drag,
    launch_app,
    observe_window,
    press_keys,
    right_click,
    scroll,
    type_text,
)

from .fake_desktop import FakeDesktop, window


def test_点击成功且桌面无变化时_说明前台没变也没有新窗口() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    desktop.foreground = 1
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]

    result = click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="点一下", dangerous=False
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_点击把目标窗口带到前台时_说明前台变了且没有新窗口() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]

    result = click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="点一下", dangerous=False
    )

    assert desktop.foreground == 1
    assert result["change"] == {"foreground_changed": True, "new_windows": []}


def test_点击后前台窗口换成另一个时_说明前台变了且没有新窗口() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=2,
                title="计算器",
                process_name="CalculatorApp.exe",
                rect=Rect(0, 0, 100, 100),
            ),
            window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240)),
        ]
    )
    desktop.foreground = 1
    desktop.after_input(foreground=2)
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]

    result = click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="点一下", dangerous=False
    )

    assert result["change"] == {"foreground_changed": True, "new_windows": []}


def test_点击后出现看得见的新窗口时_说明点出它们且前台没变() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    desktop.foreground = 1
    desktop.after_input(
        windows=[
            window(handle=9, title="另存为", process_name="notepad.exe"),
            window(handle=8, title=" ", process_name="notepad.exe"),
            window(handle=7, title="藏着", process_name="notepad.exe", is_visible=False),
            window(handle=6, title="缩着", process_name="notepad.exe", is_minimized=True),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]

    result = click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开保存", dangerous=False
    )

    assert result["change"] == {
        "foreground_changed": False,
        "new_windows": [
            {
                "handle": 9,
                "title": "<untrusted-screen>另存为</untrusted-screen>",
                "process_name": "notepad.exe",
            },
            {
                "handle": 8,
                "title": "<untrusted-screen> </untrusted-screen>",
                "process_name": "notepad.exe",
            },
        ],
    }


def _clickable() -> tuple[FakeDesktop, Screenshots, TaskScope, str]:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    desktop.foreground = 1
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]
    return desktop, screenshots, scope, screenshot_id


def test_双击成功且桌面无变化时_也返回变化说明() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

    result = double_click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开", dangerous=False
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_右键成功且桌面无变化时_也返回变化说明() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

    result = right_click(
        desktop, screenshots, scope, screenshot_id, 10, 20, intent="打开菜单", dangerous=False
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_拖拽成功且桌面无变化时_也返回变化说明() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

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

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_滚动成功且桌面无变化时_也返回变化说明() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

    result = scroll(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        10,
        20,
        -3,
        intent="向下翻",
        dangerous=False,
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_按键成功且前台本来就是目标窗口时_说明前台没变也没有新窗口() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

    result = press_keys(
        desktop, screenshots, scope, screenshot_id, ["ctrl", "s"], intent="保存", dangerous=False
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_按键把目标窗口带到前台时_说明前台变了且没有新窗口() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()
    desktop.foreground = None

    result = press_keys(
        desktop, screenshots, scope, screenshot_id, ["ctrl", "s"], intent="保存", dangerous=False
    )

    assert result["change"] == {"foreground_changed": True, "new_windows": []}


def test_输入文本成功且前台本来就是目标窗口时_说明前台没变也没有新窗口() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()

    result = type_text(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        "你好",
        intent="填写",
        dangerous=False,
    )

    assert result["change"] == {"foreground_changed": False, "new_windows": []}


def test_启动应用成功后_说明点出新窗口且前台没变() -> None:
    desktop = FakeDesktop()
    desktop.spawn = [
        window(handle=9, title="无标题 - 记事本", process_name="notepad.exe", process_id=4242)
    ]
    _, clock, sleep = _frozen_clock()

    result = launch_app(
        desktop, "notepad", intent="打开记事本", dangerous=False, clock=clock, sleep=sleep
    )

    assert result["change"] == {
        "foreground_changed": False,
        "new_windows": [
            {
                "handle": 9,
                "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
                "process_name": "notepad.exe",
            }
        ],
    }


def test_启动后新窗口来到前台时_说明前台也变了() -> None:
    desktop = FakeDesktop()
    desktop.spawn = [
        window(handle=9, title="无标题 - 记事本", process_name="notepad.exe", process_id=4242)
    ]
    desktop.after_input(foreground=9)
    _, clock, sleep = _frozen_clock()

    result = launch_app(
        desktop, "notepad", intent="打开记事本", dangerous=False, clock=clock, sleep=sleep
    )

    assert result["change"] == {
        "foreground_changed": True,
        "new_windows": [
            {
                "handle": 9,
                "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
                "process_name": "notepad.exe",
            }
        ],
    }


def test_输入文本把目标窗口带到前台时_说明前台变了且没有新窗口() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()
    desktop.foreground = None

    result = type_text(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        "你好",
        intent="填写",
        dangerous=False,
    )

    assert result["change"] == {"foreground_changed": True, "new_windows": []}


def test_输入文本弹出新窗口时_说明点出它且前台没变() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()
    desktop.after_input(windows=[window(handle=9, title="字体", process_name="notepad.exe")])

    result = type_text(
        desktop,
        screenshots,
        scope,
        screenshot_id,
        "你好",
        intent="填写",
        dangerous=False,
    )

    assert result["change"] == {
        "foreground_changed": False,
        "new_windows": [
            {
                "handle": 9,
                "title": "<untrusted-screen>字体</untrusted-screen>",
                "process_name": "notepad.exe",
            }
        ],
    }


def test_点击被拦截时不返回执行成功的变化说明() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(200, 200, 80, 80)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(200, 200, 320, 240)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id: str = observe_window(desktop, screenshots, 1).metadata["screenshot_id"]

    with pytest.raises(Intercepted, match="任务作用域之外"):
        click(
            desktop, screenshots, scope, screenshot_id, 10, 10, intent="点广告", dangerous=False
        )

    assert desktop.clicks == []


def test_启动超时不算执行成功() -> None:
    desktop = FakeDesktop()
    _, clock, sleep = _frozen_clock()

    with pytest.raises(ActionError, match="超时"):
        launch_app(
            desktop,
            "notepad",
            intent="打开记事本",
            dangerous=False,
            timeout=1,
            clock=clock,
            sleep=sleep,
        )


def test_输入文本没能来到前台时不算执行成功() -> None:
    desktop, screenshots, scope, screenshot_id = _clickable()
    desktop.focus_fails = True

    with pytest.raises(ForegroundError, match="前台"):
        type_text(
            desktop,
            screenshots,
            scope,
            screenshot_id,
            "你好",
            intent="填写",
            dangerous=False,
        )

    assert desktop.pasted == []


def _frozen_clock() -> tuple[list[float], Callable[[], float], Callable[[float], None]]:
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    return now, clock, sleep
