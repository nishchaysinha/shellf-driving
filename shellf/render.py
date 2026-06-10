"""Render an emulated terminal screen to a PNG image.

LLMs take image input, so besides a text snapshot we can hand the model an actual
picture of the terminal -- colors, bold, reverse video, the block cursor, and an
overlaid marker showing where the mouse last acted. For an agent driving a colorful
TUI (lazygit, k9s, btop) the picture often conveys layout far better than text.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

# Standard xterm 16-color palette (name -> RGB), matching pyte's color names.
_PALETTE = {
    "black": (0, 0, 0),
    "red": (205, 0, 0),
    "green": (0, 205, 0),
    "brown": (205, 205, 0),     # pyte calls yellow "brown"
    "yellow": (205, 205, 0),
    "blue": (0, 0, 238),
    "magenta": (205, 0, 205),
    "cyan": (0, 205, 205),
    "white": (229, 229, 229),
    "brightblack": (127, 127, 127),
    "brightred": (255, 0, 0),
    "brightgreen": (0, 255, 0),
    "brightbrown": (255, 255, 0),
    "brightyellow": (255, 255, 0),
    "brightblue": (92, 92, 255),
    "brightmagenta": (255, 0, 255),
    "brightcyan": (0, 255, 255),
    "brightwhite": (255, 255, 255),
}

_DEFAULT_FG = (216, 216, 216)
_DEFAULT_BG = (12, 12, 12)
_CURSOR = (220, 220, 60)
_MOUSE = (255, 80, 80)

_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def _color(value, default):
    """Resolve a pyte color (name, 6-hex string, or 'default') to an RGB tuple."""
    if value == "default" or value is None:
        return default
    if value in _PALETTE:
        return _PALETTE[value]
    if len(value) == 6:
        try:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
        except ValueError:
            return default
    return default


class Renderer:
    """Caches fonts/metrics so repeated screenshots are cheap."""

    def __init__(self, font_size: int = 16):
        self.font = ImageFont.truetype(_FONT_REGULAR, font_size)
        try:
            self.bold = ImageFont.truetype(_FONT_BOLD, font_size)
        except OSError:
            self.bold = self.font
        # Monospace cell metrics from a representative glyph.
        bbox = self.font.getbbox("M")
        self.cell_w = max(1, self.font.getlength("M").__ceil__())
        self.cell_h = font_size + 6
        self.ascent = bbox[1]

    def render(self, screen, cursor: dict, mouse: tuple | None = None) -> bytes:
        """Render the screen to PNG bytes.

        cursor: {"x","y","hidden"} (0-based cell coords).
        mouse:  (x, y) 1-based cell coords of the last mouse action, or None.
        """
        cols, rows = screen.columns, screen.lines
        w, h = cols * self.cell_w, rows * self.cell_h
        img = Image.new("RGB", (w, h), _DEFAULT_BG)
        draw = ImageDraw.Draw(img)

        for y in range(rows):
            line = screen.buffer[y]
            for x in range(cols):
                ch = line[x]
                fg = _color(ch.fg, _DEFAULT_FG)
                bg = _color(ch.bg, _DEFAULT_BG)
                if ch.reverse:
                    fg, bg = bg, fg
                px, py = x * self.cell_w, y * self.cell_h
                if bg != _DEFAULT_BG:
                    draw.rectangle([px, py, px + self.cell_w, py + self.cell_h], fill=bg)
                data = ch.data
                if data and data != " ":
                    font = self.bold if ch.bold else self.font
                    draw.text((px, py + 2), data, font=font, fill=fg)

        # Block cursor (where keystrokes go) — draw as a translucent outline.
        if cursor and not cursor.get("hidden"):
            cx, cy = cursor["x"] * self.cell_w, cursor["y"] * self.cell_h
            draw.rectangle(
                [cx, cy, cx + self.cell_w - 1, cy + self.cell_h - 1],
                outline=_CURSOR,
                width=2,
            )

        # Mouse marker (where the last click/scroll landed).
        if mouse:
            mx = (mouse[0] - 1) * self.cell_w + self.cell_w // 2
            my = (mouse[1] - 1) * self.cell_h + self.cell_h // 2
            r = max(3, self.cell_w // 2)
            draw.ellipse([mx - r, my - r, mx + r, my + r], outline=_MOUSE, width=2)
            draw.line([mx - r, my, mx + r, my], fill=_MOUSE, width=1)
            draw.line([mx, my - r, mx, my + r], fill=_MOUSE, width=1)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
