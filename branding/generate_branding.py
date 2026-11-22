#!/usr/bin/env python3
"""
Generate minimal monochrome Switch OLED-friendly 32-bit BI_RGB BMP branding for MooSwitch.

Generates:
  - bootloader/bootlogo.bmp  (720x1280 portrait: 1280x720 landscape rotated 90 deg CCW)

Hekate/Nyx, Sphaira, and Ultrahand intentionally use their clean built-in default UI themes.
If branding/custom/bootlogo.png exists, it is resized/converted into a 32-bit BGRA BMP
cleanly instead of using procedural art.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES_BOLD = (
    "/usr/share/fonts/gsfonts/NimbusSans-Bold.otf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

FONT_CANDIDATES_MONO = (
    "/usr/share/fonts/Adwaita/AdwaitaMono-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/gnu-free/FreeMono.otf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _load_font(size: int, mono: bool = False) -> ImageFont.ImageFont:
    candidates = FONT_CANDIDATES_MONO if mono else FONT_CANDIDATES_BOLD
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def write_bmp32_bgra(img: Image.Image, out_path: Path) -> None:
    """
    Write an exact 54-byte header + bottom-up 32-bit BI_RGB BGRA BMP
    matching Hekate (bootloader/main.c) and Nyx (bmp_to_lvimg_obj).
    """
    rgba = img.convert("RGBA")
    width, height = rgba.size
    r, g, b, a = rgba.split()
    bgra = Image.merge("RGBA", (b, g, r, a)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    pixel_bytes = bgra.tobytes()
    img_size = len(pixel_bytes)
    assert img_size == width * height * 4

    bf_type = b"BM"
    bf_off_bits = 54
    bf_size = bf_off_bits + img_size
    file_hdr = struct.pack("<2sIHHI", bf_type, bf_size, 0, 0, bf_off_bits)
    info_hdr = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize (BITMAPINFOHEADER)
        width,       # biWidth
        height,      # biHeight > 0 => bottom-up scanlines
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        img_size,    # biSizeImage
        2835,        # biXPelsPerMeter (~72 DPI)
        2835,        # biYPelsPerMeter (~72 DPI)
        0,           # biClrUsed
        0,           # biClrImportant
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fp:
        fp.write(file_hdr)
        fp.write(info_hdr)
        fp.write(pixel_bytes)


def _cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 36,
) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        u = 1.0 - t
        x = (u**3) * p0[0] + 3 * (u**2) * t * p1[0] + 3 * u * (t**2) * p2[0] + (t**3) * p3[0]
        y = (u**3) * p0[1] + 3 * (u**2) * t * p1[1] + 3 * u * (t**2) * p2[1] + (t**3) * p3[1]
        pts.append((x, y))
    return pts


def _draw_outer_rounded_half(
    draw: ImageDraw.ImageDraw,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    radius: int,
    side: str,
    fill: int,
) -> None:
    """Draw a Switch Joy-Con half rounded on its outer corners and flat on its inner spine."""
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)
    mid_x = (x0 + x1) // 2
    if side == "left":
        draw.rectangle([mid_x, y0, x1, y1], fill=fill)
    else:
        draw.rectangle([x0, y0, mid_x, y1], fill=fill)


def _draw_mooswitch_emblem_2x(draw: ImageDraw.ImageDraw, cx: int, body_top: int) -> None:
    """
    Draw a minimal pure black-and-white MooSwitch glyph at 2x scale:
    twin Joy-Con halves crowned by sleek monochrome cow horns.
    """
    white = 255
    black = 0

    half_w = 84
    body_h = 188
    gap = 16
    radius = 38
    stroke = 11

    lx1 = cx - gap // 2
    lx0 = lx1 - half_w
    rx0 = cx + gap // 2
    rx1 = rx0 + half_w
    by1 = body_top + body_h

    # Left Joy-Con: solid white + dark analog dot
    _draw_outer_rounded_half(draw, lx0, body_top, lx1, by1, radius=radius, side="left", fill=white)
    dot_r = 14
    ldx, ldy = lx0 + 42, body_top + 58
    draw.ellipse([ldx - dot_r, ldy - dot_r, ldx + dot_r, ldy + dot_r], fill=black)

    # Right Joy-Con: clean white border on pure black + white analog dot
    _draw_outer_rounded_half(draw, rx0, body_top, rx1, by1, radius=radius, side="right", fill=white)
    _draw_outer_rounded_half(
        draw,
        rx0 + stroke,
        body_top + stroke,
        rx1 - stroke,
        by1 - stroke,
        radius=max(1, radius - stroke),
        side="right",
        fill=black,
    )
    rdx, rdy = rx1 - 42, by1 - 60
    draw.ellipse([rdx - dot_r, rdy - dot_r, rdx + dot_r, rdy + dot_r], fill=white)

    # Twin minimal curved horns rising symmetrically from upper outer shoulders
    left_horn_outer = _cubic_bezier(
        (lx0 + 6, body_top + 26),
        (lx0 - 30, body_top - 16),
        (lx0 - 26, body_top - 60),
        (lx0 + 8, body_top - 86),
    )
    left_horn_inner = _cubic_bezier(
        (lx0 + 8, body_top - 86),
        (lx0 - 2, body_top - 52),
        (lx0 + 6, body_top - 20),
        (lx0 + radius, body_top + stroke // 2),
    )
    left_horn = left_horn_outer + left_horn_inner[1:]
    right_horn = [(2 * cx - x, y) for x, y in left_horn]
    draw.polygon(left_horn, fill=white)
    draw.polygon(right_horn, fill=white)


def _draw_tracked_text_centered(
    draw: ImageDraw.ImageDraw,
    cx: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    tracking: int,
    fill: int,
) -> None:
    if not text:
        return
    advances = [draw.textlength(ch, font=font) for ch in text]
    total_w = sum(advances) + tracking * (len(text) - 1)
    x = cx - total_w / 2.0
    for ch, adv in zip(text, advances):
        draw.text((x, y), ch, font=font, fill=fill)
        x += adv + tracking


def _render_bootlogo_minimal_bw_landscape(pack_cfg: dict) -> Image.Image:
    """Render an ultra-minimal pure B&W MooSwitch bootscreen (1280x720 landscape)."""
    w2, h2 = 2560, 1440
    cx = w2 // 2
    canvas = Image.new("L", (w2, h2), 0)
    draw = ImageDraw.Draw(canvas)

    # Emblem + Wordmark + Hairline + Subtitle stacked near optical center
    _draw_mooswitch_emblem_2x(draw, cx=cx, body_top=464)

    f_title = _load_font(86, mono=False)
    f_sub = _load_font(28, mono=True)

    brand = pack_cfg.get("brand_title", "MOOSWITCH").upper()
    _draw_tracked_text_centered(draw, cx=cx, y=716, text=brand, font=f_title, tracking=18, fill=255)

    # Subtle 1px hairline (2px at 2x)
    rule_half_w = 96
    rule_y = 824
    draw.rectangle([cx - rule_half_w, rule_y, cx + rule_half_w, rule_y + 2], fill=58)

    subtitle_base = pack_cfg.get("brand_subtitle", "cowMMC")
    version = str(pack_cfg.get("version", "2026.11.22"))
    ver_label = (
        version
        if version.startswith(("v", "nightly-")) or (len(version) >= 4 and version[:4].isdigit())
        else f"v{version}"
    )
    subtitle = f"{subtitle_base}  ·  {ver_label}"
    _draw_tracked_text_centered(draw, cx=cx, y=854, text=subtitle, font=f_sub, tracking=4, fill=142)

    gray = canvas.resize((1280, 720), Image.Resampling.LANCZOS)
    opaque = Image.new("L", (1280, 720), 255)
    return Image.merge("RGBA", (gray, gray, gray, opaque))


def _ensure_opaque_rgba(img: Image.Image) -> Image.Image:
    r, g, b, _ = img.convert("RGBA").split()
    opaque = Image.new("L", img.size, 255)
    return Image.merge("RGBA", (r, g, b, opaque))


def generate_branding(
    pack_cfg: dict,
    custom_dir: Path,
    sd_root: Path,
    preview_png: Path | None = None,
) -> list[str]:
    override = custom_dir / "bootlogo.png"
    if override.is_file():
        with Image.open(override) as im:
            rgba = im.convert("RGBA")
            if rgba.size == (720, 1280):
                landscape = rgba.rotate(-90, expand=True)
            else:
                if rgba.size != (1280, 720):
                    rgba = rgba.resize((1280, 720), Image.Resampling.LANCZOS)
                landscape = rgba
            landscape = _ensure_opaque_rgba(landscape)
    else:
        landscape = _render_bootlogo_minimal_bw_landscape(pack_cfg)

    if preview_png is not None:
        preview_png.parent.mkdir(parents=True, exist_ok=True)
        landscape.save(preview_png)

    # Rotate 90 deg CCW -> 720x1280 portrait as required by Hekate framebuffer
    portrait = landscape.rotate(90, expand=True)
    assert portrait.size == (720, 1280), f"bootlogo produced {portrait.size}, expected (720, 1280)"
    write_bmp32_bgra(portrait, sd_root / "bootloader/bootlogo.bmp")
    return ["bootloader/bootlogo.bmp"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate minimal B&W MooSwitch bootlogo BMP")
    parser.add_argument("--pack-json", default=str(Path(__file__).resolve().parent.parent / "pack.json"))
    parser.add_argument("--custom-dir", default=str(Path(__file__).resolve().parent / "custom"))
    parser.add_argument("--sd-root", required=True)
    parser.add_argument("--preview-png", default=None, help="Optional 1280x720 landscape PNG preview path")
    args = parser.parse_args()

    pack_cfg = json.loads(Path(args.pack_json).read_text(encoding="utf-8"))
    preview = Path(args.preview_png) if args.preview_png else None
    out = generate_branding(pack_cfg, Path(args.custom_dir), Path(args.sd_root), preview_png=preview)
    print(f"[+] Generated {len(out)} Switch BMP assets in {args.sd_root}:")
    for rel in out:
        print(f"    - {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
