"""Cline CLI adapter (``npm i -g cline``).

Candidate invocations, enabled only if ``cline --help`` lists every token. The prompt is read from stdin; verified end
to end against cline 3.0 inside the userns sandbox. ``--yolo`` auto-approves Cline's tools for unattended runs (the
MultiGenz sandbox is the boundary); ``--plan`` refuses file modifications and is used for read-only roles.
"""

from __future__ import annotations

from multigenz.adapters.claude import CODE_MODES, READ_MODES
from multigenz.adapters.cli_adapter import CliAdapter
from multigenz.config import InvocationCfg


class ClineAdapter(CliAdapter):
    BUILTIN_INVOCATIONS = [
        InvocationCfg(name="yolo", argv=["{command}", "--yolo"], stdin="prompt",
                      requires_help_tokens=["--yolo", "[prompt]"], modes=CODE_MODES),
        InvocationCfg(name="plan", argv=["{command}", "--plan"], stdin="prompt",
                      requires_help_tokens=["--plan", "[prompt]"], modes=READ_MODES),
    ]
