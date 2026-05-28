from __future__ import annotations

import json
from pathlib import Path
import sys
import textwrap

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.domain import CreateRunRequest
from app.modules.miniapp_agent_loop.tool_router import ToolRouter
from app.services.lsp_server_manager import LspServerManager, ManagedLspServer, decode_lsp_messages, encode_lsp_message


def test_lsp_json_rpc_framing_round_trips_partial_messages() -> None:
    first = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"rootUri": "file:///tmp/x"}}
    second = {"jsonrpc": "2.0", "method": "initialized", "params": {}}
    encoded = encode_lsp_message(first) + encode_lsp_message(second)

    messages, remaining = decode_lsp_messages(encoded[:-4])
    completed, tail = decode_lsp_messages(remaining + encoded[-4:])

    assert messages == [first]
    assert completed == [second]
    assert tail == b""


def test_managed_lsp_server_collects_fake_publish_diagnostics(tmp_path: Path) -> None:
    server_script = tmp_path / "fake_lsp.py"
    server_script.write_text(
        textwrap.dedent(
            r'''
            from __future__ import annotations

            import json
            import sys

            def read_message():
                header = b""
                while b"\r\n\r\n" not in header:
                    chunk = sys.stdin.buffer.read(1)
                    if not chunk:
                        return None
                    header += chunk
                length = 0
                for line in header.decode("ascii", errors="ignore").splitlines():
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1].strip())
                return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))

            def send(payload):
                body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                sys.stdout.buffer.flush()

            while True:
                message = read_message()
                if message is None:
                    break
                method = message.get("method")
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {"textDocumentSync": 1}}})
                elif method == "textDocument/didOpen":
                    uri = message["params"]["textDocument"]["uri"]
                    send({
                        "jsonrpc": "2.0",
                        "method": "textDocument/publishDiagnostics",
                        "params": {
                            "uri": uri,
                            "diagnostics": [{
                                "range": {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 5}},
                                "severity": 1,
                                "message": "fake diagnostic",
                                "code": "FAKE001",
                            }],
                        },
                    })
                elif method == "shutdown":
                    send({"jsonrpc": "2.0", "id": message["id"], "result": None})
                elif method == "exit":
                    break
            '''
        ),
        encoding="utf-8",
    )
    source = tmp_path / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    server = ManagedLspServer(
        key="fake",
        workspace_id="ws_fake",
        run_id=None,
        language="python",
        root=tmp_path,
        command=[sys.executable, str(server_script)],
    )

    state = server.start()
    server.open_file(source)
    diagnostics = server.collect_diagnostics(timeout=1.0)
    server.shutdown()

    assert state.status == "running"
    assert diagnostics[0]["method"] == "textDocument/publishDiagnostics"
    assert diagnostics[0]["params"]["diagnostics"][0]["message"] == "fake diagnostic"


def test_lsp_context_endpoint_uses_static_fallback_when_server_unavailable(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "LSP Fallback Workspace",
            "description": "LSP fallback test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    container = app.state.container
    container.lsp_context_service.server_manager = LspServerManager(command_overrides={"python": []})
    source = container.workspace_service.source_dir(workspace["workspace_id"]) / "app.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def hello():\n    return 1\n", encoding="utf-8")

    report = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/context").json()
    diagnostics = client.get(f"/workspaces/{workspace['workspace_id']}/diagnostics/lsp").json()

    assert report["schema"] == "grounded.lsp_context.v1"
    assert report["engine"] == "static"
    assert report["fallback_used"] is True
    assert report["diagnostics_ref"].startswith("lsp_diagnostics:")
    assert report["symbol_index_ref"].startswith("lsp_symbol_index:")
    assert report["route_graph_ref"].startswith("lsp_route_graph:")
    assert diagnostics["engine"] == "static"
    assert diagnostics["server_status"]["python"]["status"] == "unavailable"
    assert "jumps" in diagnostics
    assert "next_sequence" in diagnostics


def test_lsp_run_context_endpoint_and_tool_visibility(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path)
    client = TestClient(app)
    workspace = client.post(
        "/workspaces",
        json={
            "name": "LSP Run Workspace",
            "description": "LSP run context test",
            "target_platform": "telegram_mini_app",
            "preview_profile": "telegram_mock",
        },
    ).json()
    app.state.container.run_service._execute_run = lambda run_id, payload: None
    run = app.state.container.run_service.create_run(
        workspace["workspace_id"],
        CreateRunRequest(prompt="inspect lsp", mode="generate", intent="create", generation_mode="fast"),
    )
    tool_names = {tool["name"] for tool in ToolRouter.allowed_openai_tools()}

    report = client.get(f"/runs/{run.run_id}/lsp-context").json()
    servers = client.get(f"/workspaces/{workspace['workspace_id']}/lsp/servers").json()

    assert {"lsp_diagnostics", "lsp_symbol_context", "lsp_definition", "lsp_find_references", "lsp_route_graph", "lsp_route_static_context"}.issubset(tool_names)
    assert report["schema"] == "grounded.lsp_context.v1"
    assert report["run_id"] == run.run_id
    assert report["lsp_context_ref"].endswith(run.run_id)
    assert servers["schema"] == "grounded.lsp_servers.v1"
