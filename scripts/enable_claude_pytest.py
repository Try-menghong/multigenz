#!/usr/bin/env python3
"""Add the pytest-capable Claude invocation to a policy file and raise max_task_attempts.

usage: python3 scripts/enable_claude_pytest.py my-policy.yaml [attempts]

Edits the text in place (comments are kept), writes my-policy.yaml.bak first, validates the result with
MultiGenz's own policy loader and restores the backup if it is invalid.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

BLOCK = '''    invocations:
      - name: print-accept-edits-pytest
        argv: ["{command}", "--print", "--permission-mode", "acceptEdits", "--allowedTools", "Bash(python3 -m pytest:*)", "--no-session-persistence"]
        stdin: prompt
        requires_help_tokens: ["--print", "--permission-mode", "\\"acceptEdits\\"", "--allowedTools", "--no-session-persistence"]
        modes: [code, conflict]
      - name: print-plan
        argv: ["{command}", "--print", "--permission-mode", "plan", "--no-session-persistence"]
        stdin: prompt
        requires_help_tokens: ["--print", "--permission-mode", "\\"plan\\"", "--no-session-persistence"]
        modes: [plan, review, analysis]
'''


def indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    attempts = sys.argv[2] if len(sys.argv) > 2 else "4"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if any(re.match(r"^ {4}invocations:", l) for l in lines):
        print("an uncommented `invocations:` already exists under the agent; not adding another")
        return 1
    start = next((i for i, l in enumerate(lines) if re.match(r"^ {4}credentials:", l)), None)
    if start is None:
        print("could not find a `    credentials:` line (4 spaces) under the claude agent; add the block by hand")
        return 1
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or indent(lines[end]) > 4):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # insert right after the credentials block, before trailing blank lines
    lines[end:end] = BLOCK.splitlines(keepends=True)
    text = "".join(lines)
    text, n = re.subn(r"(?m)^(\s*max_task_attempts:).*$", rf"\1 {attempts}", text)
    shutil.copy(path, str(path) + ".bak")
    path.write_text(text, encoding="utf-8")
    try:
        from multigenz.config import load_policy

        pol = load_policy(path)
        names = [i.name for a in pol.agents.values() for i in a.invocations]
    except Exception as exc:  # noqa: BLE001 - any failure means: put the original back
        shutil.copy(str(path) + ".bak", path)
        print(f"result was not a valid policy ({exc}); original restored")
        return 1
    print(f"ok: invocations {names}; max_task_attempts set on {n} line(s); backup at {path}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
