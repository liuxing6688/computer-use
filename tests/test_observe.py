"""只读工具「观察窗口」与「放大」的行为。"""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from computer_use.desktop import Rect
from computer_use.observation import ObservationError, Screenshots
from computer_use.tools import observe_window, zoom

from .fake_desktop import FakeDesktop, window


def _decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def test_观察只返回目标窗口的截图() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, title="别的窗口", rect=Rect(0, 0, 400, 300)),
            window(handle=2, title="目标", rect=Rect(500, 200, 320, 240)),
        ],
        images={
            1: Image.new("RGB", (400, 300), "blue"),
            2: Image.new("RGB", (320, 240), "red"),
        },
    )

    observed = observe_window(desktop, Screenshots(), handle=2)

    image = _decode(observed.png)
    assert image.size == (320, 240)
    assert image.getcolors() == [(320 * 240, (255, 0, 0))]


def test_观察附带窗口标识_采集时刻_缩放比_屏幕偏移与截图_ID() -> None:
    desktop = FakeDesktop(
        [
            window(
                handle=0x1234,
                title="无标题 - 记事本",
                process_name="notepad.exe",
                rect=Rect(left=500, top=200, width=320, height=240),
            )
        ],
        dpi_scale=1.25,
    )

    before = datetime.now(timezone.utc)
    observed = observe_window(desktop, Screenshots(), handle=0x1234)
    after = datetime.now(timezone.utc)

    assert observed.targets == ()
    metadata = dict(observed.metadata)
    screenshot_id = metadata.pop("screenshot_id")
    captured_at = datetime.fromisoformat(metadata.pop("captured_at"))
    assert isinstance(screenshot_id, str) and screenshot_id
    assert before <= captured_at <= after
    assert metadata == {
        "window": {
            "handle": 0x1234,
            "title": "<untrusted-screen>无标题 - 记事本</untrusted-screen>",
            "process_name": "notepad.exe",
        },
        "size": {"width": 320, "height": 240},
        "scale": 1.0,
        "dpi_scale": 1.25,
        "screen_offset": {"x": 500, "y": 200},
        "window_offset": {"x": 0, "y": 0},
    }


@pytest.mark.parametrize("handle", [2, 404], ids=["最小化的窗口", "不存在的窗口"])
def test_拒绝观察不可操作的窗口(handle: int) -> None:
    desktop = FakeDesktop(
        [window(handle=1), window(handle=2, title="缩到任务栏", is_minimized=True)]
    )

    with pytest.raises(ObservationError):
        observe_window(desktop, Screenshots(), handle=handle)


def test_截图前一刻关掉的窗口被拒绝() -> None:
    desktop = FakeDesktop([window(handle=1)], gone={1})

    with pytest.raises(ObservationError):
        observe_window(desktop, Screenshots(), handle=1)


def test_截图_ID_可解析回对应的窗口与屏幕坐标() -> None:
    desktop = FakeDesktop(
        [
            window(handle=1, rect=Rect(0, 0, 400, 300)),
            window(handle=2, rect=Rect(500, 200, 320, 240)),
        ]
    )
    screenshots = Screenshots()
    first = observe_window(desktop, screenshots, handle=1)
    second = observe_window(desktop, screenshots, handle=2)

    resolved = screenshots.resolve(second.metadata["screenshot_id"])

    assert resolved.window.handle == 2
    assert resolved.to_screen(10, 20) == (510, 220)
    assert screenshots.resolve(first.metadata["screenshot_id"]).window.handle == 1


def test_超出模型图像上限的窗口由服务端缩小_坐标仍换算到物理像素() -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(100, 50, 3136, 1000))])
    screenshots = Screenshots()

    observed = observe_window(desktop, screenshots, handle=1)

    assert _decode(observed.png).size == (1568, 500)
    assert observed.metadata["scale"] == 0.5
    assert observed.metadata["size"] == {"width": 1568, "height": 500}
    resolved = screenshots.resolve(observed.metadata["screenshot_id"])
    assert resolved.to_screen(10, 20) == (121, 91)


def test_放大返回指定矩形的原尺寸裁剪与它相对窗口的偏移() -> None:
    frame = Image.new("RGB", (3136, 1000), "white")
    frame.paste("red", (2000, 400, 2100, 480))
    desktop = FakeDesktop(
        [window(handle=1, rect=Rect(100, 50, 3136, 1000))], images={1: frame}
    )
    screenshots = Screenshots()
    observed = observe_window(desktop, screenshots, handle=1)

    zoomed = zoom(
        screenshots, observed.metadata["screenshot_id"], Rect(990, 190, 70, 60)
    )

    crop = _decode(zoomed.png)
    assert crop.size == (140, 120)
    assert crop.getpixel((19, 19)) == (255, 255, 255)
    assert crop.getpixel((20, 20)) == (255, 0, 0)
    assert crop.getpixel((119, 99)) == (255, 0, 0)
    assert crop.getpixel((120, 100)) == (255, 255, 255)
    assert zoomed.metadata["scale"] == 1.0
    assert zoomed.metadata["window_offset"] == {"x": 1980, "y": 380}
    assert zoomed.metadata["screen_offset"] == {"x": 2080, "y": 430}
    assert zoomed.metadata["captured_at"] == observed.metadata["captured_at"]


def test_放大返回与观察同一形状的空目标清单() -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(100, 50, 320, 240))])
    screenshots = Screenshots()
    observed = observe_window(desktop, screenshots, handle=1)

    zoomed = zoom(
        screenshots, observed.metadata["screenshot_id"], Rect(10, 20, 30, 40)
    )

    assert zoomed.targets == observed.targets == ()
    assert set(zoomed.metadata) == set(observed.metadata)
    assert zoomed.metadata["screenshot_id"] != observed.metadata["screenshot_id"]


def test_放大的截图_ID_同样可解析回屏幕坐标() -> None:
    desktop = FakeDesktop([window(handle=7, rect=Rect(100, 50, 3136, 1000))])
    screenshots = Screenshots()
    observed = observe_window(desktop, screenshots, handle=7)
    zoomed = zoom(
        screenshots, observed.metadata["screenshot_id"], Rect(990, 190, 70, 60)
    )

    resolved = screenshots.resolve(zoomed.metadata["screenshot_id"])

    assert zoomed.metadata["screenshot_id"] != observed.metadata["screenshot_id"]
    assert resolved.window.handle == 7
    assert resolved.to_screen(20, 20) == (2100, 450)


@pytest.mark.parametrize(
    "rect",
    [Rect(300, 200, 30, 50), Rect(-1, 0, 10, 10), Rect(10, 10, 0, 10)],
    ids=["越出右下", "越出左上", "空矩形"],
)
def test_放大拒绝不在截图内的矩形(rect: Rect) -> None:
    desktop = FakeDesktop([window(handle=1, rect=Rect(0, 0, 320, 240))])
    screenshots = Screenshots()
    observed = observe_window(desktop, screenshots, handle=1)

    with pytest.raises(ObservationError):
        zoom(screenshots, observed.metadata["screenshot_id"], rect)


def test_服务端只记住最近的若干张截图() -> None:
    desktop = FakeDesktop([window(handle=1)])
    screenshots = Screenshots(capacity=2)
    oldest, middle, newest = (
        observe_window(desktop, screenshots, handle=1).metadata["screenshot_id"]
        for _ in range(3)
    )

    with pytest.raises(ObservationError):
        screenshots.resolve(oldest)
    assert screenshots.resolve(middle).id == middle
    assert screenshots.resolve(newest).id == newest


def test_解析未知的截图_ID_被拒绝() -> None:
    with pytest.raises(ObservationError):
        Screenshots().resolve("shot-不存在")
