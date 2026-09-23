"""核心：输入动作。按截图像素坐标定位，放行前先做命中测试。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

from dataclasses import dataclass

from computer_use.desktop import DesktopPort, Window
from computer_use.observation import Screenshots, confirm_unchanged
from computer_use.scope import TaskScope


@dataclass(frozen=True)
class Landed:
    """一次动作实际落在哪里：屏幕物理像素坐标，以及那里的顶层窗口。"""

    window: Window
    x: int
    y: int


def click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
) -> Landed:
    """在截图 `screenshot_id` 的像素 `(x, y)` 处单击。

    落点未通过命中测试、或截图之后落点附近的界面已经变化时抛 `Intercepted`。
    """

    screenshot = screenshots.resolve(screenshot_id)
    screen_x, screen_y = screenshot.to_screen(x, y)
    window = scope.admit(desktop, screen_x, screen_y)
    confirm_unchanged(desktop, screenshot, screen_x, screen_y, window)
    desktop.click(screen_x, screen_y)
    return Landed(window=window, x=screen_x, y=screen_y)
