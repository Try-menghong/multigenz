"""The prompt's completion menu: commands, their arguments, and @ file mentions."""

import subprocess

from multigenz.tui.complete import common_prefix, completions, match_files, repo_files
from multigenz.tui.home import COMMANDS

AGENTS = {"claude": "opus", "codex": "default model", "cline": "default model"}
RUNS = [{"run_id": "abc123", "status": "running", "objective": "Add login\nwith tests"},
        {"run_id": "abd999", "status": "failed", "objective": "Fix bug"}]


def labels(text, cursor=None, **kw):
    start, end, items = completions(text, len(text) if cursor is None else cursor, commands=COMMANDS,
                                    agents=AGENTS, runs=RUNS, themes=["nord", "textual-dark"], **kw)
    return [i.label for i in items]


def test_commands_then_their_arguments():
    assert labels("/") == list(COMMANDS)
    assert labels("/pl") == ["/plan", "/planning"]
    assert labels("/model c") == ["claude", "codex", "cline"]
    assert labels("/planning a") == ["always", "auto"]
    assert labels("/resume ab") == ["abc123", "abd999"]
    assert labels("/theme n") == ["nord"]
    assert labels("/memory ") == ["add", "remove", "clear"]
    assert labels("build the thing") == [] and labels("/model claude op") == []


def test_enter_runs_complete_commands_but_not_ones_that_need_more():
    _, _, items = completions("/pl", 3, commands=COMMANDS)
    final = {i.label: i.final for i in items}
    assert final == {"/plan": False, "/planning": True}
    _, _, items = completions("/memory a", 9, commands=COMMANDS)
    assert [(i.insert, i.final) for i in items] == [("add ", False)]


def test_file_mentions_anywhere_in_the_text():
    files = ["README.md", "src/app/views.py", "src/app/review.py", "tests/test_views.py", "docs/overview.md"]
    start, end, items = completions("fix the bug in @vie please", 19, commands=COMMANDS, files=files)
    assert (start, end) == (15, 19)
    # names starting with the text first, then names containing it (shorter paths first)
    assert [i.label for i in items] == ["@src/app/views.py", "@docs/overview.md", "@src/app/review.py",
                                        "@tests/test_views.py"]
    assert items[0].insert == "@src/app/views.py " and not items[0].final
    assert match_files("", files)[0] == "README.md"                     # top-level files first
    # an @ on a later line works too; commands only on the first line
    assert completions("line one\nsee @REA", 17, commands=COMMANDS, files=files)[2][0].label == "@README.md"
    assert completions("note\n/mo", 8, commands=COMMANDS)[2] == []


def test_common_prefix_for_tab():
    _, _, items = completions("/pl", 3, commands=COMMANDS)
    assert common_prefix(items) == "/plan"
    assert common_prefix([]) == ""


def test_repo_files_are_the_files_at_head(tmp_path):
    assert repo_files(None) == [] and repo_files(tmp_path) == []
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_text("x\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("y\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
                   check=True)
    (tmp_path / "uncommitted.py").write_text("z\n")
    assert repo_files(tmp_path) == ["a.py"]     # what a run's worktree will contain, nothing else
