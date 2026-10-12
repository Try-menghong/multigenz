"""Google Antigravity CLI adapter (binary ``agy``).

No built-in invocation is enabled by default: ``agy`` could not be installed in the environment MultiGenz was
developed in, so nothing about it has been run-tested. ``multigenz agents add agy`` writes EXPERIMENTAL candidate
invocations built from the flags ``agy --help`` (1.3.3) lists (``--print``, ``--mode accept-edits|plan``); they are
still only enabled if the installed binary's help contains them, and should be checked with ``multigenz agents run``.
"""

from __future__ import annotations

from multigenz.adapters.claude import CODE_MODES, READ_MODES
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg

EXPERIMENTAL_INVOCATIONS = [
    InvocationCfg(name="print-accept-edits", argv=["{command}", "--print", "--mode", "accept-edits"], stdin="prompt",
                  requires_help_tokens=["--print", "--mode", "accept-edits"], modes=CODE_MODES),
    InvocationCfg(name="print-plan", argv=["{command}", "--print", "--mode", "plan"], stdin="prompt",
                  requires_help_tokens=["--print", "--mode", "plan"], modes=READ_MODES),
]


class AntigravityAdapter(CliAdapter):
    BUILTIN_INVOCATIONS: list = []
    NO_INVOCATION_HINT = ("no invocation enabled: run `multigenz agents add agy` to add EXPERIMENTAL candidate "
                          "invocations (not run-tested), then check them with `multigenz agents run`")
