from __future__ import annotations

import re

from .models import AnsiStyle


ANSI_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def ansi_color_from_256(code: int) -> tuple[int | None, bool]:
    if code < 0:
        return None, False
    if code < 8:
        return code, False
    if code < 16:
        return code - 8, True
    if code < 232:
        base_levels = [0, 95, 135, 175, 215, 255]
        cube = code - 16
        r = base_levels[(cube // 36) % 6]
        g = base_levels[(cube // 6) % 6]
        b = base_levels[cube % 6]
    else:
        gray = 8 + (code - 232) * 10
        r = g = b = gray

    return ansi_color_from_rgb(r, g, b)


def ansi_color_from_rgb(r: int, g: int, b: int) -> tuple[int | None, bool]:
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))

    palette = {
        0: (0, 0, 0),
        1: (205, 49, 49),
        2: (13, 188, 121),
        3: (229, 229, 16),
        4: (36, 114, 200),
        5: (188, 63, 188),
        6: (17, 168, 205),
        7: (229, 229, 229),
    }
    nearest = min(
        palette,
        key=lambda idx: sum(
            (component - ref) ** 2 for component, ref in zip((r, g, b), palette[idx])
        ),
    )
    return nearest, max(r, g, b) >= 200


def apply_ansi_sgr(style: AnsiStyle, sgr: str) -> AnsiStyle:
    codes = [int(part) for part in sgr.split(";") if part] if sgr else [0]
    current = AnsiStyle(
        fg=style.fg,
        bg=style.bg,
        bold=style.bold,
        dim=style.dim,
        reverse=style.reverse,
    )
    index = 0
    while index < len(codes):
        code = codes[index]
        if code == 0:
            current = AnsiStyle()
        elif code == 1:
            current.bold = True
        elif code == 2:
            current.dim = True
        elif code == 22:
            current.bold = False
            current.dim = False
        elif code == 7:
            current.reverse = True
        elif code == 27:
            current.reverse = False
        elif 30 <= code <= 37:
            current.fg = code - 30
        elif code == 39:
            current.fg = None
        elif 40 <= code <= 47:
            current.bg = code - 40
        elif code == 49:
            current.bg = None
        elif 90 <= code <= 97:
            current.fg = code - 90
            current.bold = True
        elif 100 <= code <= 107:
            current.bg = code - 100
        elif code in {38, 48}:
            if index + 4 < len(codes) and codes[index + 1] == 2:
                mapped, is_bright = ansi_color_from_rgb(
                    codes[index + 2], codes[index + 3], codes[index + 4]
                )
                if code == 38:
                    current.fg = mapped
                    current.bold = current.bold or is_bright
                else:
                    current.bg = mapped
                index += 4
            elif index + 2 < len(codes) and codes[index + 1] == 5:
                mapped, is_bright = ansi_color_from_256(codes[index + 2])
                if code == 38:
                    current.fg = mapped
                    current.bold = current.bold or is_bright
                else:
                    current.bg = mapped
                index += 2
        index += 1
    return current


def parse_ansi_segments(text: str) -> list[tuple[str, AnsiStyle]]:
    if not text:
        return []

    segments: list[tuple[str, AnsiStyle]] = []
    style = AnsiStyle()
    start = 0
    for match in ANSI_SGR_RE.finditer(text):
        if match.start() > start:
            segments.append(
                (
                    text[start : match.start()],
                    AnsiStyle(
                        fg=style.fg,
                        bg=style.bg,
                        bold=style.bold,
                        dim=style.dim,
                        reverse=style.reverse,
                    ),
                )
            )
        style = apply_ansi_sgr(style, match.group(1))
        start = match.end()
    if start < len(text):
        segments.append(
            (
                text[start:],
                AnsiStyle(
                    fg=style.fg,
                    bg=style.bg,
                    bold=style.bold,
                    dim=style.dim,
                    reverse=style.reverse,
                ),
            )
        )
    return segments
