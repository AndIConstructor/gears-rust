"""Thread summary: trigger at finalization, summary worker, use in context assembly.

The chat uses the small-context catalog model (config/base.yaml,
`gpt-4.1-mini-tiny-ctx`: context_window 4096, max_output_tokens 1024,
max_input_tokens 3000, fixed_overhead_tokens 500). The trigger fires when
the estimated context reaches compression_threshold_pct (60, base.yaml) of
min(max_input_tokens, context_window - max_output_tokens) = 3000, that is
1800 tokens. Each message
is estimated at about 550 tokens and the system prompt at about 590, so the
first turn (about 1140) stays below and the second (about 2250) reaches it.

The summary model is `summary_model_id` (gpt-5-mini). The mock answers the
non-streaming summary request with `MOCK-SUMMARY <n> user and <m> assistant
messages` (mock_provider/responses.py).
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    CATALOG_SYSTEM_PROMPT,
    TENANT_A_ID,
    TINY_CTX_MODEL,
    expect_done,
    expect_stream_started,
    list_messages,
    parse_sse,
    outbox_payloads,
    poll_turn,
    provider_input,
    query_db,
    stream_message,
    uuid_from_db,
)
from .mock_provider.responses import SUMMARY_OUTPUT_TOKENS

SUMMARY_MODEL_PROVIDER_ID = "gpt-5-mini"  # provider_model_id of summary_model_id

# Subject of the summary request: the platform default subject
# (toolkit_security::constants::DEFAULT_SUBJECT_ID, libs/toolkit-security/src/constants.rs).
DEFAULT_SUBJECT_ID = "11111111-6a88-4768-9dfc-6bcd5187d9ed"

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


def _complete_turn(chat_id: str, content: str, request_id: str | None = None):
    rid = request_id or str(uuid.uuid4())
    status, events, raw = stream_message(chat_id, content, request_id=rid)
    assert status == 200, raw
    expect_done(events)
    poll_turn(chat_id, rid, ("done",))
    return events


def _summary_tasks(chat_id: str) -> list[dict]:
    """Thread-summary task payloads of the chat in the outbox body table.

    The task is enqueued in the transaction that finalizes the turn
    (finalization_service.rs), so once the turn is `done` it is either
    there or not enqueued at all."""
    return [
        p for p in outbox_payloads(chat_id)
        if p.get("chat_id") == chat_id
        and p.get("system_task_type") == "thread_summary_update"
        and "frozen_target_message_id" in p  # not the summary turn's usage event
    ]


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
        """The turn that reaches the threshold schedules a summary of the
        messages before it (the finalized turn stays out: retry, edit and
        delete may still replace it); the worker stores the summary and marks
        those messages compressed; the next turn sends the summary instead of
        them, followed by the unsummarized turn."""
        _require_offline(request)
        chat_id = chat_with_model(TINY_CTX_MODEL)["id"]

        _complete_turn(chat_id, "First question.")
        assert _summary_tasks(chat_id) == [], "one turn is below the threshold"

        _complete_turn(chat_id, "Second question.")
        summary = _wait_for_summary(chat_id)
        tasks = _summary_tasks(chat_id)
        assert len(tasks) == 1, tasks

        messages = list_messages(chat_id)
        assert [m["role"] for m in messages] == ["user", "assistant"] * 2
        assert summary["summary_text"] == "MOCK-SUMMARY 1 user and 1 assistant messages"
        assert summary["token_estimate"] == SUMMARY_OUTPUT_TOKENS
        # The frontier is the first turn's answer, not the finalized second turn.
        assert uuid_from_db(summary["summarized_up_to_message_id"]) == messages[1]["id"]
        assert tasks[0]["frozen_target_message_id"] == messages[1]["id"], tasks
        compressed = query_db(
            "SELECT is_compressed FROM messages WHERE chat_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at, id",
            (chat_id,),
        )
        assert [r["is_compressed"] for r in compressed] == [1, 1, 0, 0]

        # The summary request: non-streaming, on the summary model, with the
        # first turn in the prompt and without the second.
        summary_requests = [
            r for r in mock_provider.get_captured_requests() if r.get("stream") is False
        ]
        assert len(summary_requests) == 1, summary_requests
        assert summary_requests[0]["model"] == SUMMARY_MODEL_PROVIDER_ID
        # A system task: the tenant with the default subject, request_type summary.
        assert summary_requests[0]["user"] == f"{TENANT_A_ID}:{DEFAULT_SUBJECT_ID}"
        assert summary_requests[0]["metadata"] == {
            "tenant_id": TENANT_A_ID,
            "user_id": DEFAULT_SUBJECT_ID,
            "chat_id": chat_id,
            "request_type": "summary",
            "feature": "none",
        }, summary_requests[0]["metadata"]
        prompt = provider_input(summary_requests[0])[0][1]
        for line in ("User: First question.", f"Assistant: {messages[1]['content']}"):
            assert line in prompt, (line, prompt)
        assert "Second question." not in prompt, prompt

        mock_provider.clear_captured_requests()
        events = _complete_turn(chat_id, "Third question.")
        assert expect_stream_started(events).data["thread_summary_applied"] == {
            "token_estimate": SUMMARY_OUTPUT_TOKENS,
        }
        # A later summary run may add a non-streaming request; keep the turn's.
        captured = [r for r in mock_provider.get_captured_requests() if r.get("stream") is not False]
        assert len(captured) == 1, captured
        assert captured[0]["instructions"] == CATALOG_SYSTEM_PROMPT
        # The second turn is not summarized, and on this small-context model
        # it does not fit next to the summary: truncation drops it as a whole
        # turn (question and answer), never an answer without its question.
        assert provider_input(captured[0]) == [
            ("user", SUMMARY_PREAMBLE + summary["summary_text"]),
            ("user", "Third question."),
        ]

    @pytest.mark.timeout(60)
    def test_retry_after_summary_does_not_resend_replaced_answer(
        self, request, chat_with_model, mock_provider,
    ):
        """Retrying the turn that triggered the summary: the summary does not
        contain that turn, so the retried request carries the summary, the
        original question and nothing of the replaced answer."""
        _require_offline(request)
        chat_id = chat_with_model(TINY_CTX_MODEL)["id"]
        _complete_turn(chat_id, "First question.")
        second_rid = str(uuid.uuid4())
        _complete_turn(chat_id, "Second question.", request_id=second_rid)
        summary = _wait_for_summary(chat_id)
        replaced_answer = list_messages(chat_id)[3]["content"]

        mock_provider.clear_captured_requests()
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/turns/{second_rid}/retry",
            headers={"Accept": "text/event-stream"}, timeout=90,
        )
        assert resp.status_code == 200, resp.text
        expect_done(parse_sse(resp.text))

        captured = [r for r in mock_provider.get_captured_requests() if r.get("stream") is not False]
        assert len(captured) == 1, captured
        assert provider_input(captured[0]) == [
            ("user", SUMMARY_PREAMBLE + summary["summary_text"]),
            ("user", "Second question."),
        ]
        assert replaced_answer not in str(captured[0]), "replaced answer resent"
