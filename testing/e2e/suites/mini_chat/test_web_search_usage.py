"""Web search usage verification tests.

Exercises web search with the mock provider, then checks quota usage via the
REST quota endpoint (literal expected credits, see EXPECTED_CREDITS), message
tokens via the messages API, and that no quota reserve is left in the DB.

Provider-parameterized — runs against both OpenAI and Azure mock endpoints.
"""

from __future__ import annotations

import uuid

import pytest
import httpx

from .conftest import (
    API_PREFIX, PROVIDER_DEFAULT_MODEL, USER_A_ID,
    assert_no_reserves, expect_done, parse_sse,
)


# ── Quota endpoint helpers ───────────────────────────────────────────────

def _get_quota_status() -> dict:
    resp = httpx.get(f"{API_PREFIX}/quota/status", timeout=10)
    assert resp.status_code == 200
    return resp.json()


def _find_period(tiers: list, tier_name: str, period_name: str) -> dict | None:
    for t in tiers:
        if t["tier"] == tier_name:
            for p in t["periods"]:
                if p["period"] == period_name:
                    return p
    return None


# ── Expected charges ─────────────────────────────────────────────────────
#
# Mock "SEARCH:*" scenario: usage input = max(80, 50 * input items) and
# output = 15. A first message sends one input item, so input = 80.
# credits_micro = ceil(input * in_mult / 1e6) + ceil(output * out_mult / 1e6)
# with the base.yaml multipliers:
#   gpt-5.2        (openai): 80 * 1.0 + 15 * 3.0  =  80 +  45 = 125
#   azure-gpt-4.1  (azure):  80 * 3.0 + 15 * 15.0 = 240 + 225 = 465
EXPECTED_USAGE = {"input_tokens": 80, "output_tokens": 15}
EXPECTED_CREDITS = {"openai": 125, "azure": 465}


@pytest.mark.multi_provider
class TestWebSearchUsageAccounting:
    """Verify that web search turns produce correct quota and message records."""

    @pytest.fixture(autouse=True)
    def _offline_only(self, request):
        if request.config.getoption("mode") == "online":
            pytest.skip("literal credit amounts depend on the mock's fixed usage")

    def test_web_search_usage_correct(self, provider, server):
        """Single web-search turn: verify credits, messages, and turn state."""
        model = PROVIDER_DEFAULT_MODEL[provider]

        # Snapshot quota before via REST
        before = _get_quota_status()
        before_td = _find_period(before["tiers"], "total", "daily")
        spent_before = before_td["used_credits_micro"]

        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": model})
        assert resp.status_code == 201
        chat = resp.json()
        chat_id = chat["id"]

        rid = str(uuid.uuid4())
        _url = f"{API_PREFIX}/chats/{chat_id}/messages:stream"
        _resp = httpx.post(_url, json={"content": "SEARCH: current population of Tokyo", "web_search": {"enabled": True}, "request_id": rid}, headers={"Accept": "text/event-stream"}, timeout=90)
        status = _resp.status_code
        events = parse_sse(_resp.text) if status == 200 else []
        assert status == 200
        done = expect_done(events)

        sse_usage = done.data["usage"]
        sse_input = sse_usage["input_tokens"]
        sse_output = sse_usage["output_tokens"]

        tool_events = [e for e in events if e.event == "tool"]
        ws_tool_dones = [
            e for e in tool_events
            if isinstance(e.data, dict)
            and e.data.get("phase") == "done"
            and e.data.get("name") in ("web_search", "web_search_preview")
        ]

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
        after = _get_quota_status()
        spent_after = _find_period(after["tiers"], "total", "daily")["used_credits_micro"]
        assert spent_after - spent_before == EXPECTED_CREDITS[provider]

        assert len(ws_tool_dones) > 0, "Expected web_search tool done events"
