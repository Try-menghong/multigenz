"""Contract tests: the shapes documented in docs/ARCHITECTURE.md must not drift."""

import inspect
import time

import pytest
from pydantic import ValidationError

from multigenz.contracts.agent import (AgentAdapter, AgentEvent, AgentResult, CancellationToken, ExecutionContext)
from multigenz.contracts.task import PlanDocument, TaskSpec, TaskStatus


def test_task_status_values_are_exactly_the_documented_ones():
    assert {s.value for s in TaskStatus} == {"pending", "ready", "running", "validating", "succeeded", "failed",
                                             "blocked", "cancelled"}


def test_task_spec_contains_the_documented_base_fields_with_documented_defaults():
    fields = TaskSpec.model_fields
    for name in ("schema_version", "task_id", "title", "objective", "dependencies", "allowed_paths",
                 "required_capabilities", "acceptance_commands", "max_attempts", "timeout_seconds"):
        assert name in fields, name
    t = TaskSpec(task_id="x", title="t", objective="o", allowed_paths=["a/"])
    assert (t.schema_version, t.max_attempts, t.timeout_seconds) == (1, 2, 900)
    # additive extensions are optional, so a document written against the base contract still validates
    doc = {"task_id": "x", "title": "t", "objective": "o", "allowed_paths": ["a/"]}
    assert TaskSpec.model_validate(doc) == t


def test_plan_document_is_versioned_and_strict():
    with pytest.raises(ValidationError):
        PlanDocument.model_validate({"schema_version": 2, "tasks": []})
    with pytest.raises(ValidationError):
        PlanDocument.model_validate({"tasks": [], "extra": 1})


def test_agent_event_and_result_fields():
    assert list(AgentEvent.model_fields) == ["run_id", "task_id", "attempt_id", "sequence", "kind", "message", "timestamp"]
    assert list(AgentResult.model_fields) == ["task_id", "attempt_id", "exit_code", "commit_sha", "artifacts", "success",
                                              "error_code"]
    assert AgentResult(task_id="t", attempt_id="a", exit_code=None, commit_sha=None, artifacts=[], success=False).error_code is None


def test_agent_adapter_abc_requires_the_documented_methods():
    assert AgentAdapter.__abstractmethods__ == {"healthcheck", "execute", "cancel"}
    with pytest.raises(TypeError):
        AgentAdapter()  # type: ignore[abstract]
    assert inspect.isasyncgenfunction(AgentAdapter.execute) or inspect.iscoroutinefunction(AgentAdapter.execute) \
        or "AsyncIterator" in str(inspect.signature(AgentAdapter.execute).return_annotation)


def test_execution_context_is_validated_and_holds_no_credential_values(tmp_path):
    base = dict(run_id="r", task_id="t", attempt_id="a", worktree=tmp_path, sandbox_id="s", sandbox_backend="userns",
                deadline=time.time() + 5, cancel=CancellationToken(), log_dir=tmp_path)
    ctx = ExecutionContext(**base, credential_refs=("ANTHROPIC_API_KEY",))
    assert ctx.remaining_seconds > 0
    for bad in ("sk-ant-realsecretvalue", "has space", "a=b", "1STARTS_WITH_DIGIT", "x" * 100):
        with pytest.raises(ValidationError):
            ExecutionContext(**base, credential_refs=(bad,))
    with pytest.raises(ValidationError):
        ExecutionContext(**{**base, "worktree": "relative/path"})
    with pytest.raises(ValidationError):
        ctx.run_id = "other"  # frozen record


def test_every_registered_adapter_class_implements_the_contract():
    from multigenz.adapters.antigravity import AntigravityAdapter
    from multigenz.adapters.claude import ClaudeAdapter
    from multigenz.adapters.cli_adapter import CliAdapter
    from multigenz.adapters.codex import CodexAdapter
    from multigenz.adapters.litellm_adapter import LiteLLMAdapter
    for cls in (CliAdapter, ClaudeAdapter, CodexAdapter, AntigravityAdapter, LiteLLMAdapter):
        assert issubclass(cls, AgentAdapter) and not getattr(cls, "__abstractmethods__", None), cls
