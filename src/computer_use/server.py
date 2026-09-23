"""MCP 工具壳：把工具层挂到 FastMCP 上。不含任何判定逻辑。"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent

from computer_use import tools
from computer_use.action_log import ActionLog, Intercepted
from computer_use.desktop import DesktopPort, Rect
from computer_use.observation import ObservationError, Screenshots
from computer_use.scope import ScopeError, TaskScope

T = TypeVar("T")

INSTRUCTIONS = """\
操控本机 Windows 桌面。
窗口一律以 `handle` 指称；`list_windows` 的矩形为屏幕物理像素。
观察与放大返回的截图各带一个 `screenshot_id`；指称截图上的位置时，一律用那张截图的像素坐标，
换算到屏幕由服务端完成。
动手之前先用 `declare_scope` 声明本次任务涉及的窗口。点击落在任务作用域之外、
或落在高危窗口（终端、系统设置、资源管理器、Agent 自身所在的窗口）上时一律被拒绝。
"""


def create_server(desktop: DesktopPort) -> FastMCP:
    """在给定的平台边界上装配一台 server。"""

    mcp = FastMCP(name="computer-use", instructions=INSTRUCTIONS)
    screenshots = Screenshots()
    scope = TaskScope()
    log = ActionLog(desktop)

    @mcp.tool
    def list_windows() -> list[dict[str, Any]]:
        """列出桌面上可操作的窗口，含标题、进程名与屏幕矩形。

        排除不可见、最小化与无标题的窗口。
        """

        return log.run(
            tool="list_windows",
            target={},
            intent=None,
            evidence_window=None,
            action=lambda: tools.list_windows(desktop),
        )

    @mcp.tool
    def observe_window(handle: int) -> ToolResult:
        """截取一个窗口（不截全屏），被其他窗口遮住的部分也照常画出。

        元数据含截图 ID、窗口、采集时刻、截图尺寸、缩放比（截图像素 / 屏幕物理像素，
        窗口过大时服务端会先缩小）、显示器 DPI 缩放、截图左上角的屏幕偏移，以及相对窗口的偏移。
        两个偏移都是物理像素，仅供参考；指称位置时只用截图像素坐标。
        """

        return _observed_result(
            lambda: log.run(
                tool="observe_window",
                target={"window": handle},
                intent=None,
                evidence_window=handle,
                action=lambda: tools.observe_window(desktop, screenshots, handle),
            )
        )

    @mcp.tool
    def zoom(screenshot_id: str, left: int, top: int, width: int, height: int) -> ToolResult:
        """把某张截图上的一块矩形按屏幕原尺寸放大，用于看清小目标。

        矩形用那张截图的像素坐标给出。返回的是同一时刻的裁剪，不会重新截图；
        它有自己的截图 ID，`window_offset` 是裁剪相对窗口截图左上角的偏移。
        """

        rect = Rect(left=left, top=top, width=width, height=height)
        window = _window_of(screenshots, screenshot_id)
        return _observed_result(
            lambda: log.run(
                tool="zoom",
                target={"window": window, "screenshot_id": screenshot_id, "rect": asdict(rect)},
                intent=None,
                evidence_window=window,
                action=lambda: tools.zoom(screenshots, screenshot_id, rect),
            )
        )

    @mcp.tool
    def declare_scope(handles: list[int]) -> list[dict[str, Any]]:
        """声明本次任务涉及的窗口（任务作用域），替换原有的作用域，返回声明后的作用域。

        窗口须是 `list_windows` 列出的可操作窗口；作用域内窗口弹出的对话框也算作用域内。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="declare_scope",
                target={"windows": handles},
                intent=None,
                evidence_window=None,
                action=lambda: tools.declare_scope(desktop, scope, handles),
            )
        )

    @mcp.tool
    def get_scope() -> list[dict[str, Any]]:
        """当前的任务作用域：每个窗口的句柄、标题与进程名，记的是声明时的样子。尚未声明时为空。"""

        return log.run(
            tool="get_scope",
            target={},
            intent=None,
            evidence_window=None,
            action=lambda: tools.get_scope(scope),
        )

    @mcp.tool
    def click(screenshot_id: str, x: int, y: int, intent: str) -> dict[str, Any]:
        """在某张截图的像素 `(x, y)` 处单击鼠标左键。

        坐标用那张截图的像素坐标给出，换算到屏幕由服务端完成。执行前做命中测试：
        落点处的窗口不在任务作用域内、或是高危窗口时拒绝执行并说明原因。
        `intent` 用一句话说明这次点击要做什么，记入动作日志。
        返回落点处的窗口与落点的屏幕物理像素坐标（仅供参考）。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="click",
                target={"window": window, "screenshot_id": screenshot_id, "x": x, "y": y},
                intent=intent,
                evidence_window=window,
                action=lambda: tools.click(desktop, screenshots, scope, screenshot_id, x, y),
            )
        )

    return mcp


def _window_of(screenshots: Screenshots, screenshot_id: str) -> int | None:
    """截图所属窗口的句柄；截图 ID 解析不了时为 `None`，交由工具本身报错。"""

    try:
        return screenshots.resolve(screenshot_id).window.handle
    except ObservationError:
        return None


def _refusal_as_tool_error(call: Callable[[], T]) -> T:
    """把核心的拒绝与拦截原样转成回给模型的工具错误。"""

    try:
        return call()
    except (ObservationError, ScopeError, Intercepted) as error:
        raise ToolError(str(error)) from error


def _observed_result(observe: Callable[[], tools.Observed]) -> ToolResult:
    observed = _refusal_as_tool_error(observe)
    return ToolResult(
        content=[
            Image(data=observed.png, format="png").to_image_content(),
            TextContent(type="text", text=json.dumps(observed.metadata, ensure_ascii=False)),
        ],
        structured_content=observed.metadata,
    )


def main() -> None:
    from computer_use.win32_desktop import Win32Desktop

    create_server(Win32Desktop()).run()


if __name__ == "__main__":
    main()
