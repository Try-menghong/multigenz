"""OpenAI Codex CLI adapter.

No built-in invocation is enabled by default. ``codex exec`` itself works unattended (prompt on stdin), but Codex's
own sandbox needs bubblewrap to create a nested user namespace, which fails inside MultiGenz's userns sandbox
(``bwrap: setting up uid map: Operation not permitted``; the legacy Landlock mode also requires bubblewrap in 0.162).
Inside MultiGenz, Codex can therefore only run commands with its inner sandbox turned off
(``--sandbox danger-full-access``), relying on the MultiGenz sandbox alone. That is an administrator decision, so the
invocations below are only written into a policy by ``multigenz agents add codex --trust-outer-sandbox``.
"""

from __future__ import annotations

from multigenz.adapters.claude import CODE_MODES, READ_MODES
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg

_EXEC = ["{command}", "exec", "--sandbox", "danger-full-access", "--skip-git-repo-check", "--ephemeral",
         "--color", "never"]
_TOKENS = ["--sandbox", "danger-full-access", "--skip-git-repo-check", "--ephemeral", "--color"]

OUTER_SANDBOX_INVOCATIONS = [
    InvocationCfg(name="exec-outer-sandbox", argv=_EXEC, stdin="prompt", help_args=["exec", "--help"],
                  requires_help_tokens=_TOKENS, modes=CODE_MODES),
    InvocationCfg(name="exec-outer-sandbox-read", argv=_EXEC, stdin="prompt", help_args=["exec", "--help"],
                  requires_help_tokens=_TOKENS, modes=READ_MODES),
]


class CodexAdapter(CliAdapter):
    BUILTIN_INVOCATIONS: list = []
    NO_INVOCATION_HINT = ("no invocation enabled: Codex's own sandbox cannot run nested inside MultiGenz's; run "
                          "`multigenz agents add codex --trust-outer-sandbox` to rely on the MultiGenz sandbox alone")
