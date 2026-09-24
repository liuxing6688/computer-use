"""核心：任务作用域，以及动作落点能否放行的命中测试。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

from typing import Mapping, Sequence

from computer_use.action_log import Intercepted
from computer_use.desktop import DesktopPort, Window
from computer_use.untrusted import quoted_window
from computer_use.windows import operable_windows


class ScopeError(Exception):
    """任务作用域无法按要求声明；消息原样回给模型。"""


class TaskScope:
    """本次任务声明涉及的一组窗口。尚未声明时为空，此时任何落点都在作用域之外。

    作用域内窗口所拥有的窗口（它弹出的对话框、菜单）也算作用域内。
    窗口按句柄与所属进程共同认定，句柄在窗口关闭后被别的进程复用时不再算数。
    """

    def __init__(self) -> None:
        self._windows: tuple[Window, ...] = ()
        self._entered: dict[int, str] = {}

    def remember_text(self, handle: int, text: str) -> None:
        """记下打进这个窗口的文本，供之后的发送类确认一并呈现。"""

        self._entered[handle] = self._entered.get(handle, "") + text

    def entered_text(self, handle: int) -> str:
        """已经打进这个窗口、尚未随发送类确认清掉的文本。"""

        return self._entered.get(handle, "")

    def forget_text(self, handle: int) -> None:
        """发送类确认已经把这段内容摆给人看过，下次从空的开始。"""

        self._entered.pop(handle, None)

    @property
    def windows(self) -> tuple[Window, ...]:
        """声明时那一刻的窗口。"""

        return self._windows

    def declare(self, desktop: DesktopPort, handles: Sequence[int]) -> None:
        """以 `handles` 替换当前作用域。其中任何一个不可操作时整体拒绝，原作用域不变。"""

        if not handles:
            raise ScopeError("任务作用域至少要有一个窗口")
        operable = {w.handle: w for w in operable_windows(desktop)}
        missing = [h for h in handles if h not in operable]
        if missing:
            raise ScopeError(
                f"窗口 {', '.join(map(str, missing))} 不存在或不可操作（不可见、最小化或无标题），"
                "任务作用域未改变"
            )
        chosen = tuple(operable[h] for h in dict.fromkeys(handles))
        from computer_use.confirmation import confirm_scope_switch

        confirm_scope_switch(desktop, self._windows, chosen)
        self._windows = chosen

    def admit(self, desktop: DesktopPort, x: int, y: int) -> Window:
        """命中测试：屏幕物理像素 `(x, y)` 处的顶层窗口，它须在作用域内。

        不在作用域内时抛 `Intercepted`，理由指明落点处是哪个窗口。
        """

        if not self._windows:
            raise Intercepted("尚未声明任务作用域，任何落点都在作用域之外；请先声明本次任务涉及的窗口")
        all_windows = {w.handle: w for w in desktop.list_windows()}
        handle = desktop.window_at(x, y)
        hit = all_windows.get(handle) if handle is not None else None
        if hit is None:
            raise Intercepted(f"落点 ({x}, {y}) 处没有窗口")
        chain = _owner_chain(hit, all_windows)
        agent_processes = frozenset(desktop.agent_process_ids())
        for w in chain:
            if (risk := _high_risk(w, agent_processes)) is not None:
                raise Intercepted(
                    f"落点 ({x}, {y}) 处是{_describe(hit)}，属于高危窗口（{risk}），"
                    "即使在任务作用域内也一律拒绝"
                )
        declared = {(w.handle, w.process_id) for w in self._windows}
        if not any((w.handle, w.process_id) in declared for w in chain):
            raise Intercepted(f"落点 ({x}, {y}) 处是{_describe(hit)}，在任务作用域之外")
        return hit

    def admit_window(self, desktop: DesktopPort, window: Window) -> Window:
        """文本输入的目标：截图所属的那个窗口，须仍在，且在作用域内、不是高危窗口。

        文本输入没有落点，目标就是这扇窗口本身。它弹出的对话框也算作用域内。
        """

        if not self._windows:
            raise Intercepted(
                "尚未声明任务作用域，任何窗口都在作用域之外；请先声明本次任务涉及的窗口"
            )
        all_windows = {w.handle: w for w in desktop.list_windows()}
        current = all_windows.get(window.handle)
        if current is None:
            raise Intercepted(
                f"{quoted_window(window.title, window.handle)}已经不在了"
            )
        if current.process_id != window.process_id:
            raise Intercepted(
                f"句柄 {window.handle} 已属于{_describe(current)}，不再是截图里的那个窗口，"
                "在任务作用域之外"
            )
        chain = _owner_chain(current, all_windows)
        agent_processes = frozenset(desktop.agent_process_ids())
        for w in chain:
            if (risk := _high_risk(w, agent_processes)) is not None:
                raise Intercepted(
                    f"目标是{_describe(current)}，属于高危窗口（{risk}），"
                    "即使在任务作用域内也一律拒绝"
                )
        declared = {(w.handle, w.process_id) for w in self._windows}
        if not any((w.handle, w.process_id) in declared for w in chain):
            raise Intercepted(f"目标是{_describe(current)}，在任务作用域之外")
        return current


_TERMINALS = frozenset(
    {
        "windowsterminal.exe",
        "openconsole.exe",
        "conhost.exe",
        "cmd.exe",
        "powershell.exe",
        "powershell_ise.exe",
        "pwsh.exe",
        "wsl.exe",
        "mintty.exe",
        "alacritty.exe",
        "wezterm-gui.exe",
    }
)
_SYSTEM_SETTINGS = frozenset(
    {
        "systemsettings.exe",
        "control.exe",
        "mmc.exe",
        "regedit.exe",
        "taskmgr.exe",
        "useraccountcontrolsettings.exe",
    }
)
_FILE_EXPLORER = "explorer.exe"


def risk_of_process(process_name: str) -> str | None:
    """进程名为何使它的窗口成为高危窗口；不是时为 `None`。

    进程名认不出来时按高危处理：够不到的多半是提权进程，而提权的终端正是最危险的那一种。
    """

    name = process_name.lower()
    if not name:
        return "无法确认所属进程，可能是提权窗口"
    if name in _TERMINALS:
        return "终端"
    if name in _SYSTEM_SETTINGS:
        return "系统设置"
    if name == _FILE_EXPLORER:
        return "资源管理器，含桌面与任务栏"
    return None


def _high_risk(window: Window, agent_processes: frozenset[int]) -> str | None:
    """`window` 为何是高危窗口；不是时为 `None`。"""

    if window.process_id in agent_processes:
        return "Agent 自身所在的窗口"
    return risk_of_process(window.process_name)


def _describe(window: Window) -> str:
    return quoted_window(window.title, window.handle, process_name=window.process_name)


def _owner_chain(window: Window, all_windows: Mapping[int, Window]) -> list[Window]:
    """`window` 本身，以及它的所有者、所有者的所有者……直到没有所有者为止。"""

    chain = [window]
    while (owner := all_windows.get(chain[-1].owner or 0)) is not None and owner not in chain:
        chain.append(owner)
    return chain
