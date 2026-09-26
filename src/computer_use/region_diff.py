"""两幅窗口采集在落点周围的像素比较。

`DesktopPort` 的实现调用这里。核心只消费比较结果，不逐像素比较。
"""

from __future__ import annotations

import PIL.ImageChops

from computer_use.desktop import Capture, RegionChange

TARGET_RADIUS = 48
"""目标区域：落点上下左右各这么多物理像素，界面变化只在这块区域里才算数。"""

PIXEL_TOLERANCE = 32
"""一个像素任一通道的差不超过此值时视为没变，悬停一类的轻微变色不算。"""

CHANGED_FRACTION = 0.02
"""目标区域里变了的像素超过这个比例才算实质变化；闪烁的光标只占百分之一不到。"""


def diff_region(before: Capture, after: Capture, x: int, y: int) -> RegionChange:
    """比较两幅采集在屏幕物理像素 `(x, y)` 周围目标区域里的像素。

    `before.rect` 与 `after.rect` 须相同。
    """

    rect = before.rect
    box = (
        max(x - TARGET_RADIUS, rect.left) - rect.left,
        max(y - TARGET_RADIUS, rect.top) - rect.top,
        min(x + TARGET_RADIUS + 1, rect.left + rect.width) - rect.left,
        min(y + TARGET_RADIUS + 1, rect.top + rect.height) - rect.top,
    )
    original = before.image.convert("RGB").crop(box)
    current = after.image.convert("RGB").crop(box)
    red, green, blue = PIL.ImageChops.difference(original, current).split()
    largest = PIL.ImageChops.lighter(PIL.ImageChops.lighter(red, green), blue)
    changed = sum(largest.histogram()[PIXEL_TOLERANCE + 1 :])
    total = original.width * original.height
    return RegionChange(
        changed=changed > CHANGED_FRACTION * total,
        changed_pixels=changed,
        total_pixels=total,
    )
