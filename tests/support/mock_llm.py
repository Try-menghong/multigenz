"""A scripted, OpenAI-compatible model server for offline end-to-end tests of real coding-agent CLIs.

It speaks enough of the Chat Completions API (``/v1/chat/completions``, streaming and not) and the Responses API
(``/v1/responses``, streaming) for CLIs such as Codex, opencode and Cline to run a full agent turn against it:

1. the first request of a conversation is answered with ONE tool call that creates ``MOCK_FILE`` in the agent's
   working directory, using whichever shell- or write-tool the CLI offered (the arguments are built from that tool's own
   JSON schema, so no CLI-specific knowledge is hard-coded beyond a preference list of common tool names);
2. a planning request (MultiGenz's planner prompt) is answered with a one-task JSON plan for that file;
3. once the request carries a tool result it calls the CLI's completion tool if it offers one (e.g. Cline's
   ``submit_and_exit``), and otherwise answers with a short final message and stops.

Every request body is appended to ``--log`` (one JSON object per line) so tests can assert what the CLI sent.
Nothing here talks to a real provider. Run: ``python -m tests.support.mock_llm --port 0 --portfile p --log l``.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FILE_NAME = os.environ.get("MOCK_FILE", "mock_output.txt")
FILE_TEXT = os.environ.get("MOCK_TEXT", "written by the mock model")
FINAL_TEXT = "Done: created " + FILE_NAME
PLANNER_MARKER = "Produce a task DAG for the goal below"
PLAN = {"schema_version": 1, "summary": "create the requested file", "tasks": [{
    "schema_version": 1, "task_id": "create-file", "title": "Create " + FILE_NAME,
    "objective": "Create the file " + FILE_NAME, "dependencies": [], "allowed_paths": [FILE_NAME],
    "required_capabilities": ["coding"], "acceptance_commands": [], "shared_paths": [],
    "required_artifacts": [FILE_NAME], "validation_profile": None}]}

# Preference order. Shell tools run in the agent's cwd; write tools are the fallback.
SHELL_TOOLS = ("bash", "shell", "shell_command", "exec_command", "execute_command", "run_shell_command", "run_command",
               "run_commands")
WRITE_TOOLS = ("write", "write_to_file", "write_file", "create_file")
# Some CLIs (Cline) only finish when a dedicated completion tool is called.
COMPLETION_TOOLS = ("submit_and_exit", "attempt_completion", "complete_task", "task_complete")


def _shell_cmd() -> str:
    return f"printf '%s\\n' '{FILE_TEXT}' > {FILE_NAME}"


def _fill(schema: dict, values: dict) -> dict:
    """Build arguments for a tool from its JSON schema: known keys get our values, other required keys defaults."""
    props = (schema or {}).get("properties", {}) or {}
    required = (schema or {}).get("required", []) or []
    args: dict = {}
    for key, spec in props.items():
        t = spec.get("type") if isinstance(spec, dict) else None
        if key in values:
            v = values[key]
            if t == "array" and isinstance(v, str):
                v = ["bash", "-lc", v]
            args[key] = v
        elif key in required:
            args[key] = {"string": "mock", "boolean": False, "integer": 0, "number": 0, "array": [], "object": {}}.get(t, "mock")
    return args


def choose_call(tools: list[dict]) -> tuple[str, dict] | None:
    """Pick a tool the CLI offered and build its arguments. ``tools`` are normalised {name, parameters}."""
    by_name = {t["name"].lower(): t for t in tools if t.get("name")}
    for name in SHELL_TOOLS:
        if name in by_name:
            t = by_name[name]
            cmd = _shell_cmd()
            return t["name"], _fill(t.get("parameters") or {}, {
                "command": cmd, "cmd": cmd, "commands": [cmd], "description": "create the requested file",
                "requires_approval": False,
            })
    for name in WRITE_TOOLS:
        if name in by_name:
            t = by_name[name]
            return t["name"], _fill(t.get("parameters") or {}, {
                "filePath": FILE_NAME, "file_path": FILE_NAME, "path": FILE_NAME, "content": FILE_TEXT + "\n",
                "absolute_path": FILE_NAME,
            })
    return None


def completion_call(tools: list[dict]) -> tuple[str, dict] | None:
    by_name = {t["name"].lower(): t for t in tools if t.get("name")}
    for name in COMPLETION_TOOLS:
        if name in by_name:
            t = by_name[name]
            return t["name"], _fill(t.get("parameters") or {}, {
                "summary": FINAL_TEXT + "; verified by listing it", "result": FINAL_TEXT, "verified": True})
    return None


def next_call(tools: list[dict], results: int, called: set[str]) -> tuple[str, dict] | None:
    """Turn logic: no tool result yet -> do the work; then call a completion tool if the CLI requires one."""
    if results == 0:
        return choose_call(tools)
    done = completion_call(tools)
    if done and done[0] not in called:
        return done
    return None


def _texts(obj) -> list[str]:
    """All user-visible text in a request (chat messages or responses input), however it is nested."""
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for x in obj:
            out.extend(_texts(x))
    elif isinstance(obj, dict):
        for k in ("content", "text", "input", "messages"):
            if k in obj:
                out.extend(_texts(obj[k]))
    return out


def is_planning(body: dict) -> bool:
    return any(PLANNER_MARKER in t for t in _texts(body.get("messages") or body.get("input") or []))


def _norm_tools(raw: list) -> list[dict]:
    out = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        if "function" in t and isinstance(t["function"], dict):           # chat completions
            out.append({"name": t["function"].get("name"), "parameters": t["function"].get("parameters")})
        elif t.get("type") == "function":                                  # responses API
            out.append({"name": t.get("name"), "parameters": t.get("parameters")})
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-llm/1"
    log_path: Path | None = None
    lock = threading.Lock()

    def log_message(self, *a) -> None:  # quiet
        pass

    def _record(self, body: dict) -> None:
        if self.log_path:
            with self.lock, open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"path": self.path, "body": body}) + "\n")

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

    def _sse(self, data: dict | str, event: str | None = None) -> None:
        chunk = (f"event: {event}\n" if event else "") + "data: " + (data if isinstance(data, str) else json.dumps(data)) + "\n\n"
        self.wfile.write(chunk.encode())
        self.wfile.flush()

    def do_GET(self) -> None:  # model listings some CLIs query
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": "mock-model", "object": "model", "owned_by": "mock"}]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {"_raw": raw.decode(errors="replace")}
        self._record(body)
        if self.path.rstrip("/").endswith("/chat/completions"):
            self._chat(body)
        elif self.path.rstrip("/").endswith("/responses"):
            self._responses(body)
        else:
            self._json(404, {"error": {"message": f"unsupported path {self.path}"}})

    # -- chat completions ------------------------------------------------------------------------------------------
    def _chat(self, body: dict) -> None:
        msgs = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        results = sum(m.get("role") == "tool" for m in msgs)
        called = {tc.get("function", {}).get("name") for m in msgs for tc in (m.get("tool_calls") or [])}
        planning = is_planning(body)
        call = None if planning else next_call(_norm_tools(body.get("tools")), results, called)
        final = json.dumps(PLAN) if planning else FINAL_TEXT
        model = body.get("model", "mock-model")
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            msg: dict = {"role": "assistant", "content": None if call else final}
            if call:
                msg["tool_calls"] = [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                                      "function": {"name": call[0], "arguments": json.dumps(call[1])}}]
            self._json(200, {"id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
                             "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if call else "stop"}],
                             "usage": usage})
            return
        self._sse_start()
        base = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
        self._sse(base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        if call:
            self._sse(base | {"choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                "function": {"name": call[0], "arguments": json.dumps(call[1])}}]}, "finish_reason": None}]})
            self._sse(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            self._sse(base | {"choices": [{"index": 0, "delta": {"content": final}, "finish_reason": None}]})
            self._sse(base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse(base | {"choices": [], "usage": usage})
        self._sse("[DONE]")

    # -- responses API ---------------------------------------------------------------------------------------------
    def _responses(self, body: dict) -> None:
        items = [i for i in body.get("input") or [] if isinstance(i, dict)]
        results = sum(i.get("type") in ("function_call_output", "custom_tool_call_output") for i in items)
        called = {i.get("name") for i in items if i.get("type") == "function_call"}
        planning = is_planning(body)
        call = None if planning else next_call(_norm_tools(body.get("tools")), results, called)
        final = json.dumps(PLAN) if planning else FINAL_TEXT
        rid = "resp_" + uuid.uuid4().hex[:12]
        model = body.get("model", "mock-model")
        if call:
            item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:8], "call_id": "call_" + uuid.uuid4().hex[:8],
                    "name": call[0], "arguments": json.dumps(call[1]), "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:8], "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": final, "annotations": []}]}
        usage = {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                 "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}
        resp = {"id": rid, "object": "response", "created_at": int(time.time()), "model": model, "status": "completed",
                "output": [item], "usage": usage}
        if not body.get("stream"):
            self._json(200, resp)
            return
        self._sse_start()
        self._sse({"type": "response.created", "response": resp | {"status": "in_progress", "output": []}},
                  "response.created")
        self._sse({"type": "response.output_item.added", "output_index": 0, "item": item}, "response.output_item.added")
        if not call:
            self._sse({"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
                       "content_index": 0, "delta": final}, "response.output_text.delta")
        self._sse({"type": "response.output_item.done", "output_index": 0, "item": item}, "response.output_item.done")
        self._sse({"type": "response.completed", "response": resp}, "response.completed")


def serve(port: int = 0, log: Path | None = None) -> ThreadingHTTPServer:
    Handler.log_path = log
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--portfile")
    ap.add_argument("--log")
    a = ap.parse_args()
    srv = serve(a.port, Path(a.log) if a.log else None)
    if a.portfile:
        Path(a.portfile).write_text(str(srv.server_address[1]))
    print(f"mock-llm listening on 127.0.0.1:{srv.server_address[1]}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
