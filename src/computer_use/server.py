"""MCP 工具壳：把工具层挂到 FastMCP 上。不含任何判定逻辑。"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import TextContent

from computer_use import tools
from computer_use.desktop import DesktopPort, Rect
from computer_use.observation import ObservationError, Screenshots

INSTRUCTIONS = """\
操控本机 Windows 桌面。当前只有只读工具，不会改变桌面状态。
窗口一律以 `handle` 指称；`list_windows` 的矩形为屏幕物理像素。
观察与放大返回的截图各带一个 `screenshot_id`；指称截图上的位置时，一律用那张截图的像素坐标，
换算到屏幕由服务端完成。
"""


def create_server(desktop: DesktopPort) -> FastMCP:
    """在给定的平台边界上装配一台 server。"""

    mcp = FastMCP(name="computer-use", instructions=INSTRUCTIONS)
    screenshots = Screenshots()

    @mcp.tool
    def list_windows() -> list[dict[str, Any]]:
        """列出桌面上可操作的窗口，含标题、进程名与屏幕矩形。

        排除不可见、最小化与无标题的窗口。
        """

        return tools.list_windows(desktop)

    @mcp.tool
    def observe_window(handle: int) -> ToolResult:
        """截取一个窗口（不截全屏），被其他窗口遮住的部分也照常画出。

        元数据含截图 ID、窗口、采集时刻、截图尺寸、缩放比（截图像素 / 屏幕物理像素，
        窗口过大时服务端会先缩小）、显示器 DPI 缩放、截图左上角的屏幕偏移，以及相对窗口的偏移。
        两个偏移都是物理像素，仅供参考；指称位置时只用截图像素坐标。
        """

        return _observed_result(lambda: tools.observe_window(desktop, screenshots, handle))

    @mcp.tool
    def zoom(screenshot_id: str, left: int, top: int, width: int, height: int) -> ToolResult:
        """把某张截图上的一块矩形按屏幕原尺寸放大，用于看清小目标。

        矩形用那张截图的像素坐标给出。返回的是同一时刻的裁剪，不会重新截图；
        它有自己的截图 ID，`window_offset` 是裁剪相对窗口截图左上角的偏移。
        """

        rect = Rect(left=left, top=top, width=width, height=height)
        return _observed_result(lambda: tools.zoom(screenshots, screenshot_id, rect))

    return mcp


def _observed_result(observe: Callable[[], tools.Observed]) -> ToolResult:
    try:
        observed = observe()
    except ObservationError as error:
        raise ToolError(str(error)) from error
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
