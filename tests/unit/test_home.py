"""The pet and the home screen's helpers (team summary, command hints, tab completion, repository detection)."""

import subprocess

from rich.text import Text

from multigenz.config import parse_policy
from multigenz.tui.home import COMMANDS, complete, git_info, hints_for, team_lines, tilde
from multigenz.tui.mascot import HEIGHT, PIXELS, WIDTH, mascot, mascot_lines


def test_the_pet_has_three_heads_on_one_body_and_blinks():
    open_, blink = mascot_lines("open"), mascot_lines("blink")
    assert len(open_) == HEIGHT == 5 and all(len(line) == WIDTH for line in open_ + blink)
    assert sum(row.count("e") for row in PIXELS) == 6                 # two eyes per head
    assert open_ != blink and open_[0] == blink[0] and open_[2:] == blink[2:]   # only the eye row changes
    pet = mascot()
    assert isinstance(pet, Text) and len(pet.plain.splitlines()) == HEIGHT
    assert all(len(line) == WIDTH for line in pet.plain.splitlines())
    colours = {str(span.style.color.name) for span in pet.spans if span.style.color}
    assert {"#fb923c", "#38bdf8", "#4ade80", "#c084fc"} <= colours    # a colour per head, and the body


def pol():
    return parse_policy({
        "orchestrator": {"planning": "auto"},
        "agents": {"claude": {"adapter": "cli", "command": "claude", "model": "opus", "capabilities": ["coding"]},
                   "codex": {"adapter": "cli", "command": "codex", "capabilities": ["coding"]},
                   "cline": {"adapter": "cli", "command": "cline", "enabled": False, "capabilities": ["coding"]}},
        "roles": {"planner": ["claude"], "worker": ["codex", "claude"]}})


def test_team_summary_shows_roles_with_their_models():
    rows = dict(team_lines(pol()))
    assert rows["planning"].startswith("auto:")
    assert rows["planner"].startswith("claude (opus)") and "only with planning: always" in rows["planner"]
    assert rows["workers"] == "codex (default model) → claude (opus)"
    assert rows["conflicts"] == "same as the workers"
    assert rows["reviewer"].startswith("none")
    assert rows["agents"] == "claude (opus) · codex (default model)" and rows["disabled"] == "cline"


def test_command_hints_and_tab_completion():
    p = pol()
    assert [h for h, _ in hints_for("/", p)] == list(COMMANDS)
    assert [h for h, _ in hints_for("/pl", p)] == ["/planning"]
    assert [h for h, _ in hints_for("/model c", p)] == ["claude", "codex", "cline"]
    assert [h for h, _ in hints_for("/planning a", p)] == ["always", "auto"]
    assert hints_for("build me an api", p) == []
    assert complete("/mo", p) == "/model "
    assert complete("/model cla", p) == "/model claude "
    assert complete("/model c", p) == "/model c"                 # ambiguous: unchanged
    assert complete("/planning n", p) == "/planning never "
    assert complete("/p", p) == "/p"                              # /providers or /planning


def test_git_info_finds_the_repository_and_branch(tmp_path):
    assert git_info(tmp_path) == (None, None)
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True)
    (tmp_path / "sub").mkdir()
    top, branch = git_info(tmp_path / "sub")
    assert top == tmp_path.resolve() and branch == "trunk"


def test_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/me")
    assert tilde("/home/me/proj") == "~/proj" and tilde("/home/meow") == "/home/meow" and tilde("/srv") == "/srv"
