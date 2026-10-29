import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


PATH = (Path(__file__).resolve().parents[1] / "plugins" / "finops" / "deploy"
        / "mcp-transport-fixture.py")
spec = importlib.util.spec_from_file_location("mcp_transport_fixture", PATH)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def call(method, params=None):
    return fixture.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": {} if params is None else params})


@pytest.mark.parametrize("size", fixture.SIZES)
def test_fixture_bytes_round_trip_with_exact_length_and_hash(size):
    response = call("tools/call", {
        "name": fixture.TOOL_NAME, "arguments": {"size": size},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    raw = base64.b64decode(payload["payload_base64"], validate=True)
    assert len(raw) == payload["byte_count"] == size
    assert hashlib.sha256(raw).hexdigest() == payload["sha256"]
    assert response["result"]["isError"] is False


@pytest.mark.parametrize("arguments", [
    {}, {"size": True}, {"size": 0}, {"size": -1}, {"size": 1048577},
    {"size": "1024"}, {"size": 1024, "path": "/etc/passwd"}, None, [],
])
def test_fixture_rejects_unbounded_or_extra_arguments(arguments):
    result = call("tools/call", {"name": fixture.TOOL_NAME, "arguments": arguments})
    assert result["error"]["code"] == -32602


def test_fixture_only_exposes_the_read_only_synthetic_tool():
    tools = call("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == [fixture.TOOL_NAME]
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[0]["annotations"]["destructiveHint"] is False
    assert call("tools/call", {"name": "download", "arguments": {"size": 1024}})["error"]
    assert call("resources/read", {"uri": "file:///etc/passwd"})["error"]["code"] == -32601


def test_fixture_negotiates_protocol_and_ignores_notifications():
    assert call("initialize", {"protocolVersion": "2025-03-26"})["result"]["protocolVersion"] == "2025-03-26"
    assert call("initialize", {"protocolVersion": "unknown"})["result"]["protocolVersion"] in fixture.PROTOCOLS
    assert fixture.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert call("ping")["result"] == {}
    assert fixture.handle([])["error"]["code"] == -32600
