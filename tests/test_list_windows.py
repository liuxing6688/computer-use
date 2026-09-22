"""只读工具「列出窗口」的行为。"""

from __future__ import annotations

from computer_use.desktop import Rect
from computer_use.tools import list_windows

from .fake_desktop import FakeDesktop, window


def test_报告窗口的标题_进程名与矩形() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=0x1234,
                title="无标题 - 记事本",
                process_name="notepad.exe",
                rect=Rect(left=100, top=50, width=800, height=600),
            )
        ]
    )

    assert list_windows(desktop) == [
        {
            "handle": 0x1234,
            "title": "无标题 - 记事本",
            "process_name": "notepad.exe",
            "rect": {"left": 100, "top": 50, "width": 800, "height": 600},
        }
    ]


def test_排除最小化的窗口() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, title="前台窗口"),
            window(handle=2, title="缩到任务栏的窗口", is_minimized=True),
        ]
    )

    assert [w["handle"] for w in list_windows(desktop)] == [1]


def test_排除无标题的窗口() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, title="前台窗口"),
            window(handle=2, title=""),
            window(handle=3, title="   "),
        ]
    )

    assert [w["handle"] for w in list_windows(desktop)] == [1]


def test_排除不可见的窗口() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, title="前台窗口"),
            window(handle=2, title="后台常驻的隐藏窗口", is_visible=False),
        ]
    )

    assert [w["handle"] for w in list_windows(desktop)] == [1]
