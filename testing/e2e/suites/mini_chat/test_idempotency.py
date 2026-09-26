# Updated: 2026-04-16 by Constructor Tech
"""Tests for request_id idempotency — conflict detection, replay priority, quota invariance."""

import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    assert_problem,
    delta_text,
    expect_done,
    expect_stream_started,
    find_period,
    get_quota_status,
    open_stream,
    parse_sse,
    poll_turn,
    query_db,
    slow_scenario,
    stream_message,
)
from .mock_provider.responses import MockEvent, Scenario


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stream_url(chat_id: str) -> str:
    return f"{API_PREFIX}/chats/{chat_id}/messages:stream"


def post_stream(chat_id: str, body: dict) -> httpx.Response:
    return httpx.post(
        stream_url(chat_id), json=body,
        headers={"Accept": "text/event-stream"}, timeout=30,
    )


def turn_count(chat_id: str) -> int:
    return query_db(
        "SELECT COUNT(*) AS n FROM chat_turns WHERE chat_id = ?", (chat_id,),
    )[0]["n"]


def total_daily_used() -> int:
    return find_period(get_quota_status(), "total", "daily")["used_credits_micro"]


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("requires mock provider (offline mode)")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Request-id idempotency conflict detection and replay semantics."""

    @pytest.mark.timeout(30)
    def test_running_turn_same_request_id_409(self, request, chat, mock_provider):
        """Resending the request_id of a running turn is 409 request_id_conflict."""
        _require_offline(request)
        chat_id = chat["id"]
        body = {"content": "Hello slow.", "request_id": str(uuid.uuid4())}
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        with open_stream(chat_id, body["content"], request_id=body["request_id"]) as first:
            first.read_until_started()
            assert_problem(post_stream(chat_id, body), 409, "aborted", reason="request_id_conflict")
            expect_done(first.drain())
        assert turn_count(chat_id) == 1

    def test_failed_turn_same_request_id_409(self, request, chat, mock_provider):
        """Resending the request_id of a failed turn is 409 request_id_conflict."""
        _require_offline(request)
        chat_id = chat["id"]
        body = {"content": "Fail please.", "request_id": str(uuid.uuid4())}
        mock_provider.set_next_scenario(Scenario(
            terminal="failed",
            error={"code": "server_error", "message": "fail"},
            events=[MockEvent("response.output_text.delta", {"delta": "x"})],
        ))
        assert post_stream(chat_id, body).status_code == 200
        assert poll_turn(chat_id, body["request_id"])["state"] == "error"

        assert_problem(post_stream(chat_id, body), 409, "aborted", reason="request_id_conflict")
        assert turn_count(chat_id) == 1

    @pytest.mark.timeout(30)
    def test_cancelled_turn_same_request_id_409(self, request, chat, mock_provider):
        """Resending the request_id of a cancelled turn is 409 request_id_conflict."""
        _require_offline(request)
        chat_id = chat["id"]
        body = {"content": "Write slowly.", "request_id": str(uuid.uuid4())}
        mock_provider.set_next_scenario(slow_scenario(20, slow=0.3))

        with open_stream(chat_id, body["content"], request_id=body["request_id"]) as s:
            s.read_until(lambda e: e.event == "delta")
        assert poll_turn(chat_id, body["request_id"])["state"] == "cancelled"

        assert_problem(post_stream(chat_id, body), 409, "aborted", reason="request_id_conflict")
        assert turn_count(chat_id) == 1

    def test_request_id_replaced_by_retry_409(self, chat):
        """The request_id of a turn replaced by retry is not replayed: 409 request_id_conflict."""
        chat_id = chat["id"]
        body = {"content": "Replace me.", "request_id": str(uuid.uuid4())}
        status, events, _ = stream_message(chat_id, body["content"], request_id=body["request_id"])
        assert status == 200
        expect_done(events)
        poll_turn(chat_id, body["request_id"], ("done",))

        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/turns/{body['request_id']}/retry",
            headers={"Accept": "text/event-stream"}, timeout=30,
        )
        assert resp.status_code == 200, resp.text
        expect_done(parse_sse(resp.text))

        assert_problem(post_stream(chat_id, body), 409, "aborted", reason="request_id_conflict")
        assert turn_count(chat_id) == 2

    @pytest.mark.timeout(30)
    def test_replay_priority_over_parallel_check(self, request, chat, mock_provider):
        """Replay of a completed turn returns 200 even while another turn is running."""
        _require_offline(request)
        chat_id = chat["id"]
        rid_a = str(uuid.uuid4())
        status_a, events_a, _ = stream_message(chat_id, "Turn A.", request_id=rid_a)
        assert status_a == 200
        expect_done(events_a)

        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))
        with open_stream(chat_id, "Turn B slow.", request_id=str(uuid.uuid4())) as b:
            b.read_until_started()

            replay = post_stream(chat_id, {"content": "Turn A.", "request_id": rid_a})
            assert replay.status_code == 200, replay.text
            replay_events = parse_sse(replay.text)
            assert expect_stream_started(replay_events).data["is_new_turn"] is False
            assert delta_text(replay_events) == delta_text(events_a)
            expect_done(b.drain())

    def test_replay_does_not_modify_quota_or_call_provider(self, chat, mock_provider):
        """Replaying a completed turn changes neither quota nor provider traffic."""
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat_id, "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat_id, rid, ("done",))

        used_before = total_daily_used()
        mock_provider.clear_captured_requests()
        for _ in range(3):
            resp = post_stream(chat_id, {"content": "Say OK.", "request_id": rid})
            assert resp.status_code == 200
            assert delta_text(parse_sse(resp.text)) == delta_text(events)

        assert total_daily_used() == used_before
        assert mock_provider.get_captured_requests() == []
        assert turn_count(chat_id) == 1
