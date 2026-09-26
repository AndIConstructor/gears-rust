"""Tests for multi-turn conversation and message history."""

import httpx

from .conftest import (
    API_PREFIX,
    delta_text,
    expect_done,
    expect_stream_started,
    list_messages,
    parse_sse,
    stream_message,
)

import pytest


@pytest.mark.multi_provider
class TestMultiTurn:
    """Multiple messages in the same chat."""

    @pytest.mark.online_only
    def test_two_turns_in_sequence(self, provider_chat):
        chat_id = provider_chat["id"]

        # Turn 1
        _resp1 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Remember the number 42."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        s1 = _resp1.status_code
        ev1 = parse_sse(_resp1.text) if s1 == 200 else []
        assert s1 == 200
        assert any(e.event == "done" for e in ev1)
        ss1 = expect_stream_started(ev1)
        assert "request_id" in ss1.data
        assert "message_id" in ss1.data
        assert ss1.data.get("is_new_turn") is True

        # Turn 2 — model must recall from context
        _resp2 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What number did I ask you to remember?"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        s2 = _resp2.status_code
        ev2 = parse_sse(_resp2.text) if s2 == 200 else []
        assert s2 == 200
        assert any(e.event == "done" for e in ev2)
        ss2 = expect_stream_started(ev2)
        assert "request_id" in ss2.data
        assert "message_id" in ss2.data
        assert ss2.data.get("is_new_turn") is True

        # Verify the model actually recalled the number (proves context assembly works)
        text2 = "".join(e.data["content"] for e in ev2 if e.event == "delta")
        assert "42" in text2, (
            f"Model should recall '42' from conversation history. Got: {text2!r}"
        )

        # Check message history has 4 messages (2 user + 2 assistant)
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        assert len(msgs) == 4
        roles = [m["role"] for m in msgs]
        assert roles == ["user", "assistant", "user", "assistant"]

        first_msg = msgs[0]
        assert first_msg.get("request_id") is not None, "request_id must be non-null"
        assert isinstance(first_msg.get("attachments"), list), "attachments must be an array"

    def test_message_count_increments(self, provider_chat):
        """`message_count` is 0 for a new chat and grows by 2 (user + assistant) per turn."""
        chat_id = provider_chat["id"]
        assert provider_chat["message_count"] == 0

        for turn, content in enumerate(("Hello.", "Hello again."), start=1):
            status, events, raw = stream_message(chat_id, content)
            assert status == 200, raw
            expect_done(events)

            resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}")
            assert resp.status_code == 200
            assert resp.json()["message_count"] == 2 * turn

    def test_messages_ordered_chronologically(self, provider_chat):
        """03-12: two completed turns are listed oldest first: the first
        question, its answer, the second question, its answer."""
        chat_id = provider_chat["id"]
        answers = []
        request_ids = []
        for content in ("First message.", "Second message."):
            status, events, raw = stream_message(chat_id, content)
            assert status == 200, raw
            expect_done(events)
            request_ids.append(expect_stream_started(events).data["request_id"])
            answers.append(delta_text(events))

        msgs = list_messages(chat_id)
        assert [(m["role"], m["request_id"], m["content"]) for m in msgs] == [
            ("user", request_ids[0], "First message."),
            ("assistant", request_ids[0], answers[0]),
            ("user", request_ids[1], "Second message."),
            ("assistant", request_ids[1], answers[1]),
        ]
        timestamps = [m["created_at"] for m in msgs]
        assert timestamps == sorted(timestamps)
