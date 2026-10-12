"""Every shipped invocation must verify against the real help text of the CLI version it was written for.

The fixtures under fixtures/help/ were captured from the installed binaries (agy: from a user's machine); if a CLI
renames a flag, the probe disables the invocation instead of running a broken command, and this test shows which.
"""

from pathlib import Path

import pytest

from multigenz.adapters.cline import ClineAdapter
from multigenz.adapters.codex import OUTER_SANDBOX_INVOCATIONS
from multigenz.adapters.antigravity import EXPERIMENTAL_INVOCATIONS
from multigenz.adapters.opencode import OpencodeAdapter
from multigenz.config import InvocationCfg
from multigenz.presets import PRESETS, preset_invocations

HELP = Path(__file__).parent / "fixtures" / "help"
CASES = [
    ("cline-3.0.70.txt", ClineAdapter.BUILTIN_INVOCATIONS),
    ("opencode-1.19.0-run.txt", OpencodeAdapter.BUILTIN_INVOCATIONS),
    ("codex-0.162.1-exec.txt", OUTER_SANDBOX_INVOCATIONS),
    ("agy-1.3.3.txt", EXPERIMENTAL_INVOCATIONS),
]


@pytest.mark.parametrize("fixture,invocations", CASES, ids=[c[0] for c in CASES])
def test_invocations_verify_against_recorded_help(fixture, invocations):
    text = (HELP / fixture).read_text()
    assert invocations
    for inv in invocations:
        missing = [t for t in inv.requires_help_tokens if t not in text]
        assert not missing, f"{inv.name}: {missing}"
        assert inv.stdin == "prompt" and inv.argv[0] == "{command}"


def test_every_preset_invocation_is_a_valid_invocation_config():
    for p in PRESETS.values():
        for inv in preset_invocations(p) or []:
            InvocationCfg.model_validate(inv)


def test_codex_read_and_write_modes_are_split():
    modes = {m for inv in OUTER_SANDBOX_INVOCATIONS for m in inv.modes}
    assert modes == {"code", "conflict", "plan", "review", "analysis"}
    assert all("danger-full-access" in inv.argv for inv in OUTER_SANDBOX_INVOCATIONS)
