"""Run report: verified task states, test report, artifact list, final SHA, explicit unknown costs."""

from __future__ import annotations

import json
from pathlib import Path

from multigenz.core.runs import RunPaths
from multigenz.storage.sqlite import Store


def build_report(store: Store, run_id: str) -> dict:
    run = store.get_run(run_id)
    assert run is not None
    tasks = store.list_tasks(run_id)
    attempts = store.list_attempts(run_id)
    validations = store.events(run_id, kinds=("validation.result",))
    last_val: dict[str, dict] = {}
    for e in validations:
        last_val[e["task_id"]] = {"attempt_id": e["attempt_id"], **e["payload"]}
    measured = [a["cost_usd"] for a in attempts if a["cost_usd"] is not None]
    unknown = [a for a in attempts if a["cost_usd"] is None]
    providers = sorted({a["provider"] for a in unknown})
    report = {
        "run_id": run_id,
        "objective": run["objective"],
        "status": run["status"],
        "repo": run["repo_path"],
        "base_commit": run["base_commit"],
        "base_branch": run["base_branch"],
        "integration_branch": run["integration_branch"],
        "final_integration_commit": run["final_commit"],
        "integration_head": run["integration_head"],
        "pushed": False,
        "push_policy": "automatic push is disabled; nothing was pushed or deployed",
        "tasks": [{
            "task_id": t["task_id"], "status": t["status"], "provider": t["provider"],
            "attempts": t["total_attempts"], "charged_attempts": t["charged_attempts"],
            "branch": t["branch"], "head_commit": t["head_commit"], "integrated_commit": t["integrated_commit"],
            "failure_reason": t["failure_reason"],
            "last_validation": last_val.get(t["task_id"]),
        } for t in tasks],
        "attempts": [{
            "attempt_id": a["attempt_id"], "task_id": a["task_id"], "number": a["number"], "kind": a["kind"],
            "provider": a["provider"], "status": a["status"], "exit_code": a["exit_code"],
            "error_code": a["error_code"], "commit": a["commit_sha"], "branch": a["branch"],
            "cost_usd": a["cost_usd"] if a["cost_usd"] is not None else "unknown", "log_dir": a["log_dir"],
        } for a in attempts],
        "test_reports": [
            {"task_id": e["task_id"], "attempt_id": e["attempt_id"], "passed": e["payload"].get("passed"),
             "reports": e["payload"].get("test_reports", [])}
            for e in validations],
        "integrations": [{k: i[k] for k in ("task_id", "status", "candidate_commit", "result_commit", "detail")}
                         for i in store.list_integrations(run_id)],
        "artifacts": [{k: a[k] for k in ("kind", "path", "sha256", "task_id")} for a in store.list_artifacts(run_id)],
        "cost": {
            "measured_usd": round(sum(measured), 6) if measured else 0.0,
            "budget_usd": run["budget_usd"],
            "unknown_for_attempts": len(unknown),
            "unknown_providers": providers,
            "note": ("Spend for CLI/subscription agents cannot be measured and is reported as unknown."
                     if unknown else "All attempt costs measured."),
        },
        "event_count": run["event_seq"],
    }
    return report


def render_markdown(r: dict) -> str:
    lines = [f"# MultiGenz run {r['run_id']}", "", f"**Status:** {r['status']}", f"**Objective:** {r['objective']}", "",
             f"- Base commit: `{r['base_commit']}` ({r['base_branch']})",
             f"- Integration branch: `{r['integration_branch']}`",
             f"- Final integration commit: `{r['final_integration_commit'] or 'none'}`",
             f"- Pushed / deployed: **no** ({r['push_policy']})", "", "## Tasks", "",
             "| task | status | provider | attempts | commit | note |", "|---|---|---|---|---|---|"]
    for t in r["tasks"]:
        lines.append(f"| {t['task_id']} | {t['status']} | {t['provider'] or '-'} | {t['attempts']} | "
                     f"{(t['integrated_commit'] or t['head_commit'] or '-')[:12]} | {t['failure_reason'] or ''} |")
    lines += ["", "## Tests / validation", ""]
    for tr in r["test_reports"]:
        lines.append(f"- {tr['task_id']} (attempt {tr['attempt_id']}): {'PASS' if tr['passed'] else 'FAIL'}")
    lines += ["", "## Cost", "", f"- Measured: ${r['cost']['measured_usd']}  (budget ${r['cost']['budget_usd']})",
              f"- {r['cost']['note']}", "", "## Artifacts", ""]
    for a in r["artifacts"] or []:
        lines.append(f"- {a['kind']}: {a['path']}")
    return "\n".join(lines) + "\n"


def write_report(store: Store, run_id: str, paths: RunPaths) -> dict:
    r = build_report(store, run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.report_json.write_text(json.dumps(r, indent=2, default=str), encoding="utf-8")
    paths.report_md.write_text(render_markdown(r), encoding="utf-8")
    store.update_run(run_id, report=json.dumps(r, default=str))
    return r
