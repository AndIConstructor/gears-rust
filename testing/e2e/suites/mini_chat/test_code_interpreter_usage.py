"""Code interpreter usage verification tests.

Exercises code interpreter (XLSX upload) with the mock provider, then checks
quota usage via the REST quota endpoint (literal expected credits, see
EXPECTED_CREDITS), message tokens via the messages API, and the
code_interpreter_calls counter and reserves in the DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import httpx

from .conftest import (
    API_PREFIX, PROVIDER_DEFAULT_MODEL, USER_A_ID,
    assert_no_reserves, expect_done, find_period, get_quota_status, query_db, stream_message,
)
from .test_attachments import _upload_ready
from .test_code_interpreter import XLSX_CONTENT_TYPE, _make_minimal_xlsx


# ── DB helpers (quota_usage tool counters are not exposed via REST) ─────

def _query_ci_calls(user_id: str = USER_A_ID) -> int:
    """code_interpreter_calls of today's daily `total` quota_usage row."""
    rows = query_db(
        "SELECT code_interpreter_calls FROM quota_usage "
        "WHERE user_id = ? AND period_type = 'daily' "
        "AND period_start = ? AND bucket = 'total'",
        (user_id, datetime.now(timezone.utc).date().isoformat()),
    )
    return rows[0]["code_interpreter_calls"] if rows else 0


# ── Expected charges ─────────────────────────────────────────────────────
#
# Mock "CODEINTERP:*" scenario: usage input = max(300, 50 * input items) and
# output = 20. A first message sends one input item, so input = 300.
# credits_micro = ceil(input * in_mult / 1e6) + ceil(output * out_mult / 1e6)
# with the base.yaml multipliers:
#   gpt-5.2        (openai): 300 * 1.0 + 20 * 3.0  = 300 +  60 =  360
#   azure-gpt-4.1  (azure):  300 * 3.0 + 20 * 15.0 = 900 + 300 = 1200
EXPECTED_USAGE = {"input_tokens": 300, "output_tokens": 20}
EXPECTED_CREDITS = {"openai": 360, "azure": 1200}


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def xlsx_chat(provider):
    """Create a chat and upload a ready XLSX attachment."""
    model = PROVIDER_DEFAULT_MODEL[provider]
    resp = httpx.post(f"{API_PREFIX}/chats", json={"model": model})
    assert resp.status_code == 201
    chat = resp.json()
    chat_id = chat["id"]

    att_id = _upload_ready(chat_id, "data.xlsx", _make_minimal_xlsx(), XLSX_CONTENT_TYPE)

    return {"chat_id": chat_id, "att_id": att_id, "model": model}


@pytest.mark.multi_provider
@pytest.mark.usefixtures("offline_only")
class TestCodeInterpreterUsageAccounting:
    """Verify that code interpreter turns produce correct quota and message records.

    Offline only: the literal credit amounts depend on the mock's fixed usage."""

    @pytest.mark.timeout(20)
    def test_code_interpreter_usage_correct(self, provider, server, xlsx_chat):
        """Single CI turn: verify credits, messages, tool events, and turn state."""
        chat_id = xlsx_chat["chat_id"]
        att_id = xlsx_chat["att_id"]

        # Snapshot quota before
        spent_before = find_period(get_quota_status(), "total", "daily")["used_credits_micro"]

        rid = str(uuid.uuid4())
        status, events, _ = stream_message(
            chat_id,
            "CODEINTERP: analyze the spreadsheet data",
            attachment_ids=[att_id],
            request_id=rid,
        )
        assert status == 200
        done = expect_done(events)

        sse_usage = done.data["usage"]
        sse_input = sse_usage["input_tokens"]
        sse_output = sse_usage["output_tokens"]

        # Verify code_interpreter tool events
        tool_events = [e for e in events if e.event == "tool"]
        ci_tool_dones = [
            e for e in tool_events
            if isinstance(e.data, dict)
            and e.data.get("phase") == "done"
            and e.data.get("name") == "code_interpreter"
        ]
        assert len(ci_tool_dones) >= 1, (
            f"Expected code_interpreter done event. "
            f"Tool events: {[t.data for t in tool_events]}"
        )

        # ── Verify turn state via REST ──
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/turns/{rid}")
        assert resp.status_code == 200
        turn = resp.json()
        assert turn["state"] == "done"
        assert turn["assistant_message_id"] is not None

        # ── Verify message tokens via REST ──
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(asst_msgs) >= 1
        m = asst_msgs[-1]
        assert m["input_tokens"] == sse_input, (
            f"API input_tokens ({m['input_tokens']}) != SSE ({sse_input})"
        )
        assert m["output_tokens"] == sse_output, (
            f"API output_tokens ({m['output_tokens']}) != SSE ({sse_output})"
        )

        # ── Verify credits via quota endpoint ──
        assert sse_usage == EXPECTED_USAGE
        assert_no_reserves(USER_A_ID)
        spent_after = find_period(get_quota_status(), "total", "daily")["used_credits_micro"]
        assert spent_after - spent_before == EXPECTED_CREDITS[provider]

    @pytest.mark.timeout(20)
    def test_code_interpreter_calls_tracked_in_db(self, provider, server, xlsx_chat):
        """Verify code_interpreter_calls is incremented in quota_usage table."""
        chat_id = xlsx_chat["chat_id"]
        att_id = xlsx_chat["att_id"]

        # Get CI calls before
        ci_before = _query_ci_calls()

        status, events, _ = stream_message(
            chat_id,
            "CODEINTERP: what is the sum?",
            attachment_ids=[att_id],
        )
        assert status == 200
        expect_done(events)
        assert_no_reserves(USER_A_ID)

        # The CODEINTERP scenario emits exactly one completed code_interpreter call.
        assert _query_ci_calls() == ci_before + 1

    @pytest.mark.timeout(20)
    def test_non_ci_turn_has_zero_ci_calls(self, provider, server):
        """A normal turn (no XLSX) should not increment code_interpreter_calls."""
        model = PROVIDER_DEFAULT_MODEL[provider]

        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": model})
        assert resp.status_code == 201
        chat_id = resp.json()["id"]

        ci_before = _query_ci_calls()

        status, events, _ = stream_message(chat_id, "What is 2+2? Answer in one word.")
        assert status == 200
        expect_done(events)
        assert_no_reserves(USER_A_ID)

        ci_after = _query_ci_calls()

        assert ci_after == ci_before, (
            f"code_interpreter_calls changed without CI: before={ci_before}, after={ci_after}"
        )

        # Verify no code_interpreter tool events
        tool_events = [e for e in events if e.event == "tool"]
        ci_events = [
            e for e in tool_events
            if isinstance(e.data, dict) and e.data.get("name") == "code_interpreter"
        ]
        assert len(ci_events) == 0, (
            f"Unexpected code_interpreter events: {[t.data for t in ci_events]}"
        )
