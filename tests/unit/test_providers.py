import json
import os

import pytest

from multigenz.config import ProviderCfg, parse_policy
from multigenz.core.redaction import Redactor
from multigenz.errors import ConfigError
from multigenz.policy_edit import agent_name_for
from multigenz.providers import (PRESETS, ProviderError, litellm_model, merged_environ, opencode_config, read_secrets,
                                 set_secret)

KEY = "sk-unit-provider-0123456789abcdef"


def test_secrets_file_is_private_atomic_and_overridden_by_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = set_secret("MYPROV_API_KEY", KEY)
    set_secret("OTHER_API_KEY", "other-value-123456")
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert read_secrets() == {"MYPROV_API_KEY": KEY, "OTHER_API_KEY": "other-value-123456"}
    assert merged_environ({"PATH": "/bin"})["MYPROV_API_KEY"] == KEY
    assert merged_environ({"MYPROV_API_KEY": "from-env"})["MYPROV_API_KEY"] == "from-env"   # the environment wins
    for bad in (("1BAD", "x"), ("OK_NAME", "two\nlines"), ("OK_NAME", "")):
        with pytest.raises(ProviderError):
            set_secret(*bad)
    assert Redactor.from_environ({}, extra=read_secrets().values()).redact(f"token {KEY} used") == "token [REDACTED] used"


def test_generated_configs_reference_the_key_by_name_only():
    cfg = ProviderCfg(base_url="https://openrouter.ai/api/v1/", api_key_env="OPENROUTER_API_KEY")
    oc = opencode_config("openrouter", cfg, "qwen/qwen3-coder")
    assert oc["model"] == "mg-openrouter/qwen/qwen3-coder"
    prov = oc["provider"]["mg-openrouter"]
    assert prov["options"] == {"baseURL": "https://openrouter.ai/api/v1", "apiKey": "{env:OPENROUTER_API_KEY}"}
    assert prov["npm"] == "@ai-sdk/openai-compatible" and "qwen/qwen3-coder" in prov["models"]
    local = opencode_config("ollama", ProviderCfg(base_url="http://localhost:11434/v1"), "qwen2.5-coder")
    assert "apiKey" not in local["provider"]["mg-ollama"]["options"]
    assert opencode_config("a", ProviderCfg(kind="anthropic", base_url="https://api.anthropic.com/v1",
                                            api_key_env="ANTHROPIC_API_KEY"), "m")["provider"]["mg-a"]["npm"] == \
        "@ai-sdk/anthropic"
    assert litellm_model(cfg, "x/y") == "openai/x/y"


def test_policy_validation_for_providers():
    with pytest.raises(ConfigError, match="http"):
        parse_policy({"providers": {"p": {"base_url": "ftp://x"}}})
    with pytest.raises(ConfigError, match="environment variable name"):
        parse_policy({"providers": {"p": {"base_url": "https://x", "api_key_env": "has space"}}})
    with pytest.raises(ConfigError, match="needs a model"):
        parse_policy({"providers": {"p": {"base_url": "https://x"}},
                      "agents": {"a": {"adapter": "cli", "command": "opencode", "provider": "p"}}})
    pol = parse_policy({"providers": {"p": {"base_url": "https://x", "api_key_env": "P_KEY"}},
                        "agents": {"a": {"adapter": "cli", "command": "/opt/bin/opencode", "provider": "p", "model": "m"},
                                   "t": {"adapter": "litellm", "provider": "p", "model": "m"}}})
    assert pol.agents["a"].credentials.env == ["P_KEY"]          # the provider key goes to that agent only
    assert pol.agents["t"].credentials.env == []                  # text agents call the API from the host
    assert all(p.base_url.startswith("http") or p.key == "custom" for p in PRESETS.values())


def test_agent_names_are_valid_and_unique():
    assert agent_name_for("openrouter", "qwen/qwen3-coder:free", set()) == "openrouter-qwen3-coder-free"
    assert agent_name_for("openrouter", "qwen/qwen3-coder:free", {"openrouter-qwen3-coder-free"}).endswith("-2")
    assert agent_name_for("x", "123-model", set()) == "x-123-model"
    parse_policy({"agents": {agent_name_for("my", "A/B.C D", set()): {"adapter": "litellm", "model": "m"}}})
