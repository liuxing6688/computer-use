"""核心：输入动作。按截图像素坐标定位，放行前依次做命中测试、截图比对与危险动作判定。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from computer_use.action_log import Intercepted
from computer_use.confirmation import confirm_outbound
from computer_use.danger import Verdict, judge_call, judge_nearby_text, read_nearby
from computer_use.desktop import Clipboard, ClipboardUnavailable, DesktopPort, Window
from computer_use.interception import require_ruling
from computer_use.observation import Screenshots, confirm_unchanged
from computer_use.pace import Pace, budget_verdict
from computer_use.scope import TaskScope, risk_of_process
from computer_use.untrusted import quoted_window
from computer_use.windows import operable_windows


@dataclass(frozen=True)
class Landed:
    """一次动作实际落在哪里：屏幕物理像素坐标，以及那里的顶层窗口。"""

    window: Window
    x: int
    y: int


class ActionError(Exception):
    """动作无法按给出的参数执行；消息原样回给模型。"""


def click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Landed:
    """在截图 `screenshot_id` 的像素 `(x, y)` 处单击。

    落点未通过命中测试、或截图之后落点附近的界面已经变化时抛 `Intercepted`，裁决凭据也救不回来；
    模型自报或落点附近的文字判为危险、又没有经人裁决时同样抛 `Intercepted`。
    """

    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "intent": intent,
        "dangerous": dangerous,
    }
    [landed], gate = _prepare_points(
        desktop, screenshots, scope, screenshot_id, [(x, y)],
        tool="click", arguments=arguments, pace=pace,
    )
    _inject(gate, lambda: desktop.click(landed.x, landed.y))
    return landed


def double_click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Landed:
    """在截图 `screenshot_id` 的像素 `(x, y)` 处双击。放行条件与单击相同。"""

    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "intent": intent,
        "dangerous": dangerous,
    }
    [landed], gate = _prepare_points(
        desktop, screenshots, scope, screenshot_id, [(x, y)],
        tool="double_click", arguments=arguments, pace=pace,
    )
    _inject(gate, lambda: desktop.double_click(landed.x, landed.y))
    return landed


def right_click(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Landed:
    """在截图 `screenshot_id` 的像素 `(x, y)` 处单击右键。放行条件与单击相同。"""

    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "intent": intent,
        "dangerous": dangerous,
    }
    [landed], gate = _prepare_points(
        desktop, screenshots, scope, screenshot_id, [(x, y)],
        tool="right_click", arguments=arguments, pace=pace,
    )
    _inject(gate, lambda: desktop.right_click(landed.x, landed.y))
    return landed


@dataclass(frozen=True)
class Dragged:
    """一次拖拽的起点与终点：都是屏幕物理像素，以及各自落上的顶层窗口。"""

    start: Landed
    end: Landed


def drag(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    to_x: int,
    to_y: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Dragged:
    """从截图像素 `(x, y)` 拖到 `(to_x, to_y)`。两个落点都要在任务作用域内。"""

    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "to_x": to_x,
        "to_y": to_y,
        "intent": intent,
        "dangerous": dangerous,
    }
    [start, end], gate = _prepare_points(
        desktop, screenshots, scope, screenshot_id, [(x, y), (to_x, to_y)],
        tool="drag", arguments=arguments, pace=pace,
    )
    _inject(gate, lambda: desktop.drag(start.x, start.y, end.x, end.y))
    return Dragged(start=start, end=end)


def scroll(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    x: int,
    y: int,
    notches: int,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Landed:
    """在截图像素 `(x, y)` 处滚动。`notches` 为正向上、为负向下。"""

    if notches == 0:
        raise ActionError("滚动格数不能为 0")
    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "notches": notches,
        "intent": intent,
        "dangerous": dangerous,
    }
    [landed], gate = _prepare_points(
        desktop, screenshots, scope, screenshot_id, [(x, y)],
        tool="scroll", arguments=arguments, pace=pace,
    )
    _inject(gate, lambda: desktop.scroll(landed.x, landed.y, notches))
    return landed


@dataclass(frozen=True)
class Pressed:
    """一次按键实际送到了哪个窗口，以及规范化之后的组合键。"""

    window: Window
    keys: tuple[str, ...]


def press_keys(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    keys: Sequence[str],
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Pressed:
    """把组合键送进截图所属窗口。先把窗口带到前台，送不进去就不按。

    交给系统而不是目标窗口的组合（Windows 键、Alt+Tab、Alt+Esc、Ctrl+Esc、Ctrl+Alt+Delete）
    一律拦截，裁决凭据也不能放行。
    """

    gate = pace or Pace(desktop)
    gate.reject_if_stopped()
    screenshot = screenshots.resolve(screenshot_id)
    window = scope.admit_window(desktop, screenshot.window)
    chord = _normalize_keys(keys)
    if (blocked := _blocked_chord(frozenset(chord))) is not None:
        raise Intercepted(blocked)
    arguments = {
        "screenshot_id": screenshot_id,
        "keys": list(keys),
        "intent": intent,
        "dangerous": dangerous,
    }
    verdict = judge_call("press_keys", arguments)
    cleared = gate.over_budget()
    if cleared:
        verdict = verdict | budget_verdict()
    require_ruling(desktop, "press_keys", arguments, verdict)
    confirm_outbound(desktop, scope, intent=intent, nearby=None, window=window)
    gate.wait_to_inject(cleared=cleared)
    try:
        desktop.focus(window.handle)
        desktop.press_keys(chord)
    except BaseException:
        gate.abandon()
        raise
    gate.mark_injected()
    return Pressed(window=window, keys=chord)


@dataclass(frozen=True)
class Launched:
    """一次启动找到的新窗口，以及被启动的进程号。"""

    window: Window
    process_id: int


def launch_app(
    desktop: DesktopPort,
    app: str,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
    timeout: float = 15,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Launched:
    """启动一个 .exe 并等待它的新窗口。不把新窗口放进任务作用域。

    终端、系统设置、资源管理器和脚本宿主在启动前就拒绝：那是绕过命中测试去开一扇高危窗口。
    超时仍找不到时抛 `ActionError`，说明期间新出现了哪些别的窗口。
    """

    executable, exe_name = _executable(app)
    if (risk := risk_of_process(exe_name)) is not None:
        raise Intercepted(f"不能启动 {exe_name}：它会打开高危窗口（{risk}）")
    if exe_name.lower() in _SCRIPT_HOSTS:
        raise Intercepted(f"不能启动 {exe_name}：命令解释器或脚本宿主不从这里启动")
    gate = pace or Pace(desktop)
    gate.reject_if_stopped()
    arguments = {"app": app, "intent": intent, "dangerous": dangerous}
    verdict = judge_call("launch_app", arguments)
    cleared = gate.over_budget()
    if cleared:
        verdict = verdict | budget_verdict()
    require_ruling(desktop, "launch_app", arguments, verdict)
    before = {w.handle for w in desktop.list_windows()}
    gate.wait_to_inject(cleared=cleared)
    try:
        process_id = desktop.launch(executable)
    except BaseException:
        gate.abandon()
        raise
    gate.mark_injected()
    found = _await_window(
        desktop, before, process_id, exe_name, timeout=timeout, clock=clock, sleep=sleep, gate=gate
    )
    return Launched(window=found, process_id=process_id)


@dataclass(frozen=True)
class Typed:
    """一次文本输入实际走了降级链的哪一档，以及这段文本是否曾写入剪贴板。"""

    window: Window
    tier: Literal["clipboard", "unicode"]
    clipboard_used: bool


def type_text(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    text: str,
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Typed:
    """把 `text` 打进截图 `screenshot_id` 所属窗口的焦点输入框。

    先把该窗口带到前台。优先把文本写入剪贴板再粘贴，并在事后恢复原来的剪贴板内容；
    这一档走不通时改为逐字符注入。粘贴已经成功而恢复失败时不再注入，免得文本进两次。
    模型自报为危险、又没有经人裁决时抛 `Intercepted`。
    """

    gate = pace or Pace(desktop)
    gate.reject_if_stopped()
    screenshot = screenshots.resolve(screenshot_id)
    window = scope.admit_window(desktop, screenshot.window)
    arguments = {
        "screenshot_id": screenshot_id,
        "text": text,
        "intent": intent,
        "dangerous": dangerous,
    }
    verdict = judge_call("type_text", arguments)
    cleared = gate.over_budget()
    if cleared:
        verdict = verdict | budget_verdict()
    require_ruling(desktop, "type_text", arguments, verdict)
    gate.wait_to_inject(cleared=cleared)
    try:
        desktop.focus(window.handle)
        tier, clipboard_used = _deliver(desktop, text, gate)
    except BaseException:
        gate.abandon()
        raise
    gate.mark_injected()
    scope.remember_text(window.handle, text)
    return Typed(window=window, tier=tier, clipboard_used=clipboard_used)


def resume(desktop: DesktopPort, pace: Pace) -> str:
    """解除急停。未急停时无事发生；急停中须有一张对 `resume` 的裁决凭据。"""

    if not desktop.read_pace().stopped:
        return "没有处于急停"
    require_ruling(desktop, "resume", {}, Verdict(("急停之后恢复输入须经人确认",)))
    pace.resume()
    return "已恢复，输入工具可以继续使用"


def _prepare_points(
    desktop: DesktopPort,
    screenshots: Screenshots,
    scope: TaskScope,
    screenshot_id: str,
    points: Sequence[tuple[int, int]],
    *,
    tool: str,
    arguments: Mapping[str, Any],
    pace: Pace | None,
) -> tuple[list[Landed], Pace]:
    """把截图像素换算到屏幕，逐个做命中测试，对第一个落点做截图比对与危险判定。

    返回时注入权已经占住，调用方注入之后要 `mark_injected`，没做成要 `abandon`。
    """

    gate = pace or Pace(desktop)
    gate.reject_if_stopped()
    screenshot = screenshots.resolve(screenshot_id)
    screen = [screenshot.to_screen(x, y) for x, y in points]
    windows = [scope.admit(desktop, x, y) for x, y in screen]
    confirm_unchanged(desktop, screenshot, screen[0][0], screen[0][1], windows[0])
    nearby = read_nearby(desktop, screenshot, screen[0][0], screen[0][1])
    verdict = judge_call(tool, arguments) | judge_nearby_text(nearby)
    cleared = gate.over_budget()
    if cleared:
        verdict = verdict | budget_verdict()
    require_ruling(desktop, tool, arguments, verdict)
    confirm_outbound(
        desktop,
        scope,
        intent=str(arguments.get("intent") or ""),
        nearby=nearby,
        window=windows[0],
    )
    gate.wait_to_inject(cleared=cleared)
    landed = [
        Landed(window=window, x=x, y=y) for window, (x, y) in zip(windows, screen, strict=True)
    ]
    return landed, gate


def _inject(gate: Pace, inject: Callable[[], None]) -> None:
    try:
        inject()
    except BaseException:
        gate.abandon()
        raise
    gate.mark_injected()


_MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})
_NAMED_KEYS = frozenset(
    {
        "enter",
        "tab",
        "escape",
        "space",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "left",
        "right",
        "up",
        "down",
    }
)
_SCRIPT_HOSTS = frozenset(
    {
        "wscript.exe",
        "cscript.exe",
        "mshta.exe",
        "rundll32.exe",
        "regsvr32.exe",
    }
)
_LAUNCH_POLL = 0.2


def _normalize_keys(keys: Sequence[str]) -> tuple[str, ...]:
    if not keys:
        raise ActionError("至少要有一个按键")
    normalized = tuple(key.strip().lower() for key in keys)
    unknown = [key for key in normalized if not _known_key(key)]
    if unknown:
        raise ActionError(f"不认识的按键：{'、'.join(unknown)}")
    if len(set(normalized)) != len(normalized):
        raise ActionError("组合键里有重复的按键")
    return normalized


def _known_key(key: str) -> bool:
    if key in _MODIFIERS or key in _NAMED_KEYS:
        return True
    if len(key) == 1 and key.isascii() and (key.isalpha() or key.isdigit()):
        return True
    if key.startswith("f") and key[1:].isdigit():
        number = int(key[1:])
        return 1 <= number <= 12
    return False


def _blocked_chord(keys: frozenset[str]) -> str | None:
    """这个组合为什么不能送进目标窗口；能送时为 `None`。"""

    if "win" in keys:
        return "含 Windows 键的组合会交给系统，而不是目标窗口"
    if "alt" in keys and "tab" in keys:
        return "Alt+Tab 会切换窗口，离开任务作用域"
    if "alt" in keys and "escape" in keys:
        return "Alt+Esc 会切换窗口，离开任务作用域"
    if "ctrl" in keys and "escape" in keys:
        return "Ctrl+Esc 会打开开始菜单或任务管理器，离开任务作用域"
    if {"ctrl", "alt", "delete"} <= keys:
        return "Ctrl+Alt+Delete 交给系统，而不是目标窗口"
    return None


def _executable(app: str) -> tuple[str, str]:
    """模型给出的应用名，收成一条不带参数的可执行文件路径，以及它的文件名。"""

    text = app.strip().strip('"')
    if not text or "\x00" in text:
        raise ActionError("没有给出要启动的应用")
    if any(character.isspace() for character in text) and not text.lower().endswith(".exe"):
        raise ActionError(f"只能启动一个程序，不能附带参数：{app}")
    name = text.replace("/", "\\").rsplit("\\", 1)[-1]
    if any(character.isspace() for character in name):
        raise ActionError(f"只能启动一个程序，不能附带参数：{app}")
    if not name.lower().endswith(".exe"):
        if "." in name:
            raise ActionError(f"只能启动 .exe 程序，{name} 不是")
        text += ".exe"
        name += ".exe"
    return text, name


def _await_window(
    desktop: DesktopPort,
    before: set[int],
    process_id: int,
    exe_name: str,
    *,
    timeout: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    gate: Pace,
) -> Window:
    deadline = clock() + timeout
    while True:
        gate.reject_if_stopped()
        appeared = [w for w in desktop.list_windows() if w.handle not in before]
        operable = {w.handle for w in operable_windows(desktop)}
        for candidate in appeared:
            if candidate.handle in operable and (
                candidate.process_id == process_id
                or candidate.process_name.lower() == exe_name.lower()
            ):
                return candidate
        if clock() >= deadline:
            raise ActionError(_timeout_message(appeared, exe_name, process_id, timeout))
        sleep(_LAUNCH_POLL)


def _timeout_message(
    appeared: Sequence[Window], exe_name: str, process_id: int, timeout: float
) -> str:
    if appeared:
        listed = "、".join(
            quoted_window(w.title, w.handle, process_name=w.process_name) for w in appeared
        )
        extra = f"期间新出现的窗口：{listed}。"
    else:
        extra = "期间没有新窗口出现。"
    return (
        f"已启动 {exe_name}（进程 {process_id}），但 {timeout:g} 秒后超时，没有出现它的新窗口。{extra}"
        "若这个应用已经在运行，它可能只是把原来的窗口带到了前台。"
    )


def _deliver(
    desktop: DesktopPort, text: str, pace: Pace
) -> tuple[Literal["clipboard", "unicode"], bool]:
    """先走剪贴板粘贴；读、写或粘贴失败时恢复剪贴板（若已经写过）再逐字符注入。

    每个字符注入前都再看一眼急停，长文本也能在中途停下。
    """

    try:
        snapshot = desktop.read_clipboard()
    except ClipboardUnavailable:
        _type_characters(desktop, text, pace)
        return "unicode", False
    try:
        desktop.set_clipboard_text(text)
    except ClipboardUnavailable:
        _restore(desktop, snapshot)
        _type_characters(desktop, text, pace)
        return "unicode", False
    try:
        pace.reject_if_stopped()
        desktop.paste()
    except ClipboardUnavailable:
        _restore(desktop, snapshot)
        _type_characters(desktop, text, pace)
        return "unicode", True
    except Intercepted:
        _restore(desktop, snapshot)
        raise
    _restore(desktop, snapshot)
    return "clipboard", True


def _restore(desktop: DesktopPort, snapshot: Clipboard) -> None:
    try:
        desktop.restore_clipboard(snapshot)
    except ClipboardUnavailable as error:
        raise ClipboardUnavailable("原剪贴板内容没能恢复") from error


def _type_characters(desktop: DesktopPort, text: str, pace: Pace) -> None:
    for character in text:
        pace.reject_if_stopped()
        desktop.type_character(character)
