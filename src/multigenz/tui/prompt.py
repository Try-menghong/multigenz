"""The home screen's prompt: a multi-line text box that grows with its content (up to 10 lines).

Enter sends; a backslash right before Enter, Shift+Enter (where the terminal reports it), Alt+Enter or Ctrl+J start a
new line instead; pasted text keeps its line breaks. The key handling lives in the home screen's bindings so it can
also drive the completion menu.
"""

from __future__ import annotations

from textual.widgets import TextArea

PLACEHOLDER = ("Type a goal, e.g. Add a /health endpoint with tests   ·   / commands   @ files   # note   "
               "? shortcuts")


class PromptArea(TextArea):
    DEFAULT_CSS = """
    PromptArea { height: auto; max-height: 12; }
    """

    def __init__(self, **kw):
        super().__init__("", soft_wrap=True, tab_behavior="focus", show_line_numbers=False,
                         highlight_cursor_line=False, placeholder=PLACEHOLDER, **kw)

    # -- offsets <-> (row, column) ------------------------------------------------------------------------------
    @property
    def cursor_offset(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    def replace_span(self, start: int, end: int, text: str) -> None:
        loc = self.document.get_location_from_index
        result = self.replace(text, loc(start), loc(end))
        self.cursor_location = result.end_location

    def set_text(self, text: str) -> None:
        self.load_text(text)
        self.cursor_location = self.document.get_location_from_index(len(text))

    @property
    def on_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    @property
    def on_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    def char_before_cursor(self) -> str:
        offset = self.cursor_offset
        return self.text[offset - 1] if offset else ""
