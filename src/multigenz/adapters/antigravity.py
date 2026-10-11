"""Google Antigravity CLI adapter (binary name ``agy`` in the example policy).

No built-in invocation is shipped; see :mod:`multigenz.adapters.codex` for rationale.
"""

from __future__ import annotations

from multigenz.adapters.cli_adapter import CliAdapter


class AntigravityAdapter(CliAdapter):
    BUILTIN_INVOCATIONS: list = []
