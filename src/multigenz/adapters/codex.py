"""OpenAI Codex CLI adapter.

Candidate invocations, enabled only if ``codex exec --help`` lists every token. The prompt is read from stdin. Codex
keeps its OWN sandbox inside MultiGenz's: ``workspace-write`` for code/conflict roles, ``read-only`` for planning and
review. That works because MultiGenz runs every agent as an ordinary uid in a child user namespace (see
``nsboot.enter_agent_userns``), so Codex's bubblewrap can map that uid into its own namespace; as a capability-less uid 0
it could not (Linux >= 5.12 requires CAP_SETFCAP to map uid 0). Verified end to end with codex-cli 0.162.1.
"""

from __future__ import annotations

from multigenz.adapters.claude import CODE_MODES, READ_MODES
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg


def _exec(sandbox: str) -> list[str]:
    return ["{command}", "exec", "--sandbox", sandbox, "--skip-git-repo-check", "--ephemeral", "--color", "never"]


def _tokens(sandbox: str) -> list[str]:
    return ["--sandbox", sandbox, "--skip-git-repo-check", "--ephemeral", "--color"]


class CodexAdapter(CliAdapter):
    MODEL_FLAG = "--model"  # `agents.<name>.model`, verified in the help text before use
    BUILTIN_INVOCATIONS = [
        InvocationCfg(name="exec-workspace-write", argv=_exec("workspace-write"), stdin="prompt",
                      help_args=["exec", "--help"], requires_help_tokens=_tokens("workspace-write"), modes=CODE_MODES),
        InvocationCfg(name="exec-read-only", argv=_exec("read-only"), stdin="prompt",
                      help_args=["exec", "--help"], requires_help_tokens=_tokens("read-only"), modes=READ_MODES),
    ]
