"""opencode CLI adapter (https://opencode.ai).

Candidate invocations, enabled only if ``opencode run --help`` lists every token in ``requires_help_tokens``. Both
read the prompt from stdin (``opencode run`` with no message argument), which was verified end to end against
opencode 1.19 inside the userns sandbox. ``--auto`` approves the agent's own tool calls: the MultiGenz sandbox is the
security boundary and unattended invocations refuse to run without it. opencode's ``plan`` agent still lets its shell
tool write files, which is acceptable only because read-only roles run on a disposable export of the tree.
"""

from __future__ import annotations

from multigenz.adapters.claude import CODE_MODES, READ_MODES
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg


class OpencodeAdapter(CliAdapter):
    BUILTIN_INVOCATIONS = [
        InvocationCfg(name="run-auto", argv=["{command}", "run", "--auto"], stdin="prompt",
                      help_args=["run", "--help"], requires_help_tokens=["--auto", "message"], modes=CODE_MODES),
        InvocationCfg(name="run-plan-agent", argv=["{command}", "run", "--agent", "plan"], stdin="prompt",
                      help_args=["run", "--help"], requires_help_tokens=["--agent", "message"], modes=READ_MODES),
    ]
