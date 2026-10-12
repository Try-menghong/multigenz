"""Split view building blocks: following a log file as it grows, and which agent runs get a pane."""

from multigenz.tui.panes import MAX_PANES, LogTail, choose, elapsed


def test_log_tail_returns_only_complete_new_lines(tmp_path):
    f = tmp_path / "stdout.log"
    tail = LogTail(f)
    assert tail.read() == []                      # not written yet
    f.write_bytes(b"one\ntw")
    assert tail.read() == ["one"]
    with open(f, "ab") as fh:
        fh.write(b"o\nthree\n")
    assert tail.read() == ["two", "three"]
    assert tail.read() == []


def test_a_pane_opened_late_starts_near_the_end(tmp_path):
    f = tmp_path / "stdout.log"
    f.write_text("".join(f"line {i}\n" for i in range(1000)))
    lines = LogTail(f, start_tail=100).read()
    assert lines[-1] == "line 999" and len(lines) < 20 and all(ln.startswith("line ") for ln in lines)


def w(i, active, started):
    return {"id": i, "active": active, "started_at": started}


def test_active_runs_first_then_the_latest_finished_and_panes_keep_their_place():
    ws = [w("plan", False, "1"), w("a", True, "2"), w("b", True, "3"), w("c", False, "4"), w("d", True, "5")]
    assert [x["id"] for x in choose(ws, [])] == ["a", "b", "c", "d"]
    assert [x["id"] for x in choose(ws, ["d", "b"])] == ["d", "b", "a", "c"]       # on-screen order kept
    many = [w(str(i), True, str(i)) for i in range(7)]
    assert len(choose(many, [])) == MAX_PANES
    assert [x["id"] for x in choose(ws, [], limit=2)] == ["a", "b"]


def test_elapsed():
    assert elapsed("2026-10-12T10:00:00+00:00", "2026-10-12T10:00:42+00:00") == "42s"
    assert elapsed("2026-10-12T10:00:00+00:00", "2026-10-12T10:03:05+00:00") == "3m05s"
    assert elapsed(None, None) == ""


def test_lines_keep_their_colours_but_lose_other_escape_codes_and_hidden_secrets():
    from multigenz.core.redaction import Redactor
    from multigenz.tui.panes import render_line

    red = Redactor(["sk-hidden-0123456789abcdef"])
    t = render_line("\x1b[32mok\x1b[0m done\x1b[2K", red)
    assert t.plain == "ok done" and any(s.style.color and s.style.color.number == 2 for s in t.spans)  # green
    assert render_line("50%\r100%\r", red).plain == "100%"
    # a secret split by colour codes slips past a line-based filter, but not past the pane
    split = "key=sk-hidden-\x1b[1m0123456789abcdef\x1b[0m"
    assert "sk-hidden-0123456789abcdef" not in render_line(split, red).plain
    assert render_line("note", red, "dim").spans[-1].style == "dim"
