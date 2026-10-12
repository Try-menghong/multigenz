"""API providers: presets, model discovery, keys and the coding harness that talks to them.

* ``PRESETS``: well-known endpoints (OpenAI, OpenRouter, DeepSeek, Groq, Anthropic, local Ollama/LM Studio, ...).
* ``fetch_models``: ``GET <base_url>/models`` with the provider's key, returning the model ids it serves.
* Keys are never written to the policy. ``set_secret`` stores one in ``~/.config/multigenz/secrets.env`` (mode 0600);
  ``merged_environ`` makes those available like environment variables (the real environment wins).
* ``opencode_config``: the opencode configuration that makes an agent use ``provider/model``. The key is referenced
  as ``{env:NAME}`` (opencode substitutes it inside the sandbox), so no key is ever written to a file either.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from multigenz.config import ProviderCfg


@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    kind: str
    base_url: str
    api_key_env: str | None


PRESETS: dict[str, ProviderPreset] = {p.key: p for p in [
    ProviderPreset("openrouter", "OpenRouter", "openai-compatible", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ProviderPreset("openai", "OpenAI", "openai-compatible", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ProviderPreset("anthropic", "Anthropic", "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    ProviderPreset("deepseek", "DeepSeek", "openai-compatible", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    ProviderPreset("groq", "Groq", "openai-compatible", "https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    ProviderPreset("mistral", "Mistral", "openai-compatible", "https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    ProviderPreset("together", "Together", "openai-compatible", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    ProviderPreset("xai", "xAI", "openai-compatible", "https://api.x.ai/v1", "XAI_API_KEY"),
    ProviderPreset("gemini", "Google Gemini", "openai-compatible",
                   "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    ProviderPreset("ollama", "Ollama (local)", "openai-compatible", "http://localhost:11434/v1", None),
    ProviderPreset("lmstudio", "LM Studio (local)", "openai-compatible", "http://localhost:1234/v1", None),
    ProviderPreset("custom", "Custom OpenAI-compatible", "openai-compatible", "", None),
]}


class ProviderError(Exception):
    """A provider could not be reached or refused the request; the message says what to fix."""


# -- keys ------------------------------------------------------------------------------------------------------------
def secrets_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "multigenz" / "secrets.env"


def read_secrets(path: Path | None = None) -> dict[str, str]:
    path = path or secrets_path()
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name.strip()):
            out[name.strip()] = value
    return out


def set_secret(name: str, value: str, path: Path | None = None) -> Path:
    """Store a key (0600 file in a 0700 directory, written atomically). Never logged, never in the policy."""
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", name):
        raise ProviderError(f"invalid environment variable name {name!r}")
    if not value or "\n" in value or "\r" in value:
        raise ProviderError("the key is empty or spans several lines")
    path = path or secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = read_secrets(path)
    data[name] = value.strip()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# API keys stored by MultiGenz (mode 0600). The environment overrides these.\n")
        for k, v in sorted(data.items()):
            fh.write(f"{k}={v}\n")
    os.replace(tmp, path)
    return path


def merged_environ(environ=None) -> dict[str, str]:
    """The environment plus stored keys; a variable set in the real environment always wins."""
    env = dict(os.environ if environ is None else environ)
    return {**read_secrets(), **env}


def key_for(cfg: ProviderCfg, environ=None) -> str | None:
    if not cfg.api_key_env:
        return None
    return merged_environ(environ).get(cfg.api_key_env) or None


# -- discovery ---------------------------------------------------------------------------------------------------------
def fetch_models(cfg: ProviderCfg, api_key: str | None, *, timeout: float = 20.0) -> list[str]:
    """Model ids served by a provider (``GET <base_url>/models``), sorted. Raises ProviderError with a clear reason."""
    url = cfg.base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json", "User-Agent": "multigenz"}
    if cfg.kind == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - administrator-configured URL
            body = resp.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderError(f"{url}: the provider rejected the API key (HTTP {exc.code})") from exc
        raise ProviderError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(f"{url}: cannot reach the provider ({reason})") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{url}: the answer is not JSON") from exc
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None:
        items = data.get("models")  # some servers (e.g. Ollama's native API) use "models"
    ids = set()
    for it in items or []:
        if isinstance(it, dict):
            mid = it.get("id") or it.get("name") or it.get("model")
            if isinstance(mid, str) and mid.strip():
                ids.add(mid.strip().removeprefix("models/"))
        elif isinstance(it, str):
            ids.add(it.strip())
    if not ids:
        raise ProviderError(f"{url}: no models listed")
    return sorted(ids)


# -- the coding harness ------------------------------------------------------------------------------------------------
def opencode_config(provider_name: str, cfg: ProviderCfg, model: str) -> dict:
    """opencode configuration pointing an agent at ``provider/model`` (key referenced as ``{env:NAME}``)."""
    options: dict = {"baseURL": cfg.base_url}
    if cfg.api_key_env:
        options["apiKey"] = "{env:" + cfg.api_key_env + "}"
    pid = f"mg-{provider_name}"
    entry: dict = {"name": provider_name, "options": options, "models": {model: {"name": model}}}
    if cfg.kind == "anthropic":
        entry["npm"] = "@ai-sdk/anthropic"
    else:
        entry["npm"] = "@ai-sdk/openai-compatible"
    return {"$schema": "https://opencode.ai/config.json", "provider": {pid: entry}, "model": f"{pid}/{model}",
            "share": "disabled", "autoupdate": False}


def litellm_model(cfg: ProviderCfg, model: str) -> str:
    """The LiteLLM model string for a provider model (text-only planner/reviewer agents)."""
    return ("anthropic/" if cfg.kind == "anthropic" else "openai/") + model
