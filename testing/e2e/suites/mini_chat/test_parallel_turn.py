"""Tests for parallel turn rejection — only one generation at a time per chat."""

import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    DETAIL_TURN_ALREADY_RUNNING,
    assert_problem,
    expect_done,
    list_messages,
    open_stream,
    parse_sse,
    slow_scenario,
    stream_message,
)


class TestParallelTurn:
    """Only one active generation per chat at a time."""

    @pytest.mark.timeout(30)
    def test_second_stream_409_turn_already_running(self, request, chat, mock_provider):
        """A second stream into a chat with a running turn gets 409 turn_already_running."""
        if request.config.getoption("mode") == "online":
            pytest.skip("requires mock provider (slow scenario)")
        chat_id = chat["id"]
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        with open_stream(chat_id, "First turn.", request_id=str(uuid.uuid4())) as first:
            first.read_until_started()

            second = httpx.post(
                f"{API_PREFIX}/chats/{chat_id}/messages:stream",
                json={"content": "Second turn.", "request_id": str(uuid.uuid4())},
                headers={"Accept": "text/event-stream"},
                timeout=30,
            )
            assert_problem(
                second, 409, "aborted",
                reason="turn_already_running", detail=DETAIL_TURN_ALREADY_RUNNING,
            )

            expect_done(first.drain())

        # Only the first turn was persisted: one user + one assistant message.
        messages = list_messages(chat_id)
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[0]["content"] == "First turn."

    def test_new_stream_succeeds_after_terminal(self, chat):
        """A new stream request succeeds after the previous turn completed."""
        chat_id = chat["id"]

        # Complete first turn (normal speed)
        status1, events1, _ = stream_message(
            chat_id, "First turn.", request_id=str(uuid.uuid4())
        )
        assert status1 == 200
        expect_done(events1)

        # Immediately send another turn
        url = f"{API_PREFIX}/chats/{chat_id}/messages:stream"
        resp2 = httpx.post(
            url,
            json={"content": "Second turn.", "request_id": str(uuid.uuid4())},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        raw2 = resp2.text
        events2 = parse_sse(raw2) if resp2.status_code == 200 else []
        assert resp2.status_code == 200
        expect_done(events2)
