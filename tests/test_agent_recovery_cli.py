import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app as application
import web_api
from agent_recovery import cli as cli_module
from agent_recovery import mcp_server
from agent_recovery.cli import (
    CLIProcessResult,
    ClaudeCLIRunner,
    parse_cli_result,
)
from agent_recovery.config import AgentConfig
from agent_recovery.service import ExceptionRecoveryService
from agent_recovery.tools import RecoveryBackend


async def _noop(jobid, arguments=None):
    return {
        "success": True,
        "verified": True,
        "recovery_complete": False,
        "detail": f"ok:{jobid}",
    }


def _backend():
    return RecoveryBackend(
        restart_browser=_noop,
        refresh_page=_noop,
        recall_existing_result=_noop,
        retry_h3_push=_noop,
        restart_auto=_noop,
        regenerate=_noop,
        switch_page=_noop,
        inspect_dom=_noop,
        dismiss_popup=_noop,
        locate_task_page=_noop,
        verify_task_status=_noop,
        check_assets=_noop,
        download_video=_noop,
        release_account=_noop,
    )


def _configure_cli(monkeypatch, tmp_path):
    shim = tmp_path / ("claude.cmd" if os.name == "nt" else "claude")
    shim.write_text("@echo off\n", encoding="utf-8")
    if os.name != "nt":
        shim.chmod(0o755)
    monkeypatch.setenv("DOUBAO_CLAUDE_CLI_PATH", str(shim))
    monkeypatch.setenv("DOUBAO_AGENT_DEDUPE_SECONDS", "0")
    monkeypatch.setenv("DOUBAO_AGENT_MAX_CONCURRENCY", "1")
    return shim


def _state():
    return {
        "id": "job_cli",
        "account_id": "account_1",
        "status": "needs_review",
        "phase": "needs_review",
        "error_code": "PAGE_LOAD_FAILED",
        "web_page_url": "https://www.doubao.com/chat/session-1",
        "quota_state": "reserved",
        "submit_mode": "auto",
        "video_path": "",
        "result_push_url": "",
    }


def test_parse_cli_result_accepts_print_json_wrapper():
    final = {
        "summary": "recovered",
        "root_cause": "browser",
        "confidence": 0.9,
        "recovery_suggestion": "recall",
        "recovery_complete": True,
        "needs_human": False,
        "manual_reason": "",
    }
    output = json.dumps({"type": "result", "result": json.dumps(final)})
    assert parse_cli_result(output) == final


def test_agent_config_defaults_to_autonomous_cli_mode(monkeypatch, tmp_path):
    shim = _configure_cli(monkeypatch, tmp_path)
    config = AgentConfig.from_env(tmp_path / "missing-settings.json")
    assert config.enabled is True
    assert config.mode == "autonomous"
    assert config.cli_path == str(shim.resolve())
    assert config.cli_permission_mode == "bypassPermissions"
    assert config.cli_ready is True


def test_claude_cli_auto_detects_powershell_shim(monkeypatch, tmp_path):
    npm = tmp_path / "npm"
    npm.mkdir()
    shim = npm / "claude.ps1"
    shim.write_text("Write-Output 'claude'\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(cli_module.shutil, "which", lambda _name: None)

    assert cli_module.find_claude_cli() == str(shim.resolve())


def test_cli_process_is_isolated_and_uses_mcp_only(monkeypatch, tmp_path):
    shim = _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:60007")
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local")
    captured = {}
    mcp_seen = {}

    class FakeProcess:
        pid = 4321
        returncode = 0

        async def communicate(self, input=None):
            command = captured["args"]
            for value in command:
                if ".recovery-" in str(value):
                    matches = re.findall(
                        r'[^\s"]*\.recovery-[^\s"]+\.json', str(value)
                    )
                    assert matches
                    config_path = Path(matches[-1])
                    mcp_seen.update(
                        json.loads(config_path.read_text(encoding="utf-8"))
                    )
                    captured["config_path"] = config_path
            return (
                json.dumps(
                    {
                        "result": json.dumps(
                            {
                                "summary": "ok",
                                "root_cause": "",
                                "confidence": 1,
                                "recovery_suggestion": "done",
                                "recovery_complete": True,
                                "needs_human": False,
                                "manual_reason": "",
                            }
                        )
                    }
                ).encode(),
                b"",
            )

    async def fake_create(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(cli_module.asyncio, "create_subprocess_exec", fake_create)
    runner = ClaudeCLIRunner(
        cli_path=str(shim),
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        bridge_url="http://127.0.0.1:8765",
        timeout_seconds=5,
        python_executable=sys.executable,
    )
    started = []
    result = asyncio.run(
        runner.run(
            job_id="job_cli",
            token="token-1234567890",
            prompt="recover job_cli",
            on_started=lambda *args: started.append(args),
        )
    )

    args = captured["args"]
    command_text = " ".join(str(value) for value in args)
    assert " -p " in f" {command_text} "
    assert "--permission-mode bypassPermissions" in command_text
    assert '--tools ""' in command_text
    assert "--mcp-config" in command_text
    assert "--strict-mcp-config" in command_text
    assert "Bash" in command_text
    env = mcp_seen["mcpServers"]["doubao_recovery"]["env"]
    assert env["DOUBAO_RECOVERY_JOB_ID"] == "job_cli"
    assert env["DOUBAO_RECOVERY_TOKEN"] == "token-1234567890"
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    child_env = captured["kwargs"]["env"]
    assert "ANTHROPIC_BASE_URL" not in child_env
    assert "ANTHROPIC_MODEL" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    assert started and started[0][0] == 4321
    assert not captured["config_path"].exists()
    assert result.exit_code == 0 and result.diagnosis["recovery_complete"] is True
    if os.name == "nt":
        assert args[0].lower().endswith("cmd.exe")


def test_cli_timeout_kills_child_process(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_module,
        "_launcher_command",
        lambda _path, _args: [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )
    runner = ClaudeCLIRunner(
        cli_path=sys.executable,
        project_root=tmp_path,
        log_dir=tmp_path / "logs",
        bridge_url="http://127.0.0.1:8765",
        timeout_seconds=1,
    )
    result = asyncio.run(
        runner.run(job_id="job_timeout", token="token-1234567890", prompt="wait")
    )

    assert result.timed_out is True
    assert result.exit_code is not None
    assert "终止" in result.error
    logs = list((tmp_path / "logs").glob("cli-job_timeout-*.log"))
    assert logs and "timeout\nTrue" in logs[0].read_text(encoding="utf-8")


def test_service_runs_cli_and_records_prompt_and_process(monkeypatch, tmp_path):
    _configure_cli(monkeypatch, tmp_path)
    state = _state()

    class FakeRunner:
        def __init__(self):
            self.prompt = ""

        async def run(self, *, job_id, token, prompt, on_started):
            self.prompt = prompt
            on_started(777, token[:12], str(tmp_path / "logs" / "cli.log"))
            state["status"] = "downloaded"
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "recovered",
                    "root_cause": "transient page",
                    "confidence": 0.9,
                    "recovery_suggestion": "recalled",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=777,
            )

    runner = FakeRunner()
    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {
            "job_state": state,
            "recent_events": [{"status": "downloading", "message": "failed"}],
        },
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: runner,
    )

    async def run():
        assert service.notify_exception(
            job_id="job_cli",
            account_id="account_1",
            error_type="ReviewNeeded",
            error_code="PAGE_LOAD_FAILED",
            error_message="page timed out",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    assert "job_cli" in runner.prompt
    assert "page timed out" in runner.prompt
    assert "https://www.doubao.com/chat/session-1" in runner.prompt
    record = json.loads(service.log_path.read_text(encoding="utf-8"))
    assert record["agent_backend"] == "claude-cli"
    assert record["recovery_success"] is True
    assert record["needs_human"] is False
    assert record["process_id"] == 777
    process_events = [
        json.loads(line)
        for line in service.process_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in process_events] == ["started", "ended"]


def test_missing_cli_is_logged_as_human_handoff(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_recovery.config.find_claude_cli", lambda _path: "")
    monkeypatch.setenv(
        "DOUBAO_CLAUDE_CLI_PATH", str(tmp_path / "missing-claude.exe")
    )
    monkeypatch.setenv("DOUBAO_AGENT_DEDUPE_SECONDS", "0")
    state = _state()
    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {"job_state": state, "recent_events": []},
        log_dir=tmp_path / "logs",
    )

    async def run():
        service.notify_exception(
            job_id="job_cli",
            account_id="account_1",
            error_type="ReviewNeeded",
            error_code="PAGE_LOAD_FAILED",
            error_message="page timed out",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    record = json.loads(service.log_path.read_text(encoding="utf-8"))
    assert record["agent_backend"] == "claude-cli"
    assert record["needs_human"] is True
    assert "Claude Code CLI" in record["agent_error"]
    assert state["status"] == "needs_review"


def test_mcp_tool_call_is_token_scoped_and_audited(monkeypatch, tmp_path):
    _configure_cli(monkeypatch, tmp_path)
    state = _state()
    holder = {}

    class ToolRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(888, token[:12], str(tmp_path / "logs" / "cli.log"))
            result = await holder["service"].execute_tool(
                job_id=job_id,
                token=token,
                tool="inspect_dom",
                arguments={},
            )
            assert result["success"] is True
            with pytest.raises(PermissionError):
                await holder["service"].execute_tool(
                    job_id=job_id,
                    token="wrong",
                    tool="inspect_dom",
                    arguments={},
                )
            state["status"] = "downloaded"
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "ok",
                    "root_cause": "",
                    "confidence": 1,
                    "recovery_suggestion": "done",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=888,
            )

    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {"job_state": state, "recent_events": []},
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: ToolRunner(),
    )
    holder["service"] = service

    async def run():
        service.notify_exception(
            job_id="job_cli",
            account_id="account_1",
            error_type="ReviewNeeded",
            error_code="PAGE_LOAD_FAILED",
            error_message="page timed out",
        )
        await service.wait_for_idle()

    asyncio.run(run())
    record = json.loads(service.log_path.read_text(encoding="utf-8"))
    assert record["action_count"] == 1
    phases = [item["phase"] for item in record["operation_ledger"]]
    assert phases == ["analysis", "action", "verification", "cli_result"]
    assert record["actions"][0]["name"] == "inspect_dom"


def test_service_shutdown_cancels_runner_and_releases_account(monkeypatch, tmp_path):
    _configure_cli(monkeypatch, tmp_path)
    state = _state()
    started = None

    class BlockingRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            nonlocal started
            on_started(999, token[:12], str(tmp_path / "logs" / "cli.log"))
            started.set()
            await asyncio.Event().wait()

    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {"job_state": state, "recent_events": []},
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: BlockingRunner(),
    )

    async def run():
        nonlocal started
        started = asyncio.Event()
        service.notify_exception(
            job_id="job_cli",
            account_id="account_1",
            error_type="ReviewNeeded",
            error_code="PAGE_LOAD_FAILED",
            error_message="page timed out",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert service.account_pending("account_1") is True
        await service.shutdown()
        await asyncio.sleep(0)
        assert service.pending_count == 0
        assert service.account_pending("account_1") is False

    asyncio.run(run())


def test_same_account_recovery_processes_are_serialized(monkeypatch, tmp_path):
    _configure_cli(monkeypatch, tmp_path)
    monkeypatch.setenv("DOUBAO_AGENT_MAX_CONCURRENCY", "2")
    states = {
        "job_a": {**_state(), "id": "job_a"},
        "job_b": {**_state(), "id": "job_b"},
    }

    class SerialRunner:
        active = 0
        max_active = 0

        async def run(self, *, job_id, token, prompt, on_started):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            on_started(
                700 + self.active,
                token[:12],
                str(tmp_path / "logs" / f"{job_id}.log"),
            )
            await asyncio.sleep(0.05)
            states[job_id]["status"] = "downloaded"
            self.active -= 1
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "ok",
                    "root_cause": "",
                    "confidence": 1,
                    "recovery_suggestion": "done",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
            )

    runner = SerialRunner()
    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda jobid: {
            "job_state": states[jobid],
            "recent_events": [],
        },
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: runner,
    )

    async def run():
        for job_id in states:
            assert service.notify_exception(
                job_id=job_id,
                account_id="account_1",
                error_type="ReviewNeeded",
                error_code="PAGE_LOAD_FAILED",
                error_message="page timed out",
            )
        await service.wait_for_idle()

    asyncio.run(run())
    assert runner.max_active == 1


def test_live_recovery_status_is_exposed_to_frontend(monkeypatch, tmp_path):
    _configure_cli(monkeypatch, tmp_path)
    state = _state()
    holder = {}
    release = None
    started = None

    class StatusRunner:
        async def run(self, *, job_id, token, prompt, on_started):
            on_started(889, token[:12], str(tmp_path / "logs" / "cli.log"))
            result = await holder["service"].execute_tool(
                job_id=job_id,
                token=token,
                tool="inspect_dom",
                arguments={},
            )
            assert result["success"] is True
            started.set()
            await release.wait()
            state["status"] = "downloaded"
            return CLIProcessResult(
                exit_code=0,
                timed_out=False,
                diagnosis={
                    "summary": "page recovered",
                    "root_cause": "stale DOM",
                    "confidence": 1,
                    "recovery_suggestion": "continued",
                    "recovery_complete": True,
                    "needs_human": False,
                    "manual_reason": "",
                },
                process_id=889,
            )

    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {"job_state": state, "recent_events": []},
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: StatusRunner(),
    )
    holder["service"] = service

    async def run():
        nonlocal release, started
        release = asyncio.Event()
        started = asyncio.Event()
        service.notify_exception(
            job_id="job_cli",
            account_id="account_1",
            error_type="ReviewNeeded",
            error_code="PAGE_LOAD_FAILED",
            error_message="page timed out",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        live = service.status_for_job("job_cli")
        assert live["active"] is True
        assert live["backend"] == "claude-cli"
        assert live["action"] == "inspect_dom"
        assert live["phase"] == "verify"
        assert service.job_active("job_cli") is True

        release.set()
        await service.wait_for_idle()
        finished = service.status_for_job("job_cli")
        assert finished["active"] is False
        assert finished["state"] == "completed"
        assert service.job_active("job_cli") is False

    asyncio.run(run())


def test_restart_cleanup_terminates_recorded_stale_process(monkeypatch, tmp_path):
    service = ExceptionRecoveryService(
        backend=_backend(),
        context_loader=lambda _jobid: {},
        log_dir=tmp_path / "logs",
        cli_runner_factory=lambda _config: object(),
    )
    service.log_dir.mkdir(parents=True)
    service.process_log_path.write_text(
        json.dumps(
            {
                "event": "started",
                "job_id": "job_cli",
                "pid": 321,
                "marker": "marker-123",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls = []

    async def fake_terminate(pid, marker):
        calls.append((pid, marker))
        return True

    monkeypatch.setattr(
        "agent_recovery.service.terminate_stale_process", fake_terminate
    )
    assert asyncio.run(service.recover_after_restart()) == 1
    assert calls == [(321, "marker-123")]


def test_mcp_catalog_and_call_payload(monkeypatch):
    initialize = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert initialize["result"]["serverInfo"]["name"] == "doubao_recovery"
    catalog = mcp_server._handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = {tool["name"] for tool in catalog["result"]["tools"]}
    assert {"inspect_dom", "recall_existing_result", "restart_auto"} <= names

    monkeypatch.setattr(
        mcp_server,
        "_call_bridge",
        lambda tool, arguments: {"success": True, "tool": tool, "args": arguments},
    )
    response = mcp_server._handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "inspect_dom", "arguments": {}},
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["tool"] == "inspect_dom"
    assert response["result"]["isError"] is False


def test_internal_tool_bridge_requires_task_token(monkeypatch, tmp_path):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "manager.sqlite3")
    application.init_db()

    class ToolService:
        async def execute_tool(self, *, job_id, token, tool, arguments):
            if token != "correct-token":
                raise PermissionError("invalid token")
            return {
                "success": True,
                "verified": True,
                "job_id": job_id,
                "tool": tool,
                "arguments": arguments,
            }

    async def fake_browser(_account_id):
        return type("Ctx", (), {"pages": []})()

    bridge = FastAPI()
    web_api.mount_video_api(
        bridge,
        db=application.db,
        get_account=application.get_account,
        get_job=application.get_job,
        browser=fake_browser,
        base_dir=tmp_path,
        downloads_dir=tmp_path / "data" / "videos",
        serialize=application.job_row,
        recovery_service=ToolService(),
    )
    with TestClient(bridge) as client:
        payload = {
            "job_id": "job_cli",
            "tool": "inspect_dom",
            "arguments": {"scope": "task"},
        }
        good = client.post(
            "/api/internal/agent-recovery/tool",
            json=payload,
            headers={"X-Doubao-Agent-Token": "correct-token"},
        )
        bad = client.post(
            "/api/internal/agent-recovery/tool",
            json=payload,
            headers={"X-Doubao-Agent-Token": "wrong-token"},
        )
    assert good.status_code == 200
    assert good.json()["tool"] == "inspect_dom"
    assert bad.status_code == 403
