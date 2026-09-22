"""MCP 工具壳：把工具层挂到 FastMCP 上。不含任何判定逻辑。"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from computer_use import tools
from computer_use.desktop import DesktopPort

INSTRUCTIONS = """\
操控本机 Windows 桌面。当前只有只读工具，不会改变桌面状态。
窗口一律以 `handle` 指称；矩形为屏幕物理像素。
"""


def create_server(desktop: DesktopPort) -> FastMCP:
    """在给定的平台边界上装配一台 server。"""

    mcp = FastMCP(name="computer-use", instructions=INSTRUCTIONS)

    @mcp.tool
    def list_windows() -> list[dict[str, Any]]:
        """列出桌面上可操作的窗口，含标题、进程名与屏幕矩形。

        排除不可见、最小化与无标题的窗口。
        """

        return tools.list_windows(desktop)

    return mcp


def main() -> None:
    from computer_use.win32_desktop import Win32Desktop

    create_server(Win32Desktop()).run()


if __name__ == "__main__":
    main()
