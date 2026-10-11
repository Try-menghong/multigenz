"""M5: Textual TUI - history, dashboard, redacted logs, cancel, new run, doctor."""

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, RichLog, Static

from multigenz.core.driver import start_run
from multigenz.core.runs import open_store
from multigenz.tui.app import ConfirmScreen, DoctorScreen, LogScreen, MultiGenzApp, NewRunScreen, RunScreen, RunsScreen
from multigenz.tui import data
from tests.support.helpers import T, plan_of, scripted_policy, write_scenario, writes

SECRET = "hunter2-super-secret-value-123"


def pol(scenario):
    return scripted_policy(scenario, isolation="none")


async def finished_run(tmp_path, repo, scenario, home):
    write_scenario(scenario, "a", {"attempts": {"default": [{"write": {"a/f.py": "1\n"}, "print": ["hello from agent"]}]}})
    write_scenario(scenario, "b", {"attempts": {"default": [{"exit": 2}]}})
    rid, out = await start_run(pol(scenario), objective="demo goal", repo=repo, home=home,
                               plan=plan_of(T("a", "a/"), T("b", "b/", max_attempts=1)))
    return rid


async def text_of(widget) -> str:
    return str(widget.renderable) if hasattr(widget, "renderable") else str(widget)


async def test_history_dashboard_logs_and_redaction(tmp_path, repo, scenario, monkeypatch):
    home = tmp_path / "mg-home"
    rid = await finished_run(tmp_path, repo, scenario, home)
    st = open_store(home)
    a_attempt = st.list_attempts(rid, "a")[0]
    # a secret that somehow reached a log file on disk must still be hidden when displayed
    monkeypatch.setenv("MY_SERVICE_API_KEY", SECRET)
    with open(Path(a_attempt["log_dir"]) / "stdout.log", "a") as fh:
        fh.write(f"leaked: {SECRET}\n")
    app = MultiGenzApp(home, policy=pol(scenario))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)
        table = app.screen.query_one("#runs", DataTable)
        assert table.row_count == 1 and rid in str(table.get_row_at(0)) and "failed" in str(table.get_row_at(0))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)
        tasks = app.screen.query_one("#tasks", DataTable)
        rows = {str(tasks.get_row_at(i)[0]): [str(c) for c in tasks.get_row_at(i)] for i in range(tasks.row_count)}
        assert rows["a"][1] == "succeeded" and rows["b"][1] == "failed" and rows["a"][4] == "pass"
        header = app.screen.query_one("#run-header", Static)
        assert "FAILED" in str(header.render()) and "pushing/deploying: disabled" in str(header.render())
        ev = app.screen.query_one("#events", RichLog)
        evdump = "\n".join(strip.text for strip in ev.lines)
        assert "a: pending -> ready" in evdump and "b: running -> failed" in evdump and "RUN running -> failed" in evdump
        await pilot.press("l")  # logs for the highlighted task ("a")
        await pilot.pause()
        assert isinstance(app.screen, LogScreen)
        log = app.screen.query_one("#log", RichLog)
        dump = "\n".join(strip.text for strip in log.lines)
        assert "hello from agent" in dump and "leaked:" in dump
        assert SECRET not in dump and "[REDACTED]" in dump
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, RunsScreen)


async def test_cancel_requires_confirmation_and_sets_flag(tmp_path, repo, scenario):
    home = tmp_path / "mg-home"
    rid, _ = await start_run(pol(scenario), objective="g", repo=repo, home=home, plan=plan_of(T("a", "a/")), execute=False)
    st = open_store(home)
    app = MultiGenzApp(home, policy=pol(scenario))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")  # declining changes nothing
        await pilot.pause()
        assert st.get_run(rid)["cancel_requested"] == 0
        await pilot.press("c")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert st.get_run(rid)["cancel_requested"] == 1
        assert any(e["kind"] == "cancel.requested" and e["payload"]["by"] == "tui" for e in st.events(rid))
        assert "CANCEL REQUESTED" in str(app.screen.query_one("#run-header", Static).render())


async def test_audit_action_reports_ok(tmp_path, repo, scenario):
    home = tmp_path / "mg-home"
    await finished_run(tmp_path, repo, scenario, home)
    app = MultiGenzApp(home, policy=pol(scenario))
    notes = []
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.notify = lambda msg, **kw: notes.append((msg, kw))  # type: ignore[assignment]
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
    assert notes and notes[-1][0].startswith("Audit OK")


async def test_new_run_form_creates_run_and_starts_worker(tmp_path, repo, scenario, monkeypatch):
    home = tmp_path / "mg-home"
    write_scenario(scenario, "a", writes("a/f.py", "1\n"))
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan_of(T("a", "a/")).model_dump_json())
    spawned = []
    monkeypatch.setattr("multigenz.core.driver.spawn_detached", lambda h, rid, *a: spawned.append(rid) or 4242)
    app = MultiGenzApp(home, policy=pol(scenario))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, NewRunScreen)
        await pilot.click("#start")  # empty goal is refused
        await pilot.pause()
        assert "goal is required" in str(app.screen.query_one("#error", Static).render())
        app.screen.query_one("#goal", Input).value = "Build a thing"
        app.screen.query_one("#repo", Input).value = str(repo)
        app.screen.query_one("#plan", Input).value = str(plan_file)
        app.screen.query_one("#start", Button).press()
        for _ in range(100):  # creation runs off the UI thread
            await pilot.pause(0.1)
            if isinstance(app.screen, RunsScreen):
                break
        assert isinstance(app.screen, RunsScreen), str(app.screen.query_one("#error", Static).render())
    st = open_store(home)
    runs = st.list_runs()
    assert len(runs) == 1 and runs[0]["objective"] == "Build a thing" and spawned == [runs[0]["run_id"]]
    assert st.get_run(runs[0]["run_id"])["status"] == "running" and len(st.list_tasks(runs[0]["run_id"])) == 1


async def test_new_run_form_shows_policy_rejection(tmp_path, repo, scenario):
    home = tmp_path / "mg-home"
    app = MultiGenzApp(home, policy=pol(scenario))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#goal", Input).value = "deploy to production and git push origin main"
        app.screen.query_one("#repo", Input).value = str(repo)
        await pilot.click("#start")
        for _ in range(100):
            await pilot.pause(0.1)
            if "out of policy" in str(app.screen.query_one("#error", Static).render()):
                break
        assert isinstance(app.screen, NewRunScreen) and "out of policy" in str(app.screen.query_one("#error", Static).render())
    assert open_store(home).list_runs() == []


async def test_doctor_screen_lists_checks(tmp_path, scenario):
    app = MultiGenzApp(tmp_path / "mg-home", policy=pol(scenario))
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause(1.0)
        assert isinstance(app.screen, DoctorScreen)
        t = app.screen.query_one("#checks", DataTable)
        names = [str(t.get_row_at(i)[1]) for i in range(t.row_count)]
        assert "sqlite WAL" in names and any(n.startswith("agent: alpha") for n in names)


async def test_orphaned_runs_are_flagged_in_history(tmp_path, repo, scenario):
    home = tmp_path / "mg-home"
    rid, _ = await start_run(pol(scenario), objective="g", repo=repo, home=home, plan=plan_of(T("a", "a/")), execute=False)
    st = open_store(home)
    rows = data.runs(st, home)
    assert rows[0]["status"] == "running" and rows[0]["orphaned"] is True  # nobody holds the run lock
