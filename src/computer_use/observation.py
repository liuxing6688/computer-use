"""核心：观察一个窗口，记住每张截图对应的窗口与几何，并在动作之前核对截图是否仍然反映现状。

纯逻辑，只依赖 `DesktopPort`。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import PIL.Image
from PIL.Image import Image

from computer_use.action_log import Intercepted
from computer_use.desktop import Capture, DesktopPort, Rect, Window, WindowUnavailable
from computer_use.untrusted import quoted_window
from computer_use.windows import operable_windows

MAX_LONG_EDGE = 1568
"""模型端会把长边超过此值的图像压小，而且不告诉我们压了多少；超过时由服务端先缩，缩放比才可知。"""


class ObservationError(Exception):
    """观察无法进行；消息原样回给模型。"""


@dataclass(frozen=True)
class Target:
    """界面上一个可被单独指称的对象：一段描述，以及它在这张截图上的边界框。

    `rect` 的单位是这张截图的像素。由哪个感知通道产出不影响这项的形状。
    """

    description: str
    rect: Rect


@dataclass(frozen=True)
class Screenshot:
    """一张交给模型的截图，以及把它的像素坐标换算回屏幕所需的全部事实。

    `region` 是图像所画的屏幕区域（物理像素），`scale` 是图像像素与物理像素之比。
    """

    id: str
    window: Window
    captured_at: datetime
    image: Image
    region: Rect
    scale: float
    capture: Capture
    targets: tuple[Target, ...] = ()

    @property
    def window_offset(self) -> tuple[int, int]:
        """`region` 相对整窗截图左上角的偏移，物理像素。"""

        return (
            self.region.left - self.capture.rect.left,
            self.region.top - self.capture.rect.top,
        )

    def to_screen(self, x: int, y: int) -> tuple[int, int]:
        """截图上的像素 `(x, y)` 在屏幕上的物理像素坐标，取像素中心所落的那个物理像素。"""

        if not (0 <= x < self.image.width and 0 <= y < self.image.height):
            raise ObservationError(
                f"坐标 ({x}, {y}) 不在截图 {self.id} 的范围内"
                f"（{self.image.width}×{self.image.height}）"
            )
        return (
            self.region.left + int((x + 0.5) * self.region.width / self.image.width),
            self.region.top + int((y + 0.5) * self.region.height / self.image.height),
        )


class Screenshots:
    """服务端记住的截图，按 ID 解析。

    每张截图都握着一整张原始采集，因此只记住最近的 `capacity` 张，更早的 ID 解析即失败。
    """

    def __init__(self, capacity: int = 32) -> None:
        self._capacity = capacity
        self._by_id: dict[str, Screenshot] = {}

    def add(
        self,
        *,
        window: Window,
        captured_at: datetime,
        capture: Capture,
        region: Rect,
    ) -> Screenshot:
        image = capture.image.crop(
            (
                region.left - capture.rect.left,
                region.top - capture.rect.top,
                region.left - capture.rect.left + region.width,
                region.top - capture.rect.top + region.height,
            )
        )
        scale = min(1.0, MAX_LONG_EDGE / max(region.width, region.height))
        if scale < 1.0:
            image = image.resize(
                (max(1, round(region.width * scale)), max(1, round(region.height * scale))),
                PIL.Image.Resampling.LANCZOS,
            )
        screenshot = Screenshot(
            id=f"shot-{secrets.token_hex(4)}",
            window=window,
            captured_at=captured_at,
            image=image,
            region=region,
            scale=scale,
            capture=capture,
        )
        self._by_id[screenshot.id] = screenshot
        while len(self._by_id) > self._capacity:
            del self._by_id[next(iter(self._by_id))]
        return screenshot

    def resolve(self, screenshot_id: str) -> Screenshot:
        try:
            return self._by_id[screenshot_id]
        except KeyError:
            raise ObservationError(f"截图 {screenshot_id} 不存在或已过期，请重新观察") from None


def zoom(screenshots: Screenshots, screenshot_id: str, rect: Rect) -> Screenshot:
    """把一张截图上的矩形（该截图的像素坐标）按原尺寸重新裁出，登记为一张新截图。

    裁自那张截图背后的原始采集，而不是重新截图：模型看到的放大图与它决策时看的是同一时刻。
    """

    source = screenshots.resolve(screenshot_id)
    if (
        rect.width <= 0
        or rect.height <= 0
        or rect.left < 0
        or rect.top < 0
        or rect.left + rect.width > source.image.width
        or rect.top + rect.height > source.image.height
    ):
        raise ObservationError(
            f"矩形 {rect} 不在截图 {screenshot_id} 的范围内"
            f"（{source.image.width}×{source.image.height}）"
        )
    x_ratio = source.region.width / source.image.width
    y_ratio = source.region.height / source.image.height
    left = round(rect.left * x_ratio)
    top = round(rect.top * y_ratio)
    region = Rect(
        left=source.region.left + left,
        top=source.region.top + top,
        width=round((rect.left + rect.width) * x_ratio) - left,
        height=round((rect.top + rect.height) * y_ratio) - top,
    )
    return screenshots.add(
        window=source.window,
        captured_at=source.captured_at,
        capture=source.capture,
        region=region,
    )


def confirm_unchanged(
    desktop: DesktopPort, screenshot: Screenshot, x: int, y: int, hit: Window
) -> None:
    """重新采集窗口，用平台边界对屏幕物理像素 `(x, y)` 周围的比较结果核对截图是否仍反映现状。

    `hit` 是命中测试找到的落点处的窗口。它不是截图所属的窗口（截图之后弹出的对话框挡住了落点），
    窗口已关闭、移动或改变大小，或目标区域发生实质变化时抛 `Intercepted`。
    """

    shot = screenshot.window
    if (hit.handle, hit.process_id) != (shot.handle, shot.process_id):
        raise Intercepted(
            f"落点 ({x}, {y}) 处现在是{quoted_window(hit.title, hit.handle)}，"
            f"不是截图 {screenshot.id} 所属的窗口，请重新观察"
        )
    try:
        current = desktop.capture_window(screenshot.window.handle)
    except WindowUnavailable:
        raise Intercepted(f"截图 {screenshot.id} 所属的窗口已经关闭，请重新观察") from None
    original = screenshot.capture
    if current.rect != original.rect:
        raise Intercepted(
            f"截图 {screenshot.id} 之后窗口已移动或改变大小，截图上的坐标不再准确，请重新观察"
        )
    change = desktop.compare_region(original, current, x, y)
    if change.changed:
        raise Intercepted(
            f"截图 {screenshot.id} 之后落点附近的界面已经变化"
            f"（{change.changed_pixels}/{change.total_pixels} 个像素不同），请重新观察"
        )


def observe(desktop: DesktopPort, screenshots: Screenshots, handle: int) -> Screenshot:
    """截取一个可操作的窗口，登记为一张新截图。"""

    window = next((w for w in operable_windows(desktop) if w.handle == handle), None)
    if window is None:
        raise ObservationError(f"窗口 {handle} 不存在或不可操作（不可见、最小化或无标题）")
    try:
        capture = desktop.capture_window(handle)
    except WindowUnavailable:
        raise ObservationError(f"窗口 {handle} 在截图时已经关闭") from None
    return screenshots.add(
        window=window,
        captured_at=datetime.now(timezone.utc),
        capture=capture,
        region=capture.rect,
    )
