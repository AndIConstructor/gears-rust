"""Tests for quota settlement — reserve release, actual settlement, cancel settlement.

Settlement internals are not visible over HTTP. These tests check the effects:
turn state, quota usage, and that `quota_usage.reserved_credits_micro` of the
user is back to 0 once the turn is terminal (read directly from the DB).
"""

import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    TENANT_A_ID,
    USER_A_ID,
    assert_no_reserves,
    expect_done,
    expect_stream_started,
    find_period,
    get_quota_status,
    open_stream,
    outbox_payloads,
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
    return [
        p for p in outbox_payloads(rid)
        if p.get("request_id") == rid and "settlement_method" in p
    ]


# ── Preflight estimate of a first message in a fresh azure-gpt-4.1 chat ──
#
# Estimation budgets come from the model's catalog entry. azure-gpt-4.1
# (base.yaml): bytes_per_token_conservative 4, fixed_overhead_tokens 500,
# safety_margin_pct 10, max_output_tokens 8192, 3 credits_micro per input token,
# 15 per output token. The generation floor is gear configuration
# (config.rs default minimal_generation_floor 50; the catalog value is not
# used), and the streaming cap is the default 32768.
# A first message has no prior context and no tools, so for `n` content bytes:
#   estimated input tokens = ceil((ceil(n / 4) + 500) * 110 / 100)
#   max_output_tokens_applied = min(8192, 32768) = 8192
#   reserve_tokens = estimated input tokens + 8192
#   reserved_credits_micro = estimated input * 3 + 8192 * 15
# The estimated settlement (DESIGN §5.8) charges the estimated input tokens
# plus the 50-token generation floor: estimated input * 3 + 50 * 15.
MAX_OUTPUT_TOKENS_APPLIED = 8192
MINIMAL_GENERATION_FLOOR = 50

# "Write a long essay." / "This should fail.": 19 / 17 bytes -> ceil(n/4) = 5
#   -> (5 + 500) * 1.1 = 555.5 -> 556 input tokens; charge 556 * 3 + 750 = 2418
# "Write slowly.": 13 bytes -> 4 -> (4 + 500) * 1.1 = 554.4 -> 555; 1665 + 750 = 2415
ESTIMATED_CHARGE = {
    "Write a long essay.": 2418,
    "Write slowly.": 2415,
    "This should fail.": 2418,
}


def assert_estimated_settlement(
    rid: str, used_before: int, billing_outcome: str, content: str,
) -> None:
    """The turn is settled on the estimate, never released (no-free-cancel rule):
    one usage event with `billing_outcome`, `settlement_method: estimated` and
    the estimated charge of `content` (ESTIMATED_CHARGE), which is added to
    the total daily usage."""
    assert_no_reserves(USER_A_ID)
    expected = ESTIMATED_CHARGE[content]
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

    @pytest.mark.multi_provider
    def test_completed_turn_releases_reserve(self, provider_chat):
        """A completed turn leaves no reserve behind and its turn is done."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(provider_chat["id"], "Say OK.", request_id=rid)
        assert status == 200
        expect_done(events)

        assert poll_turn(provider_chat["id"], rid)["state"] == "done"
        assert_no_reserves(USER_A_ID)

    @pytest.mark.timeout(30)
    def test_reservation_snapshot_persisted(self, request, chat, mock_provider):
        """09-04, 14-01: the preflight reservation snapshot is on the turn row
        while the turn is still running, and completion leaves it unchanged.

        "Say OK." is 7 bytes: ceil(7 / 4) = 2 -> (2 + 500) * 1.1 = 552.2 -> 553
        estimated input tokens (see the estimate notes above), so
        reserve_tokens = 553 + 8192 = 8745 and
        reserved_credits_micro = 553 * 3 + 8192 * 15 = 124539."""
        _require_offline(request)
        chat_id = chat["id"]  # azure-gpt-4.1, no prior context
        rid = str(uuid.uuid4())
        expected = {
            "state": "running",
            "reserve_tokens": 8745,
            "max_output_tokens_applied": MAX_OUTPUT_TOKENS_APPLIED,
            "reserved_credits_micro": 124_539,
            "minimal_generation_floor_applied": MINIMAL_GENERATION_FLOOR,
            "effective_model": "azure-gpt-4.1",
        }
        columns = ", ".join(expected)

        def snapshot() -> dict:
            rows = query_db(f"SELECT {columns} FROM chat_turns WHERE request_id = ?", (rid,))
            assert len(rows) == 1, rows
            return rows[0]

        mock_provider.set_next_scenario(slow_scenario(5, slow=0.3))
        with open_stream(chat_id, "Say OK.", request_id=rid) as s:
            s.read_until_started()
            assert snapshot() == expected
            expect_done(s.drain())

        assert poll_turn(chat_id, rid, ("done",))["state"] == "done"
        assert snapshot() == {**expected, "state": "completed"}
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
        assert_estimated_settlement(rid, used_before, "aborted", "Write a long essay.")

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
        assert_estimated_settlement(rid, used_before, "aborted", "Write slowly.")

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
        assert_estimated_settlement(rid, used_before, "failed", "This should fail.")

    def test_incomplete_response_is_done_and_settled_on_actual_usage(self, request, chat):
        """A provider `response.incomplete` (mock `TRUNCATE`, reason
        max_output_tokens) ends in `done`, not `error`: the turn is completed
        with no error code, the truncated text is persisted, and it is settled
        on the actual usage of `response.usage` (DESIGN, response.incomplete)."""
        _require_offline(request)
        rid = str(uuid.uuid4())
        used_before = total_daily_used()
        status, events, raw = stream_message(chat["id"], "TRUNCATE", request_id=rid)
        assert status == 200, raw
        usage = expect_done(events).data["usage"]
        assert usage["output_tokens"] == 100, usage

        assert poll_turn(chat["id"], rid)["state"] == "done"
        rows = query_db(
            "SELECT t.state, t.error_code, m.content FROM chat_turns t "
            "JOIN messages m ON m.id = t.assistant_message_id WHERE t.request_id = ?",
            (rid,),
        )
        assert rows == [{"state": "completed", "error_code": None, "content": "Truncated text"}], rows

        assert_no_reserves(USER_A_ID)
        # azure-gpt-4.1 (base.yaml): 3 and 15 credits_micro per token.
        cost = usage["input_tokens"] * 3 + usage["output_tokens"] * 15
        (event,) = usage_events(rid)
        assert (
            event["terminal_state"], event["billing_outcome"], event["settlement_method"],
        ) == ("completed", "completed", "actual"), event
        assert event["actual_credits_micro"] == cost, event
        assert total_daily_used() - used_before == cost

    def test_one_usage_outbox_event_per_turn(self, chat):
        """A completed turn enqueues exactly one usage event, even after a
        replay (the replay itself succeeds: `done`, `is_new_turn` false)."""
        rid = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say hello.", request_id=rid)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], rid, ("done",))

        status, replay, raw = stream_message(chat["id"], "Say hello.", request_id=rid)
        assert status == 200, raw
        assert expect_stream_started(replay).data["is_new_turn"] is False
        expect_done(replay)

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
