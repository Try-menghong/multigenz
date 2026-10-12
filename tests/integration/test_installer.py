"""install.sh (served as `curl -fsSL <site>/install | bash`).

Fast checks always run. A real install into a throwaway HOME (it downloads MultiGenz's dependencies from PyPI) runs
when MULTIGENZ_TEST_INSTALLER=1.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.helpers import REPO_ROOT

SCRIPT = REPO_ROOT / "install.sh"
SITE_COPY = REPO_ROOT / "site" / "install"


def sh(args, *, home: Path, stdin: str | None = None, **env):
    e = {"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin", "SHELL": "/bin/bash", "LANG": "C.UTF-8"}
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
              "PIP_CERT", "UV_NATIVE_TLS", "PIP_INDEX_URL", "UV_INDEX_URL"):
        if k in os.environ:
            e[k] = os.environ[k]
    e.update(env)
    return subprocess.run(["bash", *args], input=stdin, capture_output=True, text=True, env=e, timeout=900)


def test_script_is_valid_and_truncation_safe():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    lines = [ln for ln in SCRIPT.read_text().splitlines() if ln.strip()]
    assert lines[-1] == 'main "$@"'          # a partially downloaded script defines functions but runs nothing
    import re
    executed = [ln for ln in SCRIPT.read_text().splitlines()
                if re.search(r"^\s*sudo\b|[;&|(]\s*sudo\b", ln) and not ln.lstrip().startswith("#")]
    assert executed == []                      # sudo appears only inside hint messages, never as a command
    assert "|| true" not in SCRIPT.read_text()


def test_site_serves_the_same_installer():
    assert SITE_COPY.read_bytes() == SCRIPT.read_bytes()


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    r = subprocess.run(["shellcheck", "-s", "bash", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_help_and_uninstall_work_when_piped(tmp_path):
    r = sh(["-s", "--", "--help"], home=tmp_path, stdin=SCRIPT.read_text())
    assert r.returncode == 0 and "MULTIGENZ_REF" in r.stdout
    r = sh(["-s", "--", "--uninstall"], home=tmp_path, stdin=SCRIPT.read_text())
    assert r.returncode == 0 and "left in place" in r.stdout


def test_bad_source_fails_loudly_without_linking(tmp_path):
    r = sh([str(SCRIPT)], home=tmp_path, MULTIGENZ_SOURCE=str(tmp_path / "nope"))
    assert r.returncode != 0 and "error:" in r.stderr
    assert not (tmp_path / ".local" / "bin" / "multigenz").exists()


@pytest.mark.skipif(os.environ.get("MULTIGENZ_TEST_INSTALLER") != "1",
                    reason="downloads dependencies; set MULTIGENZ_TEST_INSTALLER=1")
@pytest.mark.skipif(not (REPO_ROOT / "src").is_dir(), reason="needs a source checkout to install from")
def test_full_install_from_checkout_then_init_and_doctor(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = sh([str(SCRIPT)], home=home, MULTIGENZ_SOURCE=str(REPO_ROOT))
    assert r.returncode == 0, r.stdout + r.stderr
    exe = home / ".local" / "bin" / "multigenz"
    assert exe.is_symlink() and "added by the MultiGenz installer" in (home / ".bashrc").read_text()
    env = {"HOME": str(home), "PATH": f"{exe.parent}:/usr/bin:/bin"}
    assert subprocess.run([str(exe), "version"], capture_output=True, text=True, env=env).stdout.strip()
    assert subprocess.run([str(exe), "init", "-a", "cline"], capture_output=True, text=True, env=env).returncode == 0
    doctor = subprocess.run([str(exe), "doctor"], capture_output=True, text=True, env=env)
    assert "policy: valid" in doctor.stdout and "required isolation" in doctor.stdout
    again = sh([str(SCRIPT)], home=home, MULTIGENZ_SOURCE=str(REPO_ROOT))     # upgrade in place, PATH line once
    assert again.returncode == 0 and (home / ".bashrc").read_text().count("MultiGenz installer") == 1
    gone = sh([str(SCRIPT), "--uninstall"], home=home)
    assert gone.returncode == 0 and not exe.exists() and (home / ".config" / "multigenz" / "policy.yaml").exists()
