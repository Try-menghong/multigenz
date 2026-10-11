"""Claude Code CLI adapter.

The invocations below are *candidates*. They are enabled only if the installed ``claude``
binary's own ``--help`` text contains every token in ``requires_help_tokens`` (checked at
``probe()`` time). Administrators can replace them via ``agents.claude.invocations``.
"""

from __future__ import annotations

from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg

CODE_MODES = ["code", "conflict"]
READ_MODES = ["plan", "review", "analysis"]


class ClaudeAdapter(CliAdapter):
    BUILTIN_INVOCATIONS = [
        InvocationCfg(
            name="print-accept-edits",
            argv=["{command}", "--print", "--permission-mode", "acceptEdits", "--no-session-persistence"],
            stdin="prompt",
            requires_help_tokens=["--print", "--permission-mode", '"acceptEdits"', "--no-session-persistence"],
            modes=CODE_MODES,
        ),
        InvocationCfg(
            name="print-plan",
            argv=["{command}", "--print", "--permission-mode", "plan", "--no-session-persistence"],
            stdin="prompt",
            requires_help_tokens=["--print", "--permission-mode", '"plan"', "--no-session-persistence"],
            modes=READ_MODES,
        ),
    ]
