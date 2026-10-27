"""Generate the integration icon and logo PNGs.

The icon is a speech bubble (a nod to voice assistants) holding a thermostat
dial with a cool-to-warm gradient and a thermometer in the middle.

Usage:
    python scripts/generate_icon.py

Requires Pillow. Writes:
    custom_components/alexa_thermostat/brand/icon.png      (256x256)
    custom_components/alexa_thermostat/brand/icon@2x.png   (512x512)
    assets/icon.png                                        (512x512, README)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SUPERSAMPLE = 2048

BUBBLE = (20, 33, 58, 255)
TRACK = (44, 60, 92, 255)
WHITE = (255, 255, 255, 255)
COOL = (0, 202, 255)
MID = (124, 97, 255)
WARM = (255, 138, 61)

ARC_START = 135.0
ARC_SWEEP = 270.0
KNOB_POSITION = 0.68


def _lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def _gradient(t: float) -> tuple[int, int, int, int]:
    color = _lerp(COOL, MID, t * 2) if t < 0.5 else _lerp(MID, WARM, (t - 0.5) * 2)
    return (*color, 255)


def _point(cx: float, cy: float, radius: float, degrees: float) -> tuple[float, float]:
    rad = math.radians(degrees)
    return cx + radius * math.cos(rad), cy + radius * math.sin(rad)


def _circle(draw: ImageDraw.ImageDraw, center: tuple[float, float], radius: float, fill) -> None:
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def render(size: int = SUPERSAMPLE) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    s = size

    # Speech bubble body and tail.
    cx, cy, bubble_r = 0.52 * s, 0.47 * s, 0.42 * s
    _circle(draw, (cx, cy), bubble_r, BUBBLE)
    draw.polygon(
        [
            _point(cx, cy, bubble_r * 0.92, 112),
            (0.10 * s, 0.94 * s),
            _point(cx, cy, bubble_r * 0.92, 150),
        ],
        fill=BUBBLE,
    )

    # Dial track and gradient arc.
    ring_r, width = 0.31 * s, round(0.075 * s)
    box = (cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r)
    draw.arc(box, ARC_START, ARC_START + ARC_SWEEP, fill=TRACK, width=width)
    for end_angle in (ARC_START, ARC_START + ARC_SWEEP):
        _circle(draw, _point(cx, cy, ring_r - width / 2, end_angle), width / 2, TRACK)

    filled = ARC_SWEEP * KNOB_POSITION
    steps = 360
    for i in range(steps):
        a0 = ARC_START + filled * i / steps
        a1 = ARC_START + filled * (i + 1) / steps + 0.4
        draw.arc(box, a0, a1, fill=_gradient(i / steps), width=width)
    _circle(draw, _point(cx, cy, ring_r - width / 2, ARC_START), width / 2, _gradient(0))

    # Knob at the end of the filled arc.
    knob = _point(cx, cy, ring_r - width / 2, ARC_START + filled)
    _circle(draw, knob, width * 0.78, WHITE)
    _circle(draw, knob, width * 0.42, _gradient(1))

    # Thermometer in the middle.
    stem_w, stem_top, stem_bottom = 0.075 * s, cy - 0.15 * s, cy + 0.06 * s
    bulb_c, bulb_r = (cx, cy + 0.085 * s), 0.07 * s
    draw.rounded_rectangle(
        (cx - stem_w / 2, stem_top, cx + stem_w / 2, stem_bottom),
        radius=stem_w / 2,
        fill=WHITE,
    )
    _circle(draw, bulb_c, bulb_r, WHITE)
    inner_w = stem_w * 0.42
    draw.rounded_rectangle(
        (cx - inner_w / 2, cy - 0.06 * s, cx + inner_w / 2, stem_bottom + 0.01 * s),
        radius=inner_w / 2,
        fill=_gradient(1),
    )
    _circle(draw, bulb_c, bulb_r * 0.62, _gradient(1))

    return image


def main() -> None:
    master = render()
    outputs = {
        ROOT / "custom_components/alexa_thermostat/brand/icon.png": 256,
        ROOT / "custom_components/alexa_thermostat/brand/icon@2x.png": 512,
        ROOT / "assets/icon.png": 512,
    }
    for path, size in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        master.resize((size, size), Image.Resampling.LANCZOS).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
