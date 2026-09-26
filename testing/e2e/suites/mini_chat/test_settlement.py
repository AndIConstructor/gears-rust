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


def usage_events(rid: str) -> list[dict]:
    """Usage outbox payloads of the turn with `rid`."""
    rows = query_db(
        "SELECT payload FROM toolkit_outbox_body WHERE payload LIKE ?", (f"%{rid}%",),
    )
    return [
        p for p in (json.loads(r["payload"]) for r in rows)
        if p.get("request_id") == rid and "settlement_method" in p
    ]


def estimated_charge(rid: str) -> int:
    """Credits of the estimated settlement of an azure-gpt-4.1 turn (DESIGN §5.8):
    (reserve_tokens - max_output_tokens_applied) input tokens plus
    minimal_generation_floor_applied output tokens, at 3 and 15 credits_micro
    per token (base.yaml multipliers)."""
    rows = query_db(
        "SELECT reserve_tokens, max_output_tokens_applied, minimal_generation_floor_applied "
        "FROM chat_turns WHERE request_id = ?", (rid,),
    )
    assert len(rows) == 1, rows
    turn = rows[0]
    estimated_input = turn["reserve_tokens"] - turn["max_output_tokens_applied"]
    return estimated_input * 3 + turn["minimal_generation_floor_applied"] * 15


def assert_estimated_settlement(rid: str, used_before: int, billing_outcome: str) -> None:
    """The turn is settled on the estimate, never released (no-free-cancel rule):
    one usage event with `billing_outcome`, `settlement_method: estimated` and
    the estimated charge, which is added to the total daily usage."""
    assert_no_reserves(USER_A_ID)
    expected = estimated_charge(rid)
    events = usage_events(rid)
    assert len(events) == 1, events
    event = events[0]
    assert (event["billing_outcome"], event["settlement_method"]) == (
        billing_outcome, "estimated",
    ), event
    assert event["actual_credits_micro"] == expected, event
    assert total_daily_used() - used_before == expected


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("requires mock provider (offline mode)")


class TestSettlement:
    """Quota settlement after various turn outcomes."""

    def test_completed_turn_releases_reserve(self, provider_chat):
        """A completed turn leaves no reserve behind and its turn is done."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(provider_chat["id"], "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)

        assert poll_turn(provider_chat["id"], rid)["state"] == "done"
        assert_no_reserves(USER_A_ID)

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

    def test_web_search_surcharge_in_reserve(self, request, chat):
        """14-12: the same message with web search reserves more tokens and
        credits than without it (web_search_surcharge_tokens).

        The web-search turn runs first: the second turn also reserves the first
        turn's tokens as prior context, which only narrows the difference.
        """
        _require_offline(request)
        chat_id = chat["id"]  # web_search-capable default model
        content = "SEARCH: weather"

        rid_ws = str(uuid.uuid4())
        status, events, raw = stream_message(
            chat_id, content, request_id=rid_ws, web_search={"enabled": True},
        )
        assert status == 200, raw
        expect_done(events)
        poll_turn(chat_id, rid_ws, ("done",))

        rid_plain = str(uuid.uuid4())
        status, events, raw = stream_message(chat_id, content, request_id=rid_plain)
        assert status == 200, raw
        expect_done(events)
        poll_turn(chat_id, rid_plain, ("done",))

        def reserve(rid: str) -> dict:
            rows = query_db(
                "SELECT reserve_tokens, reserved_credits_micro FROM chat_turns "
                "WHERE request_id = ?", (rid,),
            )
            assert len(rows) == 1, rows
            return rows[0]

        ws, plain = reserve(rid_ws), reserve(rid_plain)
        assert ws["reserve_tokens"] > plain["reserve_tokens"], (ws, plain)
        assert ws["reserved_credits_micro"] > plain["reserved_credits_micro"], (ws, plain)

    @pytest.mark.timeout(30)
    def test_cancelled_with_content(self, request, chat, mock_provider):
        """Disconnect after some deltas (the provider reported no usage): the
        turn is cancelled, the reserve released and the estimate charged
        (billing outcome `aborted`)."""
        _require_offline(request)
        rid = str(uuid.uuid4())
        used_before = total_daily_used()
        mock_provider.set_next_scenario(slow_scenario(20, slow=0.3))

        with open_stream(chat["id"], "Write a long essay.", request_id=rid) as s:
            seen = []
            s.read_until(lambda e: e.event == "delta" and (seen.append(e) or len(seen) == 3))

        assert poll_turn(chat["id"], rid)["state"] == "cancelled"
        assert_estimated_settlement(rid, used_before, "aborted")

    @pytest.mark.timeout(30)
    def test_cancelled_without_content(self, request, chat, mock_provider):
        """Disconnect before any delta: the turn is cancelled, the reserve
        released and the estimate charged (billing outcome `aborted`)."""
        _require_offline(request)
        rid = str(uuid.uuid4())
        used_before = total_daily_used()
        scenario = slow_scenario(3, slow=0.5)
        scenario.initial_delay = 3.0
        mock_provider.set_next_scenario(scenario)

        with open_stream(chat["id"], "Write slowly.", request_id=rid) as s:
            s.read_until_started()

        assert poll_turn(chat["id"], rid)["state"] == "cancelled"
        assert_estimated_settlement(rid, used_before, "aborted")

    def test_provider_http_error_releases_reserve(self, request, chat, mock_provider):
        """A provider HTTP 500 ends the stream with `error` and fails the turn;
        the reserve is released and the estimate charged (billing outcome
        `failed`: the provider call had started)."""
        _require_offline(request)
        used_before = total_daily_used()
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
        assert_estimated_settlement(rid, used_before, "failed")

    def test_one_usage_outbox_event_per_turn(self, chat):
        """A completed turn enqueues exactly one usage event, even after replays."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say hello.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], rid, ("done",))
        stream_message(chat["id"], "Say hello.", request_id=rid)

        events = usage_events(rid)
        assert len(events) == 1, events
        event = events[0]
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
