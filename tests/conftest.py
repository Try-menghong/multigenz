from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support.helpers import make_repo  # noqa: E402


def _userns_ok() -> bool:
    from multigenz.execution.sandbox import UsernsBackend

    return asyncio.run(UsernsBackend().check())[0]


USERNS = _userns_ok()


def pytest_collection_modifyitems(config, items):
    if USERNS:
        return
    skip = pytest.mark.skip(reason="unprivileged user namespaces unavailable on this host")
    for item in items:
        if "needs_userns" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "mg-home"
    monkeypatch.setenv("MULTIGENZ_HOME", str(home))
    monkeypatch.delenv("MULTIGENZ_POLICY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return home


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def scenario(tmp_path) -> Path:
    d = tmp_path / "scenario"
    d.mkdir()
    return d
