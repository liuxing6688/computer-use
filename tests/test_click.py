"""任务作用域与点击：按截图像素坐标点击，落点须在作用域内。

落在作用域内的高危窗口上时不直接封死，未经人确认不执行，确认后才执行。
"""

from __future__ import annotations

from typing import Any

import pytest

from computer_use.action_log import Intercepted
from computer_use.desktop import Rect
from computer_use.hook import decide
from computer_use.observation import ObservationError, Screenshots
from computer_use.scope import ScopeError, TaskScope
from computer_use import tools
from computer_use.tools import declare_scope, get_scope, observe_window, zoom

from .fake_desktop import FakeDesktop, window


def click(
    desktop: FakeDesktop,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
) -> dict[str, Any]:
    """一次模型自报不危险、落点附近也没有高危词的点击；危险判定见 `test_danger.py`。"""

    return tools.click(
        desktop, screenshots, scope, screenshot_id, x, y, intent="点一下", dangerous=False
    )


def test_声明的任务作用域可被查询() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, title="无标题 - 记事本", process_name="notepad.exe"),
            window(handle=2, title="计算器", process_name="CalculatorApp.exe"),
            window(handle=3, title="不相干的窗口"),
        ]
    )
    scope = TaskScope()

    declared = declare_scope(desktop, scope, [1, 2])

    expected = [
        {"handle": 1, "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>", "process_name": "notepad.exe"},
        {"handle": 2, "title": "<untrusted-screen>计算器</untrusted-screen>", "process_name": "CalculatorApp.exe"},
    ]
    assert declared == expected
    assert get_scope(scope) == expected


def test_尚未声明时任务作用域为空() -> None:
    assert get_scope(TaskScope()) == []


def test_再次声明替换原有的任务作用域() -> None:
    desktop = FakeDesktop([window(handle=1), window(handle=2, title="计算器")])
    scope = TaskScope()
    declare_scope(desktop, scope, [1])

    declare_scope(desktop, scope, [2])

    assert [w["handle"] for w in get_scope(scope)] == [2]


@pytest.mark.parametrize(
    "handles",
    [[1, 404], [1, 2], []],
    ids=["不存在的窗口", "最小化的窗口", "空作用域"],
)
def test_拒绝声明含不可操作窗口的作用域_原作用域不变(handles: list[int]) -> None:
    desktop = FakeDesktop(
        [
            window(handle=1),
            window(handle=2, title="缩到任务栏", is_minimized=True),
            window(handle=3, title="原来的", rect=Rect(0, 0, 10, 10)),
        ]
    )
    scope = TaskScope()
    declare_scope(desktop, scope, [3])

    with pytest.raises(ScopeError):
        declare_scope(desktop, scope, handles)

    assert [w["handle"] for w in get_scope(scope)] == [3]


def _observed(desktop: FakeDesktop, screenshots: Screenshots, handle: int) -> str:
    screenshot_id: str = observe_window(desktop, screenshots, handle).metadata["screenshot_id"]
    return screenshot_id


def test_点击作用域内的窗口_按截图像素坐标落到屏幕上() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="无标题 - 记事本", rect=Rect(500, 200, 320, 240))]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    result = click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 20)

    assert desktop.clicks == [(510, 220)]
    assert result == {
        "window": {
            "handle": 1,
            "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
            "process_name": "notepad.exe",
        },
        "screen_point": {"x": 510, "y": 220},
        "change": {"foreground_changed": False, "new_windows": []},
    }


def test_125_缩放下被服务端缩小的截图_点击仍落在模型指定的物理像素上() -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(100, 50, 3136, 1000))], dpi_scale=1.25)
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    observed = observe_window(desktop, screenshots, 1)
    assert (observed.metadata["scale"], observed.metadata["dpi_scale"]) == (0.5, 1.25)

    click(desktop, screenshots, scope, observed.metadata["screenshot_id"], 10, 20)

    # 截图像素 (10, 20) 覆盖物理像素 [120, 122) × [90, 92)，取其中心所落的那个。
    assert desktop.clicks == [(121, 91)]


def test_放大图上的坐标同样换算到屏幕() -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(100, 50, 3136, 1000))])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    zoomed = zoom(screenshots, _observed(desktop, screenshots, 1), Rect(990, 190, 70, 60))

    click(desktop, screenshots, scope, zoomed.metadata["screenshot_id"], 20, 20)

    assert desktop.clicks == [(2100, 450)]


@pytest.mark.parametrize(
    ("x", "y"), [(320, 0), (0, 240), (-1, 5)], ids=["越出右边", "越出下边", "越出左边"]
)
def test_不在截图范围内的坐标被拒绝_不注入点击(x: int, y: int) -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(0, 0, 320, 240))])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(ObservationError):
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), x, y)

    assert desktop.clicks == []


def test_尚未声明任务作用域时点击被拦截() -> None:
    desktop = FakeDesktop([window(handle=1)])
    screenshots = Screenshots()

    with pytest.raises(Intercepted, match="尚未声明任务作用域"):
        click(desktop, screenshots, TaskScope(), _observed(desktop, screenshots, 1), 10, 10)

    assert desktop.clicks == []


def test_落点被作用域外的窗口挡住时点击被拦截_理由指明落点窗口() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="弹出的广告", process_name="ad.exe", rect=Rect(0, 0, 100, 100)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(0, 0, 800, 600)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)

    with pytest.raises(Intercepted, match="任务作用域之外") as intercepted:
        click(desktop, screenshots, scope, screenshot_id, 50, 50)

    assert "<untrusted-screen>弹出的广告</untrusted-screen>" in str(intercepted.value)
    assert desktop.clicks == []
    click(desktop, screenshots, scope, screenshot_id, 200, 200)
    assert desktop.clicks == [(200, 200)]


def test_点击截图所属窗口不在作用域内时被拦截() -> None:
    desktop = FakeDesktop([window(handle=1), window(handle=2, rect=Rect(900, 0, 100, 100))])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [2])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 10)

    assert desktop.clicks == []


def test_落点下已没有窗口时点击被拦截() -> None:
    desktop = FakeDesktop([window(handle=1)])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)
    closed = FakeDesktop([window(handle=1)], gone={1})

    with pytest.raises(Intercepted, match="没有窗口"):
        click(closed, screenshots, scope, screenshot_id, 10, 10)

    assert closed.clicks == []


def test_句柄被别的进程复用后不再算作用域内() -> None:
    screenshots, scope = Screenshots(), TaskScope()
    before = FakeDesktop([window(handle=1, process_id=100)])
    declare_scope(before, scope, [1])
    screenshot_id = _observed(before, screenshots, 1)
    after = FakeDesktop([window(handle=1, process_id=200, title="换了主人")])

    with pytest.raises(Intercepted, match="任务作用域之外"):
        click(after, screenshots, scope, screenshot_id, 10, 10)

    assert after.clicks == []


def test_作用域内窗口弹出的对话框也在作用域内() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="另存为", owner=1, process_id=1, rect=Rect(100, 100, 400, 300)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(0, 0, 800, 600)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    result = click(desktop, screenshots, scope, _observed(desktop, screenshots, 2), 10, 10)

    assert desktop.clicks == [(110, 110)]
    assert result["window"]["title"] == "<untrusted-screen>另存为</untrusted-screen>"


@pytest.mark.parametrize(
    ("process_name", "reason"),
    [
        ("WindowsTerminal.exe", "终端"),
        ("cmd.exe", "终端"),
        ("powershell.exe", "终端"),
        ("pwsh.exe", "终端"),
        ("SystemSettings.exe", "系统设置"),
        ("regedit.exe", "系统设置"),
        ("explorer.exe", "资源管理器"),
    ],
)
def test_任务作用域里的高危窗口未经人确认不点击(process_name: str, reason: str) -> None:
    desktop = FakeDesktop([window(handle=1, title="高危", process_name=process_name)])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="高危窗口") as intercepted:
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 10)

    message = str(intercepted.value)
    assert reason in message
    assert "dangerous" in message
    assert desktop.clicks == []
    assert desktop.dialogs == []


def test_认不出进程的窗口即使在作用域内也拒绝点击() -> None:
    desktop = FakeDesktop([window(handle=1, title="提权", process_name="")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="无法确认") as intercepted:
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 10)

    assert "高危窗口" in str(intercepted.value)
    assert desktop.clicks == []

    arguments: dict[str, Any] = {
        "screenshot_id": _observed(desktop, screenshots, 1),
        "x": 10,
        "y": 10,
        "intent": "点一下",
        "dangerous": True,
    }
    decide(desktop, {"tool_name": "mcp__computer-use__click", "tool_input": arguments})

    with pytest.raises(Intercepted, match="无法确认"):
        tools.click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == []


def test_所有者认不出进程时对话框上的点击也被拒绝() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=2,
                title="属性",
                owner=1,
                process_name="notepad.exe",
                rect=Rect(0, 0, 100, 100),
            ),
            window(handle=1, title="提权", process_name=""),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1, 2])

    with pytest.raises(Intercepted, match="无法确认"):
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 2), 10, 10)

    assert desktop.clicks == []


def test_Agent_自身所在的窗口即使在作用域内也须经人确认才点击() -> None:
    desktop = FakeDesktop(
        [window(handle=1, title="computer-use - Cursor", process_name="Cursor.exe", process_id=42)],
        agent_processes={7, 42},
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(Intercepted, match="Agent 自身") as intercepted:
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 10)

    assert "dangerous" in str(intercepted.value)
    assert desktop.clicks == []


def test_高危窗口弹出的对话框同样须经人确认才点击() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="属性", owner=1, process_name="notepad.exe", rect=Rect(0, 0, 100, 100)),
            window(handle=1, title="管理员: Windows PowerShell", process_name="pwsh.exe"),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1, 2])

    with pytest.raises(Intercepted, match="终端") as intercepted:
        click(desktop, screenshots, scope, _observed(desktop, screenshots, 2), 10, 10)

    assert "dangerous" in str(intercepted.value)
    assert desktop.clicks == []


def test_裁决不能让落到作用域外的高危窗口放行() -> None:
    desktop = FakeDesktop(
        [
            window(handle=2, title="命令提示符", process_name="cmd.exe", rect=Rect(0, 0, 80, 80)),
            window(handle=1, title="无标题 - 记事本", rect=Rect(0, 0, 320, 240)),
        ]
    )
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)
    arguments: dict[str, Any] = {
        "screenshot_id": screenshot_id,
        "x": 10,
        "y": 10,
        "intent": "点进终端",
        "dangerous": True,
    }
    decide(desktop, {"tool_name": "mcp__computer-use__click", "tool_input": arguments})

    with pytest.raises(Intercepted, match="任务作用域之外"):
        tools.click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == []


def test_人确认后才点击作用域内的高危窗口() -> None:
    desktop = FakeDesktop([window(handle=1, title="管理员: Windows PowerShell", process_name="powershell.exe")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])
    screenshot_id = _observed(desktop, screenshots, 1)
    arguments: dict[str, Any] = {
        "screenshot_id": screenshot_id,
        "x": 10,
        "y": 10,
        "intent": "点进终端",
        "dangerous": True,
    }

    with pytest.raises(Intercepted, match="没有经过人的裁决"):
        tools.click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == []
    decision = decide(desktop, {"tool_name": "mcp__computer-use__click", "tool_input": arguments})
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "ask"

    tools.click(desktop, screenshots, scope, **arguments)

    assert desktop.clicks == [(10, 10)]
    assert desktop.dialogs == []


def test_任务作用域内的普通窗口点击不额外询问() -> None:
    desktop = FakeDesktop([window(handle=1, title="无标题 - 记事本", process_name="notepad.exe")])
    screenshots, scope = Screenshots(), TaskScope()
    declare_scope(desktop, scope, [1])

    click(desktop, screenshots, scope, _observed(desktop, screenshots, 1), 10, 10)

    assert desktop.clicks == [(10, 10)]
    assert desktop.dialogs == []


def test_未知的截图_ID_被拒绝_不注入点击() -> None:
    desktop = FakeDesktop([window(handle=1)])
    scope = TaskScope()
    declare_scope(desktop, scope, [1])

    with pytest.raises(ObservationError):
        click(desktop, Screenshots(), scope, "shot-不存在", 10, 10)

    assert desktop.clicks == []
