"""Tests for quota settlement — reserve release, actual settlement, cancel settlement.

Settlement internals are not visible over HTTP. These tests check the effects:
turn state, quota usage, and that `quota_usage.reserved_credits_micro` of the
user is back to 0 once the turn is terminal (read directly from the DB).
"""

import json
import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    TENANT_A_ID,
    USER_A_ID,
    assert_no_reserves,
    expect_done,
    find_period,
    get_quota_status,
    open_stream,
    parse_sse,
    poll_turn,
    query_db,
    slow_scenario,
    stream_message,
)
from .mock_provider.responses import Scenario


def total_daily_used() -> int:
    return find_period(get_quota_status(), "total", "daily")["used_credits_micro"]


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("requires mock provider (offline mode)")


class TestSettlement:
    """Quota settlement after various turn outcomes."""

    def test_completed_turn_releases_reserve(self, chat):
        """A completed turn leaves no reserve behind and its turn is done."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)

        assert poll_turn(chat["id"], rid)["state"] == "done"
        assert_no_reserves(USER_A_ID)

    def test_completed_turn_charged_once(self, request, chat):
        """One completed turn increases the total daily usage by exactly its cost.

        Mock usage for a first message: input = max(50, 50 * 1 input item) = 50,
        output = 12 (default scenario). azure-gpt-4.1 multipliers (base.yaml):
        input 3_000_000, output 15_000_000 micro per 1M tokens:
            ceil(50 * 3_000_000 / 1e6) + ceil(12 * 15_000_000 / 1e6) = 150 + 180 = 330.
        """
        _require_offline(request)
        rid = str(uuid.uuid4())
        used_before = total_daily_used()

        status, events, _ = stream_message(chat["id"], "Say OK.", request_id=rid)
        assert status == 200
        done = expect_done(events)
        assert done.data["usage"] == {"input_tokens": 50, "output_tokens": 12}
        poll_turn(chat["id"], rid, ("done",))
        assert_no_reserves(USER_A_ID)

        assert total_daily_used() - used_before == 330

    def test_reservation_snapshot_persisted(self, chat):
        """The turn row keeps the reservation snapshot taken at preflight."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], rid, ("done",))

        rows = query_db(
            "SELECT reserve_tokens, reserved_credits_micro FROM chat_turns WHERE request_id = ?",
            (rid,),
        )
        assert len(rows) == 1, f"No turn row for request_id={rid}"
        assert rows[0]["reserve_tokens"] > 0
        assert rows[0]["reserved_credits_micro"] > 0
        assert_no_reserves(USER_A_ID)

    @pytest.mark.timeout(30)
    def test_cancelled_with_usage(self, request, chat, mock_provider):
        """Disconnect after some deltas: the turn is cancelled and the reserve released."""
        _require_offline(request)
        rid = str(uuid.uuid4())
        mock_provider.set_next_scenario(slow_scenario(20, slow=0.3))

        with open_stream(chat["id"], "Write a long essay.", request_id=rid) as s:
            seen = []
            s.read_until(lambda e: e.event == "delta" and (seen.append(e) or len(seen) == 3))

        assert poll_turn(chat["id"], rid)["state"] == "cancelled"
        assert_no_reserves(USER_A_ID)

    @pytest.mark.timeout(30)
    def test_cancelled_without_usage(self, request, chat, mock_provider):
        """Disconnect before any delta: the turn is cancelled and the reserve released."""
        _require_offline(request)
        rid = str(uuid.uuid4())
        scenario = slow_scenario(3, slow=0.5)
        scenario.initial_delay = 3.0
        mock_provider.set_next_scenario(scenario)

        with open_stream(chat["id"], "Write slowly.", request_id=rid) as s:
            s.read_until_started()

        assert poll_turn(chat["id"], rid)["state"] == "cancelled"
        assert_no_reserves(USER_A_ID)

    def test_provider_http_error_releases_reserve(self, request, chat, mock_provider):
        """A provider HTTP 500 ends the stream with `error`, fails the turn, releases the reserve."""
        _require_offline(request)
        mock_provider.set_next_scenario(Scenario(
            http_error_status=500,
            http_error_body={"error": {"message": "Internal server error", "type": "server_error"}},
        ))
        rid = str(uuid.uuid4())
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat['id']}/messages:stream",
            json={"content": "This should fail.", "request_id": rid},
            headers={"Accept": "text/event-stream"},
            timeout=30,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert [e.event for e in events] == ["stream_started", "error"]

        assert poll_turn(chat["id"], rid)["state"] == "error"
        assert_no_reserves(USER_A_ID)

    def test_replay_does_not_charge_again(self, chat):
        """Replaying a completed turn (3 times) leaves quota usage unchanged."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say hello.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], rid, ("done",))
        assert_no_reserves(USER_A_ID)
        used_before = total_daily_used()

        for _ in range(3):
            status, events, _ = stream_message(chat["id"], "Say hello.", request_id=rid)
            assert status == 200
            expect_done(events)

        assert total_daily_used() == used_before

    def test_one_usage_outbox_event_per_turn(self, chat):
        """A completed turn enqueues exactly one usage event, even after replays."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say hello.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], rid, ("done",))
        stream_message(chat["id"], "Say hello.", request_id=rid)

        rows = query_db(
            "SELECT payload FROM toolkit_outbox_body WHERE payload LIKE ?", (f"%{rid}%",),
        )
        usage_events = [
            p for p in (json.loads(r["payload"]) for r in rows)
            if p.get("request_id") == rid and "settlement_method" in p
        ]
        assert len(usage_events) == 1, usage_events
        event = usage_events[0]
        assert event["terminal_state"] == "completed"
        # dedupe_key = {tenant_id}/{turn_id}/{request_id} (UUIDs in simple form).
        expected = "/".join(uuid.UUID(v).hex for v in (TENANT_A_ID, event["turn_id"], rid))
        assert event["dedupe_key"] == expected


@pytest.mark.multi_provider
@pytest.mark.online_only
class TestSettlementPerProvider:
    """Settlement with real provider token counts."""

    def test_completed_settlement_per_provider(self, provider_chat):
        status, events, _ = stream_message(provider_chat["id"], "Say hello in exactly three words.")
        assert status == 200
        done = expect_done(events)
        usage = done.data["usage"]
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0
        assert_no_reserves(USER_A_ID)
