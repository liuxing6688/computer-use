"""核心：输入动作。按截图像素坐标定位，放行前依次做命中测试、截图比对与危险动作判定。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from computer_use.danger import Verdict, judge_call, judge_nearby_text, read_nearby
from computer_use.desktop import Clipboard, ClipboardUnavailable, DesktopPort, Window
from computer_use.interception import require_ruling
from computer_use.observation import Screenshots, confirm_unchanged
from computer_use.pace import Pace, budget_verdict
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
    *,
    intent: str,
    dangerous: bool,
    pace: Pace | None = None,
) -> Landed:
    """在截图 `screenshot_id` 的像素 `(x, y)` 处单击。

    落点未通过命中测试、或截图之后落点附近的界面已经变化时抛 `Intercepted`，裁决凭据也救不回来；
    模型自报或落点附近的文字判为危险、又没有经人裁决时同样抛 `Intercepted`。
    """

    gate = pace or Pace(desktop)
    gate.reject_if_stopped()
    screenshot = screenshots.resolve(screenshot_id)
    screen_x, screen_y = screenshot.to_screen(x, y)
    window = scope.admit(desktop, screen_x, screen_y)
    confirm_unchanged(desktop, screenshot, screen_x, screen_y, window)
    arguments = {
        "screenshot_id": screenshot_id,
        "x": x,
        "y": y,
        "intent": intent,
        "dangerous": dangerous,
    }
    verdict = judge_call("click", arguments) | judge_nearby_text(
        read_nearby(desktop, screenshot, screen_x, screen_y)
    )
    if gate.over_budget():
        verdict = verdict | budget_verdict()
    require_ruling(desktop, "click", arguments, verdict)
    gate.wait_to_inject()
    desktop.click(screen_x, screen_y)
    gate.mark_injected()
    return Landed(window=window, x=screen_x, y=screen_y)


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
    if gate.over_budget():
        verdict = verdict | budget_verdict()
    require_ruling(desktop, "type_text", arguments, verdict)
    gate.wait_to_inject()
    desktop.focus(window.handle)
    tier, clipboard_used = _deliver(desktop, text)
    gate.mark_injected()
    return Typed(window=window, tier=tier, clipboard_used=clipboard_used)


def resume(desktop: DesktopPort, pace: Pace) -> str:
    """解除急停。未急停时无事发生；急停中须有一张对 `resume` 的裁决凭据。"""

    if not desktop.read_pace().stopped:
        return "没有处于急停"
    require_ruling(desktop, "resume", {}, Verdict(("急停之后恢复输入须经人确认",)))
    pace.resume()
    return "已恢复，输入工具可以继续使用"


def _deliver(desktop: DesktopPort, text: str) -> tuple[Literal["clipboard", "unicode"], bool]:
    """先走剪贴板粘贴；读、写或粘贴失败时恢复剪贴板（若已经写过）再逐字符注入。"""

    try:
        snapshot = desktop.read_clipboard()
    except ClipboardUnavailable:
        _type_characters(desktop, text)
        return "unicode", False
    try:
        desktop.set_clipboard_text(text)
    except ClipboardUnavailable:
        _restore(desktop, snapshot)
        _type_characters(desktop, text)
        return "unicode", False
    try:
        desktop.paste()
    except ClipboardUnavailable:
        _restore(desktop, snapshot)
        _type_characters(desktop, text)
        return "unicode", True
    _restore(desktop, snapshot)
    return "clipboard", True


def _restore(desktop: DesktopPort, snapshot: Clipboard) -> None:
    try:
        desktop.restore_clipboard(snapshot)
    except ClipboardUnavailable as error:
        raise ClipboardUnavailable("原剪贴板内容没能恢复") from error


def _type_characters(desktop: DesktopPort, text: str) -> None:
    for character in text:
        desktop.type_character(character)
