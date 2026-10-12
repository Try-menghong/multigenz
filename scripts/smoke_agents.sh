#!/usr/bin/env bash
# Try every enabled agent of your policy against its REAL provider, one small task each, on a throwaway repository.
#
#   scripts/smoke_agents.sh                 # all enabled agents (uses $MULTIGENZ_POLICY or ~/.config/multigenz/policy.yaml)
#   scripts/smoke_agents.sh codex opencode  # only these
#
# Each agent is asked to create hello.txt in a scratch git repository under /tmp; MultiGenz runs it in the sandbox
# and validates the resulting commit. This uses a little of each agent's plan/API quota. Your repositories are not
# touched. Exit status: 0 if every tried agent produced a validated commit, 1 otherwise.
set -euo pipefail

if ! command -v multigenz >/dev/null 2>&1; then
  echo "multigenz is not on PATH (install it first)" >&2
  exit 2
fi

scratch="$(mktemp -d /tmp/multigenz-smoke.XXXXXX)"
repo="$scratch/repo"
mkdir -p "$repo"
git -C "$repo" init -q -b main
git -C "$repo" -c user.email=smoke@multigenz -c user.name=smoke commit -q --allow-empty -m init

if [ "$#" -gt 0 ]; then
  agents=("$@")
else
  mapfile -t agents < <(multigenz agents list --json | python3 -c '
import json, sys
for row in json.load(sys.stdin):
    if row["enabled"] and row["adapter"] == "cli" and "code" in (row.get("modes") or []):
        print(row["name"])')
fi
if [ "${#agents[@]}" -eq 0 ]; then
  echo "no enabled CLI agents in your policy (try: multigenz agents presets; multigenz agents add <preset>)" >&2
  exit 2
fi

failed=0
for agent in "${agents[@]}"; do
  printf '\n== %s\n' "$agent"
  out="$scratch/$agent.log"
  if multigenz agents run "$agent" "Create a file named hello.txt containing exactly the line: hello from $agent" \
       --repo "$repo" --allow hello.txt --timeout 600 >"$out" 2>&1 && grep -q '^VALIDATED' "$out"; then
    branch="$(awk '/^branch /{print $2}' "$out")"
    printf 'VALIDATED  %s: %s\n' "$branch" "$(git -C "$repo" show "$branch:hello.txt" 2>/dev/null | head -1)"
  else
    failed=1
    echo "FAILED (last lines below; full log: $out)"
    tail -n 15 "$out"
  fi
done

printf '\nscratch repository and logs: %s\n' "$scratch"
exit "$failed"
