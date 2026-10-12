"""The download page (site/) and the installer it serves at /install."""

import functools
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.support.helpers import REPO_ROOT

SITE = REPO_ROOT / "site"
HTML = (SITE / "index.html").read_text()


def test_page_is_self_contained_except_google_fonts():
    hosts = set(re.findall(r'(?:src|href)="https?://([^/"]+)', HTML))
    assert hosts <= {"fonts.googleapis.com", "fonts.gstatic.com", "github.com"}, hosts
    assert "<title>MultiGenz</title>" in HTML and (SITE / ".nojekyll").exists()


def test_theme_tokens_defined_for_light_and_both_dark_paths():
    assert ':root:not([data-theme="light"])' in HTML and ':root[data-theme="dark"]' in HTML
    assert "body {" in HTML and "background: var(--paper)" in HTML


def test_agent_table_matches_the_presets():
    from multigenz.presets import PRESETS
    for p in PRESETS.values():
        assert f"multigenz agents add {p.key}" in HTML, p.key
    assert "danger-full-access" not in HTML


@pytest.fixture
def served():
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(SITE))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_installer_is_served_at_install_and_runs_when_piped(served, tmp_path):
    curl = shutil.which("curl")
    if not curl:
        pytest.skip("curl not installed")
    r = subprocess.run(f"{curl} -fsSL {served}install | bash -s -- --help", shell=True, capture_output=True,
                       text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=60)
    assert r.returncode == 0 and "MULTIGENZ_SOURCE" in r.stdout


def _playwright_node_path() -> str | None:
    for base in [os.environ.get("NODE_PATH", ""), "/opt/node-tools/node_modules"]:
        for d in filter(None, base.split(":")):
            if (Path(d) / "playwright").is_dir():
                return d
    return None


@pytest.mark.skipif(not (shutil.which("node") and _playwright_node_path()), reason="node + playwright not available")
def test_page_in_a_real_browser(served):
    r = subprocess.run(["node", str(REPO_ROOT / "tests" / "support" / "site_check.js"), served], capture_output=True,
                       text=True, timeout=180, env={**os.environ, "NODE_PATH": _playwright_node_path()})
    assert r.returncode == 0, r.stderr
    views = json.loads(r.stdout)
    want = f"curl -fsSL {served}install | bash"
    for name, v in views.items():
        assert v["overflow"] == 0, name                      # no sideways scrolling, phone included
        assert v["errors"] == [], (name, v["errors"])
        assert v["cmd"] == want and v["clipboard"] == want   # the copied command points at this site's installer
        assert v["uninstall"].endswith("bash -s -- --uninstall")
    assert views["desktop-light"]["bg"] != views["desktop-dark"]["bg"]
