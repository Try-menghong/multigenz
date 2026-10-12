"""The MultiGenz pet: three little heads on one body - several agents working as one team.

Pixel art drawn with half-block characters (two pixels per terminal cell), so it renders in any modern terminal. Each
head has its own colour; now and then the eyes close for a moment (``frame="blink"``).
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

# a, b, c: the three heads; d: body; e: eyes; ".": empty. Two pixel rows make one text row.
PIXELS = (
    ".aaa...bbb...ccc...",
    "aaaaa.bbbbb.ccccc..",
    "aeaea.bebeb.cecec..",
    "aaaaa.bbbbb.ccccc..",
    ".aaa...bbb...ccc...",
    "..ddddddddddddd....",
    ".ddddddddddddddd...",
    ".ddddddddddddddd...",
    "..ddddddddddddd....",
    "...dd.......dd.....",
)
COLORS = {"a": "#fb923c", "b": "#38bdf8", "c": "#4ade80", "d": "#c084fc", "e": "#1e1b2e"}
WIDTH = len(PIXELS[0]) - 2   # the last two columns are padding in the grid
HEIGHT = len(PIXELS) // 2


def _grid(frame: str) -> list[str]:
    if frame == "blink":  # eyes closed: each eye pixel takes the colour of its head (the pixel to its left)
        return ["".join(row[i - 1] if ch == "e" else ch for i, ch in enumerate(row)) for row in PIXELS]
    if frame != "open":
        raise ValueError(f"unknown frame {frame!r}")
    return list(PIXELS)


def _cells(frame: str):
    grid = [row[:WIDTH] for row in _grid(frame)]
    for r in range(0, len(grid), 2):
        yield [(grid[r][c], grid[r + 1][c]) for c in range(WIDTH)]


def mascot_lines(frame: str = "open") -> list[str]:
    """The pet as plain text without colour (eyes are left as holes), e.g. for a README."""
    out = []
    for row in _cells(frame):
        line = ""
        for top, bot in row:
            t, b = top not in ".e", bot not in ".e"
            line += "█" if t and b else "▀" if t else "▄" if b else " "
        out.append(line)
    return out


def mascot(frame: str = "open") -> Text:
    """The pet as colourful Rich text."""
    out = Text(no_wrap=True, overflow="crop", end="")
    for n, row in enumerate(_cells(frame)):
        if n:
            out.append("\n")
        for top, bot in row:
            if top == "." and bot == ".":
                out.append(" ")
            elif top == bot:
                out.append("█", Style(color=COLORS[top]))
            elif top == ".":
                out.append("▄", Style(color=COLORS[bot]))
            elif bot == ".":
                out.append("▀", Style(color=COLORS[top]))
            else:
                out.append("▀", Style(color=COLORS[top], bgcolor=COLORS[bot]))
    return out


def mascot_svg(frame: str = "open", *, scale: int = 4, title: str | None = "MultiGenz") -> str:
    """The pet as a small SVG (same pixels, square), e.g. the download page's logo and favicon.

    ``title=None`` makes it decorative (hidden from screen readers), for use next to the name.
    """
    rects = []
    for y, row in enumerate(_grid(frame)):
        x = 0
        while x < WIDTH:
            ch = row[x]
            run = 1
            while x + run < WIDTH and row[x + run] == ch:
                run += 1
            if ch != ".":
                rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{COLORS[ch]}"/>')
            x += run
    h = len(PIXELS)
    label = f'role="img" aria-label="{title}"><title>{title}</title>' if title else 'aria-hidden="true">'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {h}" width="{WIDTH * scale}" '
            f'height="{h * scale}" shape-rendering="crispEdges" {label}{"".join(rects)}</svg>')
