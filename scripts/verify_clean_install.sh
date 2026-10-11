#!/usr/bin/env bash
# Build the wheel, install it into a brand-new virtualenv (NOT editable, no src/ on the path) and run the whole
# test suite against the installed package. Exits non-zero on any failure; nothing is masked.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
py="${PYTHON:-3.12}"
work="$(mktemp -d)"
trap 'echo "work dir kept at $work"' EXIT

uv build --wheel --out-dir "$work/dist" "$repo"
uv venv --python "$py" "$work/venv"
uv pip install --python "$work/venv/bin/python" "$work"/dist/multigenz-*.whl pytest pytest-asyncio fastapi httpx litellm

# tests only (no src/): any import of multigenz must resolve to the installed wheel
mkdir "$work/checkout"
cp -r "$repo/tests" "$repo/config" "$repo/pyproject.toml" "$work/checkout/"
cd "$work/checkout"
"$work/venv/bin/python" - <<'PY'
import multigenz, pathlib, sys
p = pathlib.Path(multigenz.__file__).resolve()
assert "site-packages" in p.parts, f"multigenz is not the installed wheel: {p}"
print("testing installed package:", p, "python", sys.version.split()[0])
PY
"$work/venv/bin/multigenz" version
"$work/venv/bin/multigenz" doctor --policy config/policy.userns.example.yaml
"$work/venv/bin/python" -m pytest -q "$@"
