"""OpenAI Codex CLI adapter.

No built-in invocation is shipped: non-interactive flags are never assumed. Define
``agents.codex.invocations`` in the policy with ``requires_help_tokens`` taken from the
installed binary's help; until one verifies, the adapter stays disabled.
"""

from __future__ import annotations

from multigenz.adapters.cli_adapter import CliAdapter


class CodexAdapter(CliAdapter):
    BUILTIN_INVOCATIONS: list = []
