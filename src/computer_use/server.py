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
from computer_use.actions import ActionError
from computer_use.desktop import (
    ClipboardUnavailable,
    CommandError,
    DesktopPort,
    FileError,
    ForegroundError,
    InjectionError,
    LaunchError,
    Rect,
)
from computer_use.observation import ObservationError, Screenshots
from computer_use.pace import INPUT_BUDGET, INPUT_INTERVAL, Pace
from computer_use.scope import ScopeError, TaskScope

T = TypeVar("T")

INSTRUCTIONS = f"""\
操控本机 Windows 桌面。
窗口一律以 `handle` 指称；`list_windows` 的矩形为屏幕物理像素。
观察与放大返回的截图各带一个 `screenshot_id`；指称截图上的位置时，一律用那张截图的像素坐标，
换算到屏幕由服务端完成。
动手之前先用 `declare_scope` 声明本次任务涉及的窗口。第一次声明直接生效；
已经声明过再换成另一组窗口时，会弹出系统对话框，人拒绝或超时则作用域不变。
点击、双击、右键、拖拽、滚动、按键或文本输入落在任务作用域之外时一律被拒绝。
落在任务作用域内的高危窗口（终端、系统设置、资源管理器、Agent 自身所在的窗口）上时，
须把 `dangerous` 设为 true 重新调用，由人在 Claude Code 中确认后才执行。
认不出所属进程的窗口一律拒绝。
`press_keys` 发送组合键与功能键。Windows 键、Alt+Tab、Alt+Esc、Ctrl+Esc、Ctrl+Alt+Delete
会离开目标窗口，一律拒绝。
`launch_app` 启动一个 .exe 并等待它的新窗口；超时会说明期间出现了哪些别的窗口。
新窗口不会自动进入任务作用域。启动终端、系统设置或资源管理器须经人确认后才启动。
单击、双击、右键、拖拽、滚动、输入文本、按键、启动应用在执行成功后都返回变化说明：
前台窗口有没有变、新出现了哪些看得见的窗口（含没有标题的）。只为这两件事不必再做一次观察。
被拦截或失败时不会把结果说成执行成功。
不能用来启动命令解释器或脚本宿主。
点击前服务端会重新采集落点附近，与那张截图比对；界面在此期间变了就拒绝执行，此时请重新观察。
输入文本用 `type_text`，打进目标窗口当前的焦点输入框。中文优先走剪贴板粘贴，原剪贴板内容会在事后恢复；
粘贴走不通时改为逐字符注入。返回里写明实际走了哪一档，以及是否占用过剪贴板。
输入工具须如实自报危险性（`dangerous`）。自报为危险的动作要由人在 Claude Code 里确认。
模型自报不危险、但落点附近读到高危词时，同一次调用会弹出系统对话框问人；人确认后才执行，拒绝或超时则不执行。
不要把 `dangerous` 改成 true 再调一次。
相邻输入动作至少隔 {INPUT_INTERVAL:g} 秒。连续输入满 {INPUT_BUDGET} 次后，下一次须原样重新调用，由人确认才能继续；
把 `dangerous` 改成 true 不能代替这次确认。
急停热键是 Ctrl+Break：按下后正在等待的输入被取消，之后所有输入工具拒绝，直到调用 `resume` 并经人确认。
只读工具在急停后仍可用。
`read_file` 与 `list_directory` 直接读盘，无需确认。
`write_file`、`move_file`、`delete_file` 一律要由人在 Claude Code 里确认后才执行；
确认里能看到目标路径和变更类型，盖过已有文件时会写明是覆盖。
`delete_file` 默认把文件移入回收站，只经常规拦截，不弹系统对话框。
永久删除要显式传入 `permanent: true`，确认理由会写成永久删除，不能靠同一次回收站确认改过去。
常规拦截通过之后还会弹出系统对话框，写明这是永久删除以及目标路径；人拒绝或超时则文件保持原样。
`run_powershell` 执行一条 PowerShell 命令并返回标准输出、标准错误与退出码。
只读命令直接执行。含写操作、下载或动态求值的命令一律要人确认；
判定看整条命令，管道、分号、脚本块和子表达式里藏着的写操作也会被拦住。
把 `dangerous` 报成 false 也不能让这类命令自己通过。
"""


def create_server(desktop: DesktopPort) -> FastMCP:
    """在给定的平台边界上装配一台 server。"""

    mcp = FastMCP(name="computer-use", instructions=INSTRUCTIONS)
    screenshots = Screenshots()
    scope = TaskScope()
    log = ActionLog(desktop)
    pace = Pace(desktop)
    desktop.register_stop_hotkey(pace.stop)

    @mcp.tool
    def list_windows() -> list[dict[str, Any]]:
        """列出桌面上可操作的窗口，含标题、进程名与屏幕矩形。

        排除不可见、最小化与无标题的窗口。
        """

        return log.run(
            tool="list_windows",
            target={},
            intent=None,
            dangerous=None,
            evidence_window=None,
            action=lambda: tools.list_windows(desktop),
        )

    @mcp.tool
    def observe_window(handle: int) -> ToolResult:
        """截取一个窗口（不截全屏），被其他窗口遮住的部分也照常画出。

        元数据含截图 ID、窗口、采集时刻、截图尺寸、缩放比（截图像素 / 屏幕物理像素，
        窗口过大时服务端会先缩小）、显示器 DPI 缩放、截图左上角的屏幕偏移，以及相对窗口的偏移。
        两个偏移都是物理像素，仅供参考；指称位置时只用截图像素坐标。
        返回里 `targets` 与 `metadata` 并列。`targets` 是目标清单，每一项是一段描述，
        以及它在这张截图上的边界框（`rect` 的 `left`、`top`、`width`、`height`，截图像素）。
        像素通道不产出目标，清单为空，定位仍靠截图。采集元数据在 `metadata` 里。
        """

        return _observed_result(
            lambda: log.run(
                tool="observe_window",
                target={"window": handle},
                intent=None,
                dangerous=None,
                evidence_window=handle,
                action=lambda: tools.observe_window(desktop, screenshots, handle),
            )
        )

    @mcp.tool
    def zoom(screenshot_id: str, left: int, top: int, width: int, height: int) -> ToolResult:
        """把某张截图上的一块矩形按屏幕原尺寸放大，用于看清小目标。

        矩形用那张截图的像素坐标给出。返回的是同一时刻的裁剪，不会重新截图；
        它有自己的截图 ID，`window_offset` 在 `metadata` 里，是裁剪相对窗口截图左上角的偏移。
        目标清单是这次观察的一部分，与 `metadata` 并列，形状与观察窗口相同；像素通道下同样为空。
        """

        rect = Rect(left=left, top=top, width=width, height=height)
        window = _window_of(screenshots, screenshot_id)
        return _observed_result(
            lambda: log.run(
                tool="zoom",
                target={"window": window, "screenshot_id": screenshot_id, "rect": asdict(rect)},
                intent=None,
                dangerous=None,
                evidence_window=window,
                action=lambda: tools.zoom(screenshots, screenshot_id, rect),
            )
        )

    @mcp.tool
    def read_file(path: str) -> dict[str, Any]:
        """读出文本文件的内容。只读，无需确认。"""

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="read_file",
                target={"path": path},
                intent=None,
                dangerous=None,
                evidence_window=None,
                action=lambda: tools.read_file(desktop, path),
            )
        )

    @mcp.tool
    def list_directory(path: str) -> list[dict[str, Any]]:
        """列出目录的直接子项，每项有名字以及是否为目录。只读，无需确认。"""

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="list_directory",
                target={"path": path},
                intent=None,
                dangerous=None,
                evidence_window=None,
                action=lambda: tools.list_directory(desktop, path),
            )
        )

    @mcp.tool
    def write_file(path: str, content: str, intent: str, dangerous: bool) -> dict[str, Any]:
        """把文本写入文件。父目录须已存在。

        新建与覆盖都要由人在 Claude Code 中确认后才会执行。确认信息含目标路径与变更类型；
        路径上已有文件时，变更类型为覆盖。文件内容不写入动作日志。
        `intent` 用一句话说明这次写入要做什么。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="write_file",
                target={"path": path},
                intent=intent,
                dangerous=dangerous,
                evidence_window=None,
                action=lambda: tools.write_file(
                    desktop, path, content, intent=intent, dangerous=dangerous
                ),
            )
        )

    @mcp.tool
    def move_file(
        source: str, destination: str, intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """把文件或目录挪到新路径。一律须经人确认。

        确认信息含两端路径。目标已是文件时，变更类型为覆盖。
        `intent` 用一句话说明这次移动要做什么。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="move_file",
                target={"source": source, "destination": destination},
                intent=intent,
                dangerous=dangerous,
                evidence_window=None,
                action=lambda: tools.move_file(
                    desktop, source, destination, intent=intent, dangerous=dangerous
                ),
            )
        )

    @mcp.tool
    def delete_file(
        path: str, intent: str, dangerous: bool, permanent: bool | None = None
    ) -> dict[str, Any]:
        """删除文件或目录。默认移入回收站。

        一律须经人确认，确认信息含目标路径与变更类型。
        省略 `permanent` 或为 false 时移入回收站，只走这次拦截，不弹系统对话框。
        `permanent` 为 true 时改为永久删除，确认理由与移入回收站不同，须单独经人确认。
        这次拦截通过之后还会弹出系统对话框，写明这是永久删除以及目标路径；
        人拒绝或一分钟内没有回应，文件保持原样。
        不能靠改 `permanent` 复用同一次确认。
        `intent` 用一句话说明这次删除要做什么。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="delete_file",
                target={"path": path, "permanent": permanent is True},
                intent=intent,
                dangerous=dangerous,
                evidence_window=None,
                action=lambda: tools.delete_file(
                    desktop,
                    path,
                    intent=intent,
                    dangerous=dangerous,
                    permanent=permanent,
                ),
            )
        )

    @mcp.tool
    def run_powershell(command: str, intent: str, dangerous: bool) -> dict[str, Any]:
        """执行一条 PowerShell 命令，返回标准输出、标准错误与退出码。

        只读命令直接执行，无需确认。含写操作、下载或动态求值的命令须由人在 Claude Code 中确认；
        判定解析整条命令，不看开头的动词。管道、分号、脚本块与子表达式中的写操作同样拦截。
        不加载配置文件。60 秒内没有结束则报错。
        `intent` 用一句话说明这次命令要做什么。命令原文记入动作日志，输出不记。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="run_powershell",
                target={"command": command},
                intent=intent,
                dangerous=dangerous,
                evidence_window=None,
                action=lambda: tools.run_powershell(
                    desktop, command, intent=intent, dangerous=dangerous
                ),
            )
        )

    @mcp.tool
    def declare_scope(handles: list[int]) -> list[dict[str, Any]]:
        """声明本次任务涉及的窗口（任务作用域），替换原有的作用域，返回声明后的作用域。

        窗口须是 `list_windows` 列出的可操作窗口；作用域内窗口弹出的对话框也算作用域内。
        第一次声明直接生效。已经有作用域时再换成另一组窗口，会弹出系统对话框；
        人拒绝或一分钟内没有回应，作用域保持原样。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="declare_scope",
                target={"windows": handles},
                intent=None,
                dangerous=None,
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
            dangerous=None,
            evidence_window=None,
            action=lambda: tools.get_scope(scope),
        )

    @mcp.tool
    def click(screenshot_id: str, x: int, y: int, intent: str, dangerous: bool) -> dict[str, Any]:
        """在某张截图的像素 `(x, y)` 处单击鼠标左键。

        坐标用那张截图的像素坐标给出，换算到屏幕由服务端完成。执行前做命中测试：
        落点处的窗口不在任务作用域内时拒绝执行并说明原因；认不出所属进程的窗口一律拒绝。
        落在任务作用域内的高危窗口（终端、系统设置、资源管理器、Agent 自身所在的窗口）上时，
        须把 `dangerous` 设为 true 重新调用，由人确认后才执行。
        截图之后窗口移动、改变大小，或落点附近的界面已经变化时也拒绝执行，须重新观察。
        `intent` 用一句话说明这次点击要做什么，记入动作日志。
        `dangerous` 自报这次点击是否危险：后果难以撤销（删除、卸载、覆盖）或后果离开本机
        （发送、提交、支付）即为危险。服务端还会识别落点附近的文字。
        模型自报不危险、但读到高危词时，同一次调用弹出系统对话框问人；人确认后才执行，拒绝或超时则不执行。
        不要把 `dangerous` 改成 true 再调一次。
        模型自报为危险的点击须由人在 Claude Code 中确认后才会执行；没有任何参数能跳过这一步。
        外发动作（发送、提交、发布、支付等）会弹出系统对话框，同时给发送类摆上当前窗口截图和已输入的内容；
        模型自报为危险时，这个对话框在 Claude Code 的确认之后。人拒绝或超时都不执行，也没有任何参数能跳过这个对话框。
        返回落点处的窗口与落点的屏幕物理像素坐标（仅供参考），以及变化说明：
        前台窗口有没有变、新出现了哪些看得见的窗口。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="click",
                target={"window": window, "screenshot_id": screenshot_id, "x": x, "y": y},
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.click(
                    desktop, screenshots, scope, screenshot_id, x, y,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def double_click(
        screenshot_id: str, x: int, y: int, intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """在某张截图的像素 `(x, y)` 处双击鼠标左键。

        坐标、命中测试、截图比对与危险判定都与 `click` 相同。
        `intent` 用一句话说明这次双击要做什么，记入动作日志。
        执行成功后返回落点与变化说明。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="double_click",
                target={"window": window, "screenshot_id": screenshot_id, "x": x, "y": y},
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.double_click(
                    desktop, screenshots, scope, screenshot_id, x, y,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def right_click(
        screenshot_id: str, x: int, y: int, intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """在某张截图的像素 `(x, y)` 处单击鼠标右键。

        坐标、命中测试、截图比对与危险判定都与 `click` 相同。
        `intent` 用一句话说明这次右键要做什么，记入动作日志。
        执行成功后返回落点与变化说明。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="right_click",
                target={"window": window, "screenshot_id": screenshot_id, "x": x, "y": y},
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.right_click(
                    desktop, screenshots, scope, screenshot_id, x, y,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def drag(
        screenshot_id: str, x: int, y: int, to_x: int, to_y: int, intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """在某张截图上从像素 `(x, y)` 拖到 `(to_x, to_y)`。

        两个点都用这张截图的像素坐标，都要落在任务作用域内。起点还要通过截图比对。
        `intent` 用一句话说明这次拖拽要做什么，记入动作日志。
        执行成功后返回两端落点与变化说明。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="drag",
                target={
                    "window": window,
                    "screenshot_id": screenshot_id,
                    "x": x,
                    "y": y,
                    "to_x": to_x,
                    "to_y": to_y,
                },
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.drag(
                    desktop, screenshots, scope, screenshot_id, x, y, to_x, to_y,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def scroll(
        screenshot_id: str, x: int, y: int, notches: int, intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """在某张截图的像素 `(x, y)` 处滚动滚轮。

        `notches` 为正向上、为负向下，一格是一次滚轮凹口。落点的命中测试与截图比对和 `click` 相同。
        `intent` 用一句话说明这次滚动要做什么，记入动作日志。
        执行成功后返回落点、格数与变化说明。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="scroll",
                target={
                    "window": window,
                    "screenshot_id": screenshot_id,
                    "x": x,
                    "y": y,
                    "notches": notches,
                },
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.scroll(
                    desktop, screenshots, scope, screenshot_id, x, y, notches,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def press_keys(
        screenshot_id: str, keys: list[str], intent: str, dangerous: bool
    ) -> dict[str, Any]:
        """把组合键或功能键送进某张截图所属的窗口。

        `keys` 按按下的顺序给出，例如 `["ctrl", "s"]`、`["f5"]`、`["alt", "f4"]`。
        字母、数字、功能键 f1–f12，以及 enter、tab、escape、space、backspace、delete、insert、
        home、end、pageup、pagedown、方向键都可以。先把该窗口带到前台，没能到前台就不按。
        Windows 键、Alt+Tab、Alt+Esc、Ctrl+Esc、Ctrl+Alt+Delete 会离开目标窗口，一律拒绝。
        `intent` 用一句话说明这次按键要做什么，记入动作日志。
        执行成功后返回送到的窗口、按键与变化说明。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="press_keys",
                target={"window": window, "screenshot_id": screenshot_id, "keys": keys},
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.press_keys(
                    desktop, screenshots, scope, screenshot_id, keys,
                    intent=intent, dangerous=dangerous, pace=pace,
                ),
            )
        )

    @mcp.tool
    def launch_app(app: str, intent: str, dangerous: bool) -> dict[str, Any]:
        """启动一个应用并等待它的新窗口，以便任务能从零开始。

        `app` 是程序名或 .exe 路径，不带参数；没有扩展名时按 .exe 启动。
        等到一个新出现的、属于这个进程（或同名可执行文件）的可见窗口再返回。
        15 秒内没出现就报错，并说明期间新出现了哪些别的窗口。
        新窗口不会加入任务作用域，要用它之前先 `declare_scope`。
        终端、系统设置、资源管理器会打开高危窗口，须经人确认后才启动。
        命令解释器与脚本宿主不能从这里启动。
        `intent` 用一句话说明为什么启动它，记入动作日志。
        执行成功后返回新窗口、进程号与变化说明。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="launch_app",
                target={"app": app},
                intent=intent,
                dangerous=dangerous,
                evidence_window=None,
                action=lambda: tools.launch_app(
                    desktop, app, intent=intent, dangerous=dangerous, pace=pace
                ),
            )
        )

    @mcp.tool
    def type_text(screenshot_id: str, text: str, intent: str, dangerous: bool) -> dict[str, Any]:
        """把文本打进某张截图所属窗口当前的焦点输入框，可以包含中文。

        先把该窗口带到前台。优先经剪贴板粘贴：先保存原剪贴板内容，粘贴之后恢复，
        调用方原来复制的内容不会被留下。粘贴走不通（剪贴板打不开，或按键送不进去）时，
        改为逐字符 Unicode 注入。
        返回实际落到的窗口、`tier`、`clipboard_used` 与变化说明。`tier` 为 `clipboard` 表示走了剪贴板粘贴，
        为 `unicode` 表示降级成了逐字符注入。`clipboard_used` 为真表示这段文本曾经写入剪贴板。
        `intent` 用一句话说明这次输入要做什么，记入动作日志。文本本身不写入日志。
        `dangerous` 自报这次输入是否危险：后果难以撤销或后果离开本机即为危险。
        判为危险的输入须由人在 Claude Code 中确认后才会执行。
        """

        window = _window_of(screenshots, screenshot_id)
        return _refusal_as_tool_error(
            lambda: log.run(
                tool="type_text",
                target={"window": window, "screenshot_id": screenshot_id},
                intent=intent,
                dangerous=dangerous,
                evidence_window=window,
                action=lambda: tools.type_text(
                    desktop,
                    screenshots,
                    scope,
                    screenshot_id,
                    text,
                    intent=intent,
                    dangerous=dangerous,
                    pace=pace,
                ),
            )
        )

    @mcp.tool
    def resume() -> str:
        """解除急停，使输入工具重新可用。

        未急停时无事发生。急停中须由人在 Claude Code 里确认后才会恢复。
        """

        return _refusal_as_tool_error(
            lambda: log.run(
                tool="resume",
                target={},
                intent=None,
                dangerous=None,
                evidence_window=None,
                action=lambda: tools.resume(desktop, pace),
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
    """把核心的拒绝、拦截与输入失败原样转成回给模型的工具错误。"""

    try:
        return call()
    except (
        ObservationError,
        ScopeError,
        Intercepted,
        ActionError,
        ForegroundError,
        ClipboardUnavailable,
        InjectionError,
        LaunchError,
        FileError,
        CommandError,
    ) as error:
        raise ToolError(str(error)) from error


def _observed_result(observe: Callable[[], tools.Observed]) -> ToolResult:
    observed = _refusal_as_tool_error(observe)
    payload = tools.as_observation(observed)
    return ToolResult(
        content=[
            Image(data=observed.png, format="png").to_image_content(),
            TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)),
        ],
        structured_content=payload,
    )


def main() -> None:
    from computer_use.win32_desktop import Win32Desktop

    create_server(Win32Desktop()).run()


if __name__ == "__main__":
    main()
