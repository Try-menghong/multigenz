#!/usr/bin/env python3
"""Add a Codex or Antigravity (agy) agent entry to a policy file.

usage: python3 scripts/add_cli_agent.py POLICY.yaml codex|agy [--cred-file SRC:DST]... [--cred-env NAME]...

The invocations written here are candidates built from the flags each binary's own --help lists. MultiGenz still
enables one only when every `requires_help_tokens` string appears in that help, and NOTHING here proves the tool
behaves as hoped (stdin prompt, sandbox, login): verify with `multigenz agents list` and `multigenz agents run`.

Edits the text in place (comments are kept), writes POLICY.yaml.bak first, validates with MultiGenz's own policy loader
and restores the backup if the result is invalid.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

CODEX = '''  codex:
    enabled: true
    adapter: cli
    command: codex
    capabilities: [coding, testing, review]
{credentials}    invocations:
      - name: exec-workspace-write
        argv: ["{{command}}", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check"]
        stdin: prompt
        help_args: ["exec", "--help"]
        requires_help_tokens: ["--sandbox", "workspace-write", "--skip-git-repo-check"]
        modes: [code, conflict]
      - name: exec-read-only
        argv: ["{{command}}", "exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        stdin: prompt
        help_args: ["exec", "--help"]
        requires_help_tokens: ["--sandbox", "read-only", "--skip-git-repo-check"]
        modes: [plan, review, analysis]
'''

AGY = '''  antigravity:
    enabled: true
    adapter: cli
    command: agy
    capabilities: [research, architecture]
{credentials}    invocations:
      - name: print-accept-edits
        argv: ["{{command}}", "--print", "--mode", "accept-edits"]
        stdin: prompt
        requires_help_tokens: ["--print", "--mode", "accept-edits"]
        modes: [code, conflict]
      - name: print-plan
        argv: ["{{command}}", "--print", "--mode", "plan"]
        stdin: prompt
        requires_help_tokens: ["--print", "--mode", "plan"]
        modes: [plan, review, analysis]
'''

AGENTS = {
    "codex": ("codex", CODEX, "~/.codex/auth.json:/home/agent/.codex/auth.json"),
    "agy": ("antigravity", AGY, None),
}


def credentials_block(files: list[str], envs: list[str]) -> str:
    if not files and not envs:
        return ""
    out = "    credentials:\n"
    if envs:
        out += f"      env: [{', '.join(envs)}]\n"
    if files:
        out += "      files:\n"
        for spec in files:
            src, sep, dst = spec.partition(":")
            if not sep or not dst.startswith("/"):
                raise SystemExit(f"--cred-file wants SRC:DST with an absolute DST, got {spec!r}")
            out += f'        - {{src: "{src}", dst: "{dst}"}}\n'
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in AGENTS:
        print(__doc__)
        return 2
    path, which = Path(argv[0]), argv[1]
    files: list[str] = []
    envs: list[str] = []
    rest = argv[2:]
    while rest:
        flag, *rest = rest
        if flag not in ("--cred-file", "--cred-env") or not rest:
            print(__doc__)
            return 2
        (files if flag == "--cred-file" else envs).append(rest.pop(0))
    if not path.is_file():
        print(f"{path}: no such policy file (hint: your policy is probably $MULTIGENZ_POLICY)")
        return 2
    key, template, default_cred = AGENTS[which]
    if not files and not envs and default_cred:
        files = [default_cred]
    block = template.format(credentials=credentials_block(files, envs))

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if any(re.match(rf"^  {key}:", l) for l in lines):
        print(f"agents.{key} already exists in {path}; not adding another")
        return 1
    start = next((i for i, l in enumerate(lines) if re.match(r"^agents:\s*(#.*)?$", l)), None)
    if start is None:
        print("could not find a top-level `agents:` line; add the block by hand")
        return 1
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith((" ", "#"))):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = block.splitlines(keepends=True)
    shutil.copy(path, str(path) + ".bak")
    path.write_text("".join(lines), encoding="utf-8")
    try:
        from multigenz.config import load_policy

        pol = load_policy(path)
        names = [i.name for i in pol.agents[key].invocations]
    except Exception as exc:  # noqa: BLE001 - any failure means: put the original back
        shutil.copy(str(path) + ".bak", path)
        print(f"result was not a valid policy ({exc}); original restored")
        return 1
    print(f"ok: agents.{key} added with invocations {names}; backup at {path}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
