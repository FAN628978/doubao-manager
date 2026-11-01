from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping

from .tools import TOOL_DEFINITIONS

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "doubao_recovery"


def _read_message() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value


def _write_message(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tool_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in TOOL_DEFINITIONS
    ]


def _call_bridge(tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    job_id = os.getenv("DOUBAO_RECOVERY_JOB_ID", "").strip()
    token = os.getenv("DOUBAO_RECOVERY_TOKEN", "").strip()
    base_url = os.getenv("DOUBAO_RECOVERY_BRIDGE_URL", "").rstrip("/")
    if not job_id or not token or not base_url:
        raise RuntimeError("recovery MCP bridge environment is incomplete")
    body = json.dumps(
        {
            "job_id": job_id,
            "tool": tool,
            "arguments": dict(arguments),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/api/internal/agent-recovery/tool",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Doubao-Agent-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"recovery bridge HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"recovery bridge unavailable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("recovery bridge returned invalid JSON")
    return payload


def _handle(message: Mapping[str, Any]) -> dict[str, Any] | None:
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": _tool_catalog()},
        }
    if method == "tools/call":
        params = message.get("params") or {}
        tool = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        allowed = {item["name"] for item in TOOL_DEFINITIONS}
        if tool not in allowed:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": f"unknown tool: {tool}"},
            }
        try:
            result = _call_bridge(tool, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False),
                        }
                    ],
                    "isError": not bool(result.get("success")),
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)[:1000]}],
                    "isError": True,
                },
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unsupported method: {method}"},
    }


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        response = _handle(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
