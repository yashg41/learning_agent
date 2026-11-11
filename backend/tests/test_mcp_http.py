"""The learning tools over loopback HTTP instead of the SDK's stdin pipe.

An `sdk`-transport MCP server has no channel of its own: the CLI calls back
down the same stdin pipe the prompt went out on, and the SDK closes that pipe
60 seconds into a turn (CLAUDE_CODE_STREAM_CLOSE_TIMEOUT), after which every
tool call fails with "Stream closed". Measured on this project's own debug
logs: max age of a successful call 59.3s, min age of a failure 60.2s — 560
calls across 29 tools, zero overlap. In-turn retries never recovered: 314
attempted, 0 succeeded, because the close is one-way.

The SDK only applies that close to servers it holds instances for, so moving
to http sidesteps it entirely rather than postponing it.
"""

import json

import pytest

from backend.config import settings
from backend.mcp_http import (
    H_BUILD_CTX,
    H_EMAIL,
    H_ROLE,
    mcp_headers,
    mcp_url,
)


def test_http_config_escapes_the_sixty_second_close():
    """The whole point: an http server never enters sdk_mcp_servers.

    query.py closes stdin under `if self.sdk_mcp_servers or has_hooks`, and
    client.py fills that dict only for `type == "sdk"`. This asserts the
    condition our transport depends on, so a future change to the config shape
    fails here rather than silently restoring the 60s ceiling.
    """
    http_cfg = {"learning-tools": {"type": "http", "url": mcp_url()}}
    sdk_servers = {
        n: c for n, c in http_cfg.items()
        if isinstance(c, dict) and c.get("type") == "sdk"
    }
    assert sdk_servers == {}, "an http config must not register an SDK instance"


def test_the_url_stays_on_loopback():
    """These tools read and write learner data with no auth of their own, so
    the 127.0.0.1 bind IS the boundary. Never advertise them routably."""
    assert mcp_url().startswith("http://127.0.0.1:")
    assert str(settings.API_PORT) in mcp_url()


def test_identity_travels_with_the_request():
    """One endpoint serves every run — a demo build overlaps a live chat turn
    by design — so per-run identity cannot live in a closure any more."""
    h = mcp_headers("learner@example.com", "pymentor", "tutor", "sess-1")
    assert h[H_EMAIL] == "learner@example.com"
    assert h[H_ROLE] == "tutor"


def test_a_tutor_carries_no_build_context():
    """build_ctx's *presence* is what unlocks save_demo and derivation_write
    (allowed_tools is advisory under bypassPermissions). A tutor that shipped
    one would hand itself builder tools."""
    h = mcp_headers("learner@example.com", "pymentor", "tutor", "s")
    assert H_BUILD_CTX not in h


def test_a_builder_carries_only_the_keys_tools_read():
    """build_ctx is also a return channel — the builder writes demo_id back
    into it — and that half cannot cross a request boundary. Only the inbound
    keys are serialised, so nothing implies the dict is shared."""
    h = mcp_headers(
        "learner@example.com", "pymentor", "builder", "s",
        build_ctx={"scratch_dir": "/tmp/build", "on_event": object()},
    )
    sent = json.loads(h[H_BUILD_CTX])
    assert sent["scratch_dir"] == "/tmp/build"
    assert "on_event" not in sent, "only JSON-safe inbound keys may be sent"


def test_build_tool_list_matches_the_sdk_server():
    """Both transports must offer the same tools. build_tool_list shares
    create_learning_tools rather than re-deriving the family filtering, so this
    guards against the two drifting apart."""
    from backend.tools import build_tool_list, create_learning_tools

    class _Ep:
        def format_for_system_prompt(self, *a, **k):
            return ""

    listed = build_tool_list("/tmp/u", _Ep(), None, None, app="pymentor")
    server = create_learning_tools("/tmp/u", _Ep(), None, None, app="pymentor")

    assert listed, "the http transport must find tools to register"
    assert server.get("type") == "sdk"
    # Every tool carries what a transport needs to register it by hand.
    for t in listed:
        assert t.name and t.description and t.input_schema
