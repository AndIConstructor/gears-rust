"""Thread summary: trigger at finalization, summary worker, use in context assembly.

The chat uses the small-context catalog model (config/base.yaml,
`gpt-4.1-mini-tiny-ctx`: context_window 4096, max_output_tokens 1024,
fixed_overhead_tokens 500). The trigger fires when the estimated context
reaches compression_threshold_pct (60, base.yaml) of
context_window - max_output_tokens = 3072, that is 1843 tokens. Each message
is estimated at about 550 tokens and the system prompt at about 590, so the
first turn (about 1140) stays below and the second (about 2250) reaches it.

The summary model is `summary_model_id` (gpt-5-mini). The mock answers the
non-streaming summary request with `MOCK-SUMMARY <n> user and <m> assistant
messages` (mock_provider/responses.py).
"""

from __future__ import annotations

import time
import uuid

import pytest

from .conftest import (
    CATALOG_SYSTEM_PROMPT,
    expect_done,
    expect_stream_started,
    list_messages,
    poll_turn,
    provider_input,
    query_db,
    stream_message,
    uuid_from_db,
)
from .mock_provider.responses import SUMMARY_OUTPUT_TOKENS

TINY_CTX_MODEL = "gpt-4.1-mini-tiny-ctx"
SUMMARY_MODEL_PROVIDER_ID = "gpt-5-mini"  # provider_model_id of summary_model_id

# Prefix of the summary message in the provider input
# (SUMMARY_PREAMBLE, domain/service/context_assembly.rs).
SUMMARY_PREAMBLE = (
    "This conversation has earlier messages that have been summarized. "
    "The summary below covers the earlier portion of the conversation. "
    "Recent messages follow after.\n\n"
)


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("inspects the mock provider's captured requests")


def _complete_turn(chat_id: str, content: str):
    rid = str(uuid.uuid4())
    status, events, raw = stream_message(chat_id, content, request_id=rid)
    assert status == 200, raw
    expect_done(events)
    poll_turn(chat_id, rid, ("done",))
    return events


def _summary_rows(chat_id: str) -> list[dict]:
    return query_db(
        "SELECT summary_text, token_estimate, summarized_up_to_message_id "
        "FROM thread_summaries WHERE chat_id = ?",
        (chat_id,),
    )


def _wait_for_summary(chat_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    rows = _summary_rows(chat_id)
    while not rows and time.monotonic() < deadline:
        time.sleep(0.2)
        rows = _summary_rows(chat_id)
    assert len(rows) == 1, f"no thread summary for chat {chat_id} within {timeout}s"
    return rows[0]


class TestThreadSummary:
    """16-05, 19-09, 19-10."""

    @pytest.mark.timeout(60)
    def test_summary_replaces_summarized_messages(self, request, chat_with_model, mock_provider):
        """The turn that reaches the threshold schedules a summary of every
        message up to its answer; the worker stores it and marks the messages
        compressed; the next turn sends the summary instead of those messages."""
        _require_offline(request)
        chat_id = chat_with_model(TINY_CTX_MODEL)["id"]

        _complete_turn(chat_id, "First question.")
        assert _summary_rows(chat_id) == [], "one turn is below the threshold"

        _complete_turn(chat_id, "Second question.")
        summary = _wait_for_summary(chat_id)

        messages = list_messages(chat_id)
        assert [m["role"] for m in messages] == ["user", "assistant"] * 2
        assert summary["summary_text"] == "MOCK-SUMMARY 2 user and 2 assistant messages"
        assert summary["token_estimate"] == SUMMARY_OUTPUT_TOKENS
        assert uuid_from_db(summary["summarized_up_to_message_id"]) == messages[-1]["id"]
        compressed = query_db(
            "SELECT is_compressed FROM messages WHERE chat_id = ? AND deleted_at IS NULL",
            (chat_id,),
        )
        assert [r["is_compressed"] for r in compressed] == [1, 1, 1, 1]

        # The summary request: non-streaming, on the summary model, with the
        # four messages in the prompt.
        summary_requests = [
            r for r in mock_provider.get_captured_requests() if r.get("stream") is False
        ]
        assert len(summary_requests) == 1, summary_requests
        assert summary_requests[0]["model"] == SUMMARY_MODEL_PROVIDER_ID
        prompt = provider_input(summary_requests[0])[0][1]
        for line in (
            "User: First question.", "User: Second question.",
            f"Assistant: {messages[1]['content']}",
        ):
            assert line in prompt, (line, prompt)

        mock_provider.clear_captured_requests()
        events = _complete_turn(chat_id, "Third question.")
        assert expect_stream_started(events).data["thread_summary_applied"] == {
            "token_estimate": SUMMARY_OUTPUT_TOKENS,
        }
        captured = mock_provider.get_captured_requests()
        assert len(captured) == 1, captured
        assert captured[0]["instructions"] == CATALOG_SYSTEM_PROMPT
        assert provider_input(captured[0]) == [
            ("user", SUMMARY_PREAMBLE + summary["summary_text"]),
            ("user", "Third question."),
        ]
