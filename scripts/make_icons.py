"""Generate Alfred's PWA icons.

Kept as a script rather than committed binaries so the mark can be retuned
without hand-editing PNGs. Run it after changing the palette:

    .venv/Scripts/python.exe scripts/make_icons.py

The mark echoes the HUD's reactor rings so the home-screen icon and the
in-app control read as the same object, with the bat silhouette at the centre.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"

VOID = (7, 9, 12, 255)
GUNMETAL = (18, 22, 29, 255)
EDGE = (51, 64, 79, 255)
SIGNAL = (240, 168, 48, 255)
SIGNAL_DIM = (125, 90, 29, 255)

# Right half of the bat, traced as a continuous outline rather than a list of
# extremes: out along the TOP edge from the head to the wing tip, then back
# along the scalloped BOTTOM edge to the tail. Ordering matters - listing the
# high and low points alternately produces a zigzag crown, not a bat.
BAT_TOP = [
    (0.500, 0.368),  # dip between the ears
    (0.542, 0.288),  # right ear
    (0.578, 0.392),  # valley behind the ear
    (0.650, 0.395),
    (0.760, 0.378),
    (0.870, 0.368),
    (0.965, 0.390),  # wing tip
]
BAT_BOTTOM = [
    (0.900, 0.455),  # underside of the tip - the wing is thinnest here
    (0.845, 0.480),
    (0.800, 0.545),  # scallop
    (0.742, 0.522),  # notch
    (0.692, 0.572),  # scallop
    (0.624, 0.548),  # notch
    (0.562, 0.626),  # scallop into the body
    (0.500, 0.720),  # base of the body
]


def bat_polygon(size: int, scale: float, dy: float) -> list[tuple[float, float]]:
    half = BAT_TOP + BAT_BOTTOM
    # Mirror everything except the two on-axis points, which must not repeat.
    left = [(1.0 - x, y) for x, y in reversed(half[1:-1])]
    out: list[tuple[float, float]] = []
    for x, y in half + left:
        cx = (x - 0.5) * scale + 0.5
        cy = (y - 0.5) * scale + 0.5 + dy
        out.append((cx * size, cy * size))
    return out


def draw_icon(size: int, *, maskable: bool = False) -> Image.Image:
    # Supersample and downscale: PIL has no anti-aliasing on polygons or arcs,
    # and at 192px the difference between 4x and 1x is the difference between
    # a crisp mark and a jagged one.
    ss = 4
    canvas = size * ss
    image = Image.new("RGBA", (canvas, canvas), VOID)
    draw = ImageDraw.Draw(image)

    # Maskable icons get cropped to a circle by the launcher, so the artwork
    # has to sit inside the safe zone (the middle 80%).
    inset = canvas * (0.14 if maskable else 0.045)
    radius = int(canvas * (0.5 if maskable else 0.22))
    draw.rounded_rectangle(
        [inset, inset, canvas - inset, canvas - inset],
        radius=radius,
        fill=GUNMETAL,
        outline=EDGE,
        width=max(1, int(canvas * 0.006)),
    )

    art_scale = 0.74 if maskable else 0.86
    centre = canvas / 2

    def ring(fraction: float, width: float, colour: tuple[int, int, int, int]) -> None:
        r = canvas * fraction * art_scale
        draw.ellipse(
            [centre - r, centre - r, centre + r, centre + r],
            outline=colour,
            width=max(1, int(canvas * width)),
        )

    ring(0.400, 0.0075, EDGE)
    ring(0.330, 0.0060, SIGNAL_DIM)

    # Broken outer arc, echoing the dashed ring on the HUD orb.
    r = canvas * 0.400 * art_scale
    box = [centre - r, centre - r, centre + r, centre + r]
    for start, end in ((-64, 18), (116, 198), (246, 296)):
        draw.arc(box, start, end, fill=SIGNAL, width=max(1, int(canvas * 0.011)))

    draw.polygon(bat_polygon(canvas, art_scale * 0.98, -0.012 * art_scale), fill=SIGNAL)

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (180, 192, 512):
        draw_icon(size).save(OUT / f"alfred-{size}.png")
    draw_icon(512, maskable=True).save(OUT / "alfred-maskable.png")

    (OUT / "alfred.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect width="24" height="24" rx="5" fill="#12161d"/>'
        '<circle cx="12" cy="12" r="8.4" fill="none" stroke="#7d5a1d" stroke-width=".8"/>'
        '<path d="M12 7.6c.5 1.6 1.6 2.6 3.3 2.9 1.3.2 2.5 0 3.7-.6-.7 1.6-.5 2.9.4 4-1.3.1-2.3.6-2.9 1.5-.6.8-.8 1.7-.7 2.8-1-.7-2-.9-2.9-.6-.7.2-1.3.7-1.8 1.5-.5-.8-1.1-1.3-1.8-1.5-.9-.3-1.9-.1-2.9.6.1-1.1-.1-2-.7-2.8-.6-.9-1.6-1.4-2.9-1.5.9-1.1 1.1-2.4.4-4 1.2.6 2.4.8 3.7.6 1.7-.3 2.8-1.3 3.3-2.9z" fill="#f0a830"/>'
        "</svg>",
        encoding="utf-8",
    )
    print(f"Wrote icons to {OUT}")


if __name__ == "__main__":
    main()
