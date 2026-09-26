"""Tests for the streaming message endpoint (POST /v1/chats/{id}/messages:stream).

These tests hit a real LLM provider — they require valid API keys in .provider-keys
and a running server (started automatically or via run-server.sh --bg).
"""

import uuid

import pytest
import httpx

from .conftest import (
    API_PREFIX,
    assert_problem,
    expect_done,
    expect_stream_started,
    parse_sse,
    slow_scenario,
)

# A request body that fails JSON deserialization (missing required field,
# wrong type) is rejected by axum's `Json` extractor before the handler runs.



@pytest.mark.multi_provider
class TestStreamBasic:
    """Basic streaming happy path."""

    def test_stream_returns_200_sse(self, provider_chat):
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Say hello in one word."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert len(events) > 0
        assert events[0].event == "stream_started"
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True

    def test_stream_has_terminal_done(self, provider_chat):
        """Stream must end with exactly one 'done' event."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Say hi."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True
        terminal = [e for e in events if e.event in ("done", "error")]
        assert len(terminal) == 1
        assert terminal[0].event == "done"

    def test_stream_has_delta_events(self, provider_chat):
        """Stream should contain at least one delta with text content."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Tell me a one-line joke."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True
        deltas = [e for e in events if e.event == "delta"]
        assert len(deltas) > 0
        for d in deltas:
            assert d.data["type"] == "text"
            assert isinstance(d.data["content"], str)

    def test_stream_assembled_text_nonempty(self, provider_chat):
        """Concatenated delta content should form a non-empty response."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "What is 2+2? Answer in one word."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True
        text = "".join(
            e.data["content"] for e in events if e.event == "delta"
        )
        assert len(text.strip()) > 0


@pytest.mark.multi_provider
class TestStreamDoneEvent:
    """Validate the 'done' event fields per DESIGN.md."""

    def test_done_event_contract(self, provider_chat):
        """`done` carries models, quota decision and usage; no message_id, no internal token fields."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Say OK."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        d = expect_done(parse_sse(resp.text)).data
        assert d["effective_model"] == provider_chat["model"]
        assert d["selected_model"] == provider_chat["model"]
        assert d["quota_decision"] == "allow"
        assert "message_id" not in d, "message_id belongs to stream_started"
        usage = d["usage"]
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        # Token breakdown fields are internal-only and not exposed in the SSE API.
        for internal in ("cache_read_input_tokens", "cache_write_input_tokens", "reasoning_tokens"):
            assert internal not in usage


@pytest.mark.multi_provider
class TestStreamEventOrdering:
    """SSE event ordering: ping* (delta|tool)* citations? (done|error)"""

    def test_no_events_after_terminal(self, provider_chat):
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Say hi."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        terminal_idx = None
        for i, e in enumerate(events):
            if e.event in ("done", "error"):
                terminal_idx = i
                break
        assert terminal_idx is not None
        # Nothing after terminal
        assert terminal_idx == len(events) - 1



class TestStreamPing:
    """`ping` keepalives are sent only while the stream waits for the first content."""

    @pytest.mark.timeout(30)
    def test_ping_only_before_content(self, request, chat, mock_provider):
        """The provider stays silent for 6 s (> sse_ping_interval_seconds=5 in base.yaml):
        at least one ping arrives, and none after the first delta."""
        if request.config.getoption("mode") == "online":
            pytest.skip("requires mock provider (delayed scenario)")
        scenario = slow_scenario(3, slow=0.3)
        scenario.initial_delay = 6.0
        mock_provider.set_next_scenario(scenario)

        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat['id']}/messages:stream",
            json={"content": "Think first."},
            headers={"Accept": "text/event-stream"},
            timeout=30,
        )
        assert resp.status_code == 200
        types = [e.event for e in parse_sse(resp.text)]
        first_delta = types.index("delta")
        assert types[0] == "stream_started"
        assert "ping" in types[1:first_delta], types
        assert "ping" not in types[first_delta:], types
        assert types[-1] == "done"


@pytest.mark.multi_provider
class TestStreamPreflightErrors:
    """Pre-stream errors should return JSON, not SSE."""

    def test_chat_not_found(self, server):
        resp = httpx.post(
            f"{API_PREFIX}/chats/{uuid.uuid4()}/messages:stream",
            json={"content": "hello"},
            headers={"Accept": "text/event-stream"},
            timeout=10,
        )
        assert_problem(resp, 404, "not_found")

    def test_empty_content_rejected(self, provider_chat):
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": ""},
            headers={"Accept": "text/event-stream"},
            timeout=10,
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="EMPTY_CONTENT")

    def test_missing_content_rejected(self, provider_chat):
        """A body that does not match the schema is 422 invalid_argument
        (platform JSON extractor; malformed JSON would be 400)."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={},
            headers={"Accept": "text/event-stream"},
            timeout=10,
        )
        assert_problem(resp, 422, "invalid_argument")

    def test_malformed_attachment_id_rejected(self, provider_chat):
        """04-04: an attachment id that is not a UUID fails body deserialization: 422."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Hello", "attachment_ids": ["not-a-uuid"]},
            headers={"Accept": "text/event-stream"},
            timeout=30,
        )
        assert_problem(resp, 422, "invalid_argument")

    def test_nonexistent_attachment_id_rejected(self, provider_chat, mock_provider):
        """04-05: an unknown attachment id is 400 invalid_attachment; the provider is not called."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Hello", "attachment_ids": [str(uuid.uuid4())]},
            headers={"Accept": "text/event-stream"},
            timeout=30,
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="invalid_attachment")
        assert mock_provider.get_last_request() is None


@pytest.mark.multi_provider
class TestMessages:
    """Verify messages are persisted after streaming."""

    def test_messages_persisted_after_stream(self, provider_chat):
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Say exactly: PONG"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert any(e.event == "done" for e in events)

        # Fetch messages
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles

        user_msg = next(m for m in msgs if m["role"] == "user")
        assert user_msg.get("request_id") is not None, "request_id must be non-null"
        assert isinstance(user_msg.get("attachments"), list), "attachments must be an array"

        asst_msg = next(m for m in msgs if m["role"] == "assistant")
        assert asst_msg.get("request_id") is not None, "request_id must be non-null"
        assert isinstance(asst_msg.get("attachments"), list), "attachments must be an array"

    def test_user_message_content_matches(self, provider_chat):
        prompt = "Say exactly: TEST_ECHO"
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": prompt},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assert any(prompt in m["content"] for m in user_msgs)

    def test_assistant_message_has_tokens(self, provider_chat):
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Say OK."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        asst = [m for m in msgs if m["role"] == "assistant"]
        assert len(asst) >= 1
        # Token counts should be populated
        assert asst[0].get("input_tokens", 0) > 0 and asst[0].get("output_tokens", 0) > 0
