"""Tests for turn lifecycle — null assistant_message_id on content-less cancel/failure."""

import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    expect_done,
    list_messages,
    open_stream,
    poll_turn,
    slow_scenario,
    stream_message,
)
from .mock_provider.responses import Scenario


class TestTurnLifecycle:
    """Terminal turn states and assistant_message_id."""

    @pytest.fixture(autouse=True)
    def _offline_only(self, request):
        if request.config.getoption("mode") == "online":
            pytest.skip("requires mock provider (offline mode)")

    @pytest.mark.timeout(30)
    def test_cancelled_without_content_null_message_id(self, chat, mock_provider):
        """Disconnect before the first delta: cancelled, no assistant_message_id, no message."""
        chat_id = chat["id"]
        request_id = str(uuid.uuid4())
        scenario = slow_scenario(3, slow=0.5)
        scenario.initial_delay = 3.0  # provider silent for 3 s after the headers
        mock_provider.set_next_scenario(scenario)

        with open_stream(chat_id, "Write slowly.", request_id=request_id) as s:
            s.read_until_started()

        turn = poll_turn(chat_id, request_id)
        assert turn["state"] == "cancelled"
        assert turn.get("assistant_message_id") is None
        assert [m["role"] for m in list_messages(chat_id)] == ["user"]

    def test_failed_turn_null_message_id(self, chat, mock_provider):
        """A provider failure without content: error, no assistant_message_id, no message."""
        chat_id = chat["id"]
        request_id = str(uuid.uuid4())
        mock_provider.set_next_scenario(Scenario(
            terminal="failed",
            error={"code": "server_error", "message": "fail"},
            events=[],
        ))

        status, _, _ = stream_message(chat_id, "Fail now.", request_id=request_id)
        assert status == 200

        turn = poll_turn(chat_id, request_id)
        assert turn["state"] == "error"
        assert turn.get("assistant_message_id") is None
        assert [m["role"] for m in list_messages(chat_id)] == ["user"]

    def test_done_turn_state_stable_after_completion(self, chat):
        """Once GET turn reports `done`, later reads keep reporting `done` with the same message."""
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat_id, "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)

        first = poll_turn(chat_id, rid)
        assert first["state"] == "done"
        for _ in range(5):
            resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/turns/{rid}", timeout=5)
            assert resp.status_code == 200
            assert resp.json()["state"] == "done"
            assert resp.json()["assistant_message_id"] == first["assistant_message_id"]
