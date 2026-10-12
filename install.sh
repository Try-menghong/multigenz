#!/usr/bin/env bash
# MultiGenz installer.
#
#   curl -fsSL https://<your-site>/install | bash
#
# What it does (nothing else): finds Python >= 3.12 (or lets uv provide one), creates a private virtualenv in
# ~/.multigenz, installs MultiGenz into it, links ~/.local/bin/multigenz, and runs `multigenz doctor`.
# It never uses sudo, never installs system packages and never touches your repositories.
#
# Environment overrides:
#   MULTIGENZ_REF            git branch/tag to install            (default: main)
#   MULTIGENZ_REPO           GitHub owner/repo                    (default: Try-menghong/multigenz)
#   MULTIGENZ_SOURCE         any pip requirement or local path; overrides REPO/REF (e.g. a wheel or a checkout)
#   MULTIGENZ_INSTALL_DIR    where the virtualenv lives           (default: ~/.multigenz)
#   MULTIGENZ_BIN_DIR        where the `multigenz` link goes      (default: ~/.local/bin)
#   MULTIGENZ_NO_MODIFY_PATH=1   do not add MULTIGENZ_BIN_DIR to your shell profile
#   MULTIGENZ_ALLOW_UNSUPPORTED=1 install on a non-Linux OS (agents will not run: the sandbox is Linux-only)
#
# Uninstall: curl -fsSL https://<your-site>/install | bash -s -- --uninstall
#
# The whole script is a function called on the last line, so a truncated download runs nothing.

set -euo pipefail

main() {
  local repo="${MULTIGENZ_REPO:-Try-menghong/multigenz}"
  local ref="${MULTIGENZ_REF:-main}"
  local install_dir="${MULTIGENZ_INSTALL_DIR:-$HOME/.multigenz}"
  local bin_dir="${MULTIGENZ_BIN_DIR:-$HOME/.local/bin}"
  local venv="$install_dir/venv"

  if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
  else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
  fi

  if [ "${1:-}" = "--uninstall" ]; then
    uninstall "$install_dir" "$bin_dir"
    return 0
  fi
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
    return 0
  fi

  say "${BOLD}MultiGenz installer${RESET}"

  # -- platform -----------------------------------------------------------------------------------------------
  local os
  os="$(uname -s)"
  if [ "$os" != "Linux" ]; then
    if [ "${MULTIGENZ_ALLOW_UNSUPPORTED:-}" != "1" ]; then
      die "MultiGenz runs agents in a Linux user-namespace sandbox and does not support $os.
Set MULTIGENZ_ALLOW_UNSUPPORTED=1 to install anyway (planning/inspection only; agents will refuse to run)."
    fi
    warn "$os is not supported for running agents (the sandbox is Linux-only); continuing because you asked"
  fi
  need git "install git (2.38 or newer) with your package manager"
  local gitv
  gitv="$(git --version | awk '{print $3}')"
  if ! version_ge "$gitv" "2.38"; then
    die "git $gitv is too old; MultiGenz needs git >= 2.38 (for merge-tree --write-tree)"
  fi
  if ! command -v unshare >/dev/null 2>&1; then
    warn "unshare(1) not found (package util-linux); the userns sandbox will be unavailable until it is installed"
  fi

  # -- source -------------------------------------------------------------------------------------------------
  local source="${MULTIGENZ_SOURCE:-}"
  if [ -z "$source" ]; then
    source="$(pick_git_source "$repo" "$ref")"
  elif [ -e "$source" ]; then
    source="$(cd "$source" && pwd)"
  fi
  say "source:  ${DIM}${source}${RESET}"

  # -- python + virtualenv -------------------------------------------------------------------------------------
  mkdir -p "$install_dir"
  chmod 700 "$install_dir"
  if command -v uv >/dev/null 2>&1; then
    say "python:  ${DIM}via uv ($(uv --version))${RESET}"
    if [ ! -x "$venv/bin/python" ]; then
      uv venv --quiet --python ">=3.12" "$venv"
    fi
    if ! uv pip install --quiet --python "$venv/bin/python" --upgrade "$source"; then
      die "installing $source failed (see above). If it is not a MultiGenz package, set MULTIGENZ_REF or MULTIGENZ_SOURCE."
    fi
  else
    local py
    py="$(find_python)"
    if [ -z "$py" ]; then
      die "Python >= 3.12 not found. Install it (e.g. 'sudo apt install python3.12 python3.12-venv'),
or install uv (https://docs.astral.sh/uv/) and re-run this installer: uv can provide Python for you."
    fi
    say "python:  ${DIM}$py ($("$py" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))${RESET}"
    if [ ! -x "$venv/bin/python" ]; then
      if ! "$py" -m venv "$venv"; then
        die "could not create a virtualenv with $py (Debian/Ubuntu: sudo apt install python3-venv)"
      fi
    fi
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    if ! "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$source"; then
      die "installing $source failed (see above). If it is not a MultiGenz package, set MULTIGENZ_REF or MULTIGENZ_SOURCE."
    fi
  fi

  if [ ! -x "$venv/bin/multigenz" ]; then
    die "installation finished but $venv/bin/multigenz is missing"
  fi

  # -- link + PATH ---------------------------------------------------------------------------------------------
  mkdir -p "$bin_dir"
  ln -sfn "$venv/bin/multigenz" "$bin_dir/multigenz"
  local version
  version="$("$bin_dir/multigenz" version)"
  say "${GREEN}installed${RESET} multigenz $version -> $bin_dir/multigenz"

  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) add_to_path "$bin_dir" ;;
  esac

  # -- health check (informational: a fresh install has no policy yet) -----------------------------------------
  say ""
  say "${BOLD}multigenz doctor${RESET}"
  if "$bin_dir/multigenz" doctor; then
    :
  else
    say "${DIM}(doctor reports problems above; a fresh install has no policy yet - run 'multigenz init')${RESET}"
  fi

  say ""
  say "${BOLD}Next steps${RESET}"
  say "  multigenz agents presets      # which coding-agent CLIs are supported and installed"
  say "  multigenz init                # write ~/.config/multigenz/policy.yaml with the CLIs found"
  say "  multigenz doctor              # check the sandbox, git and agents"
  say "  multigenz run \"Add a health endpoint with tests\" --repo ./your-repo"
  say "  multigenz                     # inside a repository: the interactive home (type a goal, /model, /team)"
}

usage() {
  cat <<'HELP'
MultiGenz installer: curl -fsSL <site>/install | bash [-s -- --uninstall]

Installs MultiGenz into a private virtualenv (~/.multigenz) and links ~/.local/bin/multigenz.
No sudo, no system packages, no changes to your repositories.

  MULTIGENZ_REF=<branch|tag>       version to install (default: main)
  MULTIGENZ_REPO=<owner/repo>      GitHub repository (default: Try-menghong/multigenz)
  MULTIGENZ_SOURCE=<path|pip spec> install from a checkout, wheel or any pip requirement instead
  MULTIGENZ_INSTALL_DIR=<dir>      virtualenv location (default: ~/.multigenz)
  MULTIGENZ_BIN_DIR=<dir>          link location (default: ~/.local/bin)
  MULTIGENZ_NO_MODIFY_PATH=1       do not edit your shell profile
HELP
}

say()  { printf '%s\n' "$*"; }
warn() { printf '%swarning:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    die "$1 is required: $2"
  fi
}

version_ge() {  # version_ge 2.43.0 2.38  -> true if $1 >= $2
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

find_python() {
  local c v
  for c in python3.14 python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
      if version_ge "$v" "3.12"; then
        command -v "$c"
        return 0
      fi
    fi
  done
  echo ""
}

pick_git_source() {  # https for public repositories; ssh when https needs credentials (private repositories)
  local repo="$1" ref="$2"
  if GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "https://github.com/$repo.git" "$ref" >/dev/null 2>&1; then
    echo "git+https://github.com/$repo.git@$ref"
    return 0
  fi
  if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
     git ls-remote --exit-code "git@github.com:$repo.git" "$ref" >/dev/null 2>&1; then
    echo "git+ssh://git@github.com/$repo.git@$ref"
    return 0
  fi
  die "cannot read github.com/$repo at '$ref' over https or ssh.
If the repository is private, add an SSH key with access (or use MULTIGENZ_SOURCE=/path/to/checkout);
if '$ref' does not exist, set MULTIGENZ_REF to a branch or tag that does."
}

add_to_path() {
  local dir="$1" line rc
  line="export PATH=\"$dir:\$PATH\"  # added by the MultiGenz installer"
  if [ "${MULTIGENZ_NO_MODIFY_PATH:-}" = "1" ]; then
    warn "$dir is not on your PATH; add this to your shell profile: export PATH=\"$dir:\$PATH\""
    return 0
  fi
  case "$(basename "${SHELL:-bash}")" in
    zsh)  rc="$HOME/.zshrc" ;;
    fish) warn "fish: run 'fish_add_path $dir' to put multigenz on your PATH"; return 0 ;;
    *)    rc="$HOME/.bashrc" ;;
  esac
  if [ -f "$rc" ] && grep -Fq "# added by the MultiGenz installer" "$rc"; then
    return 0
  fi
  printf '\n%s\n' "$line" >> "$rc"
  say "added $dir to PATH in $rc (open a new terminal, or run: export PATH=\"$dir:\$PATH\")"
}

uninstall() {
  local install_dir="$1" bin_dir="$2"
  if [ -L "$bin_dir/multigenz" ]; then
    rm "$bin_dir/multigenz"
  fi
  if [ -d "$install_dir/venv" ]; then
    rm -r "$install_dir/venv"
  fi
  if [ -d "$install_dir" ] && [ -z "$(ls -A "$install_dir")" ]; then
    rmdir "$install_dir"
  fi
  say "removed the MultiGenz program. Your policy (~/.config/multigenz) and run history (~/.local/state/multigenz)"
  say "were left in place; delete them yourself if you no longer need them."
}

main "$@"
