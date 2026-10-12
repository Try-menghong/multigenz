"""Hermes Agent adapter (Nous Research, ``pip install hermes-agent``; binary ``hermes``).

Candidate invocation, enabled only if ``hermes --help`` lists every token: one-shot mode, ``hermes -z PROMPT``, which
prints only the final answer and auto-bypasses approvals (the MultiGenz sandbox is the boundary). Hermes takes the prompt
only as an argument, hence ``{prompt}``. Verified end to end with hermes-agent 0.19.0 inside the userns sandbox against
a local scripted model. Hermes keeps its state in ``~/.hermes`` (``HERMES_HOME``): inside the sandbox that is a
throw-away home that only receives the files the policy grants (its config, .env, skills, plugins).
"""

from __future__ import annotations

from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg


class HermesAdapter(CliAdapter):
    MODEL_FLAG = "--model"  # `agents.<name>.model`, verified in the help text before use
    BUILTIN_INVOCATIONS = [
        InvocationCfg(name="oneshot", argv=["{command}", "-z", "{prompt}"], stdin="none",
                      requires_help_tokens=["-z PROMPT", "--oneshot"]),
    ]
