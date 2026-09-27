"""Tests for turn mutation operations — retry, edit, delete, concurrency, replaced_by tracking."""

import threading
import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    TINY_CTX_MODEL,
    OpenStream,
    assert_problem,
    delta_text,
    expect_done,
    expect_stream_started,
    list_messages,
    open_stream,
    parse_sse,
    poll_turn,
    query_db,
    slow_scenario,
    stream_message,
    uuid_from_db,
)
from .mock_provider.responses import MockEvent, Scenario


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def complete_turn(chat_id: str, content: str = "Say OK.") -> str:
    """Send a message and return its request_id once the turn is done."""
    rid = str(uuid.uuid4())
    status, events, _ = stream_message(chat_id, content, request_id=rid)
    assert status == 200, f"stream_message failed: {status}"
    expect_done(events)
    poll_turn(chat_id, rid, ("done",))
    return rid


def turn_url(chat_id: str, rid: str) -> str:
    return f"{API_PREFIX}/chats/{chat_id}/turns/{rid}"


def retry(chat_id: str, rid: str) -> httpx.Response:
    return httpx.post(
        f"{turn_url(chat_id, rid)}/retry",
        headers={"Accept": "text/event-stream"}, timeout=90,
    )


def edit(chat_id: str, rid: str, content: str) -> httpx.Response:
    return httpx.patch(
        turn_url(chat_id, rid), json={"content": content},
        headers={"Accept": "text/event-stream"}, timeout=90,
    )


def cancel_turn(chat_id: str, mock_provider, content: str = "Cancel me.") -> tuple[str, str]:
    """Disconnect after two deltas; return (request_id, received text)."""
    rid = str(uuid.uuid4())
    mock_provider.set_next_scenario(slow_scenario(20, slow=0.3))
    with open_stream(chat_id, content, request_id=rid) as s:
        seen = []
        s.read_until(lambda e: e.event == "delta" and (seen.append(e) or len(seen) == 2))
        received = delta_text(s.events)
    assert poll_turn(chat_id, rid)["state"] == "cancelled"
    return rid, received


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("requires mock provider (offline mode)")


# ---------------------------------------------------------------------------
# Tests: retry
# ---------------------------------------------------------------------------

class TestTurnRetry:
    """POST /turns/{request_id}/retry constraints and effect."""

    @pytest.mark.timeout(30)
    def test_retry_running_turn_400(self, request, chat, mock_provider):
        """Retrying a running turn is 400 failed_precondition (turn_state/STATE)."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        with open_stream(chat_id, "Slow turn.", request_id=rid) as s:
            s.read_until_started()
            resp = retry(chat_id, rid)
            assert_problem(
                resp, 400, "failed_precondition",
                violation_subject="turn_state", violation_type="STATE",
            )
            assert httpx.get(turn_url(chat_id, rid)).json()["state"] == "running"
            expect_done(s.drain())

    @pytest.mark.timeout(30)
    def test_retry_non_latest_turn_409(self, chat):
        """Retrying a turn that is not the latest is 409 NOT_LATEST_TURN; nothing changes."""
        chat_id = chat["id"]
        rid1 = complete_turn(chat_id, "First turn.")
        complete_turn(chat_id, "Second turn.")
        before = list_messages(chat_id)

        assert_problem(retry(chat_id, rid1), 409, "aborted", reason="NOT_LATEST_TURN")
        assert list_messages(chat_id) == before

    @pytest.mark.timeout(30)
    def test_retry_replaces_the_answer(self, chat):
        """After retry the chat holds the original user message and exactly one new answer."""
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Retry me.")

        resp = retry(chat_id, rid)
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        new_rid = expect_stream_started(events).data["request_id"]
        expect_done(events)

        messages = list_messages(chat_id)
        assert [(m["role"], m["request_id"]) for m in messages] == [
            ("user", new_rid), ("assistant", new_rid),
        ]
        assert messages[0]["content"] == "Retry me."
        assert messages[1]["content"] == delta_text(events)

    @pytest.mark.timeout(30)
    def test_retry_failed_turn(self, request, chat, mock_provider):
        """08-12: a turn in `error` state can be retried; the retry produces one new answer."""
        _require_offline(request)
        chat_id = chat["id"]
        mock_provider.set_next_scenario(Scenario(
            events=[MockEvent("response.output_text.delta", {"delta": "Partial"})],
            terminal="failed",
            error={"code": "server_error", "message": "Mock provider error"},
        ))
        rid = str(uuid.uuid4())
        status, _, _ = stream_message(chat_id, "Trigger error.", request_id=rid)
        assert status == 200
        assert poll_turn(chat_id, rid)["state"] == "error"

        resp = retry(chat_id, rid)
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        new_rid = expect_stream_started(events).data["request_id"]
        expect_done(events)

        assistant = [m for m in list_messages(chat_id) if m["role"] == "assistant"]
        assert [m["request_id"] for m in assistant] == [new_rid]

    @pytest.mark.timeout(30)
    def test_retry_cancelled_turn(self, request, chat, mock_provider):
        """A cancelled turn can be retried; its partial answer is replaced by one new answer."""
        _require_offline(request)
        chat_id = chat["id"]
        rid, _ = cancel_turn(chat_id, mock_provider)

        resp = retry(chat_id, rid)
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        new_rid = expect_stream_started(events).data["request_id"]
        expect_done(events)

        messages = list_messages(chat_id)
        assert [(m["role"], m["request_id"]) for m in messages] == [
            ("user", new_rid), ("assistant", new_rid),
        ]
        assert messages[1]["content"] == delta_text(events)


# ---------------------------------------------------------------------------
# Tests: edit
# ---------------------------------------------------------------------------

class TestTurnEdit:
    """PATCH /turns/{request_id} replaces the last user message and its answer."""

    @pytest.mark.timeout(30)
    def test_edit_replaces_user_message_and_answer(self, chat):
        chat_id = chat["id"]
        complete_turn(chat_id, "Keep this turn.")
        rid = complete_turn(chat_id, "Original question.")
        before = list_messages(chat_id)

        resp = edit(chat_id, rid, "Edited question.")
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        new_rid = expect_stream_started(events).data["request_id"]
        expect_done(events)

        messages = list_messages(chat_id)
        assert messages[:2] == before[:2], "the earlier turn must be untouched"
        assert [(m["role"], m["request_id"]) for m in messages[2:]] == [
            ("user", new_rid), ("assistant", new_rid),
        ]
        assert messages[2]["content"] == "Edited question."
        assert messages[3]["content"] == delta_text(events)
        old_ids = {m["id"] for m in before[2:]}
        assert old_ids.isdisjoint(m["id"] for m in messages)

    @pytest.mark.timeout(30)
    def test_edit_empty_content_400(self, chat):
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Question.")
        before = list_messages(chat_id)

        assert_problem(edit(chat_id, rid, ""), 400, "invalid_argument", field_reason="EMPTY_CONTENT")
        assert list_messages(chat_id) == before

    @pytest.mark.timeout(30)
    def test_edit_whitespace_only_content_400(self, chat):
        """Edit content of only whitespace is empty after trimming: 400
        invalid_argument EMPTY_CONTENT on `content`; the turn is kept."""
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Question.")
        before = list_messages(chat_id)

        body = assert_problem(
            edit(chat_id, rid, "  \n\t "), 400, "invalid_argument", field_reason="EMPTY_CONTENT",
        )
        assert [v["field"] for v in body["context"]["field_violations"]] == ["content"], body
        assert list_messages(chat_id) == before
        assert poll_turn(chat_id, rid)["state"] == "done"

    @pytest.mark.timeout(30)
    def test_edit_body_errors(self, chat):
        """A body without `content` (schema-invalid) is 422 invalid_argument;
        malformed JSON is 400 invalid_argument. The turn is kept."""
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Question.")
        before = list_messages(chat_id)

        resp = httpx.patch(turn_url(chat_id, rid), json={}, timeout=30)
        assert_problem(resp, 422, "invalid_argument")
        resp = httpx.patch(
            turn_url(chat_id, rid), content=b"{not json",
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="json_syntax_error")
        assert list_messages(chat_id) == before

    @pytest.mark.usefixtures("offline_only")
    @pytest.mark.timeout(30)
    def test_edit_content_over_max_input_tokens_400(self, chat_with_model, mock_provider):
        """Edit content of 12000 bytes on gpt-4.1-mini-tiny-ctx is estimated at
        (3000 + 500) * 1.1 = 3850 tokens > max_input_tokens 3000 (see
        test_streaming.py TestStreamInputLimits): 400 out_of_range
        INPUT_TOO_LONG; the turn is kept and the provider is not called."""
        chat_id = chat_with_model(TINY_CTX_MODEL)["id"]
        rid = complete_turn(chat_id, "Question.")
        before = list_messages(chat_id)

        mock_provider.clear_captured_requests()
        resp = edit(chat_id, rid, "x" * 12_000)
        assert_problem(resp, 400, "out_of_range", field_reason="INPUT_TOO_LONG")
        assert mock_provider.get_captured_requests() == []
        assert list_messages(chat_id) == before
        assert poll_turn(chat_id, rid)["state"] == "done"

    @pytest.mark.usefixtures("offline_only")
    @pytest.mark.timeout(30)
    def test_edit_content_over_context_budget_400(self, chat_with_model, mock_provider):
        """Edit content of 6000 bytes on gpt-4.1-mini-tiny-ctx (2200 tokens)
        passes max_input_tokens but, with the system prompt (587 tokens),
        exceeds the context budget min(3000, 4096 - 1024) - 500 = 2500 (see
        test_streaming.py TestStreamInputLimits). Context assembly
        runs after the edit committed (DESIGN §3.9, preflight-before-mutation
        order): 400 out_of_range CONTEXT_BUDGET_EXCEEDED, the provider is not
        called, the old turn is replaced and the new turn fails with
        `context_length_exceeded` and no answer."""
        chat_id = chat_with_model(TINY_CTX_MODEL)["id"]
        rid = complete_turn(chat_id, "Question.")
        content = "x" * 6_000

        mock_provider.clear_captured_requests()
        resp = edit(chat_id, rid, content)
        assert_problem(resp, 400, "out_of_range", field_reason="CONTEXT_BUDGET_EXCEEDED")
        assert mock_provider.get_captured_requests() == []

        live = query_db(
            "SELECT request_id FROM chat_turns WHERE chat_id = ? AND deleted_at IS NULL",
            (chat_id,),
        )
        assert len(live) == 1, live
        new_rid = uuid_from_db(live[0]["request_id"])
        assert new_rid != rid
        assert httpx.get(turn_url(chat_id, rid)).status_code == 404
        turn = poll_turn(chat_id, new_rid)
        assert (turn["state"], turn["error_code"]) == ("error", "context_length_exceeded"), turn
        assert turn.get("assistant_message_id") is None, turn
        assert [(m["role"], m["content"], m["request_id"]) for m in list_messages(chat_id)] == [
            ("user", content, new_rid),
        ]

    @pytest.mark.timeout(30)
    def test_edit_non_latest_turn_409(self, chat):
        chat_id = chat["id"]
        rid1 = complete_turn(chat_id, "First turn.")
        complete_turn(chat_id, "Second turn.")
        before = list_messages(chat_id)

        assert_problem(edit(chat_id, rid1, "Changed."), 409, "aborted", reason="NOT_LATEST_TURN")
        assert list_messages(chat_id) == before

    @pytest.mark.timeout(30)
    def test_edit_running_turn_400(self, request, chat, mock_provider):
        """Editing a running turn is 400 failed_precondition (turn_state/STATE);
        the turn keeps streaming."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        with open_stream(chat_id, "Slow turn.", request_id=rid) as s:
            s.read_until_started()
            assert_problem(
                edit(chat_id, rid, "Changed."), 400, "failed_precondition",
                violation_subject="turn_state", violation_type="STATE",
            )
            assert httpx.get(turn_url(chat_id, rid)).json()["state"] == "running"
            expect_done(s.drain())
        assert [m["content"] for m in list_messages(chat_id) if m["role"] == "user"] == [
            "Slow turn.",
        ]


class TestUnknownTurn:
    """Retry, edit and delete of a request_id that has no turn in the chat: 404 not_found."""

    @pytest.mark.timeout(30)
    def test_retry_unknown_turn_404(self, chat):
        complete_turn(chat["id"])
        assert_problem(retry(chat["id"], str(uuid.uuid4())), 404, "not_found")

    @pytest.mark.timeout(30)
    def test_edit_unknown_turn_404(self, chat):
        complete_turn(chat["id"])
        assert_problem(edit(chat["id"], str(uuid.uuid4()), "Changed."), 404, "not_found")

    @pytest.mark.timeout(30)
    def test_delete_unknown_turn_404(self, chat):
        complete_turn(chat["id"])
        resp = httpx.delete(turn_url(chat["id"], str(uuid.uuid4())), timeout=10)
        assert_problem(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# Tests: delete
# ---------------------------------------------------------------------------

class TestTurnDelete:
    """DELETE /turns/{request_id} constraints and behavior."""

    @pytest.mark.timeout(30)
    def test_delete_last_turn_204(self, chat):
        """Deleting the last (and only) turn returns 204 and removes messages."""
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Only turn.")

        resp = httpx.delete(turn_url(chat_id, rid), timeout=10)
        assert resp.status_code == 204
        assert list_messages(chat_id) == []

    @pytest.mark.timeout(30)
    def test_delete_running_turn_400(self, request, chat, mock_provider):
        """Deleting a running turn is 400 failed_precondition (turn_state/STATE)."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        with open_stream(chat_id, "Slow turn.", request_id=rid) as s:
            s.read_until_started()
            resp = httpx.delete(turn_url(chat_id, rid), timeout=10)
            assert_problem(
                resp, 400, "failed_precondition",
                violation_subject="turn_state", violation_type="STATE",
            )
            assert httpx.get(turn_url(chat_id, rid)).json()["state"] == "running"
            expect_done(s.drain())

    @pytest.mark.timeout(30)
    def test_delete_non_latest_turn_409(self, chat):
        """Deleting a turn that is not the latest is 409 NOT_LATEST_TURN; nothing changes."""
        chat_id = chat["id"]
        rid1 = complete_turn(chat_id, "First turn.")
        complete_turn(chat_id, "Second turn.")
        before = list_messages(chat_id)

        resp = httpx.delete(turn_url(chat_id, rid1), timeout=10)
        assert_problem(resp, 409, "aborted", reason="NOT_LATEST_TURN")
        assert list_messages(chat_id) == before

    @pytest.mark.timeout(30)
    def test_soft_deleted_turn_excluded_from_messages(self, chat):
        """After deleting the last turn, its messages disappear from GET /messages."""
        chat_id = chat["id"]
        rid1 = complete_turn(chat_id, "First turn.")
        rid2 = complete_turn(chat_id, "Second turn.")

        resp = httpx.delete(turn_url(chat_id, rid2), timeout=10)
        assert resp.status_code == 204

        messages = list_messages(chat_id)
        assert [(m["role"], m["request_id"]) for m in messages] == [
            ("user", rid1), ("assistant", rid1),
        ]

    @pytest.mark.timeout(30)
    def test_get_deleted_turn_404(self, chat):
        chat_id = chat["id"]
        rid = complete_turn(chat_id)
        assert httpx.delete(turn_url(chat_id, rid), timeout=10).status_code == 204

        assert_problem(httpx.get(turn_url(chat_id, rid)), 404, "not_found")

    @pytest.mark.timeout(30)
    def test_second_delete_turn_409_not_latest(self, chat):
        """A deleted turn is no longer the latest one: deleting it again is 409 NOT_LATEST_TURN."""
        chat_id = chat["id"]
        rid = complete_turn(chat_id)
        assert httpx.delete(turn_url(chat_id, rid), timeout=10).status_code == 204

        resp = httpx.delete(turn_url(chat_id, rid), timeout=10)
        assert_problem(resp, 409, "aborted", reason="NOT_LATEST_TURN")

    @pytest.mark.timeout(30)
    def test_deleted_turn_not_sent_to_provider(self, request, chat, mock_provider):
        """The content of a deleted turn is not part of the next provider request."""
        _require_offline(request)
        chat_id = chat["id"]
        complete_turn(chat_id, "KEEP-7f3a question.")
        rid = complete_turn(chat_id, "FORGET-9c1d question.")
        assert httpx.delete(turn_url(chat_id, rid), timeout=10).status_code == 204

        mock_provider.clear_captured_requests()
        complete_turn(chat_id, "Next question.")
        provider_input = str(mock_provider.get_last_request()["input"])
        assert "KEEP-7f3a" in provider_input
        assert "FORGET-9c1d" not in provider_input


# ---------------------------------------------------------------------------
# Tests: concurrent retries
# ---------------------------------------------------------------------------

class TestConcurrentRetries:
    """Two retries of the same turn — exactly one wins."""

    @pytest.mark.timeout(30)
    def test_concurrent_retries_one_wins(self, request, chat, mock_provider):
        """Two simultaneous retries of the same turn: one streams (200), the
        other is 409 NOT_LATEST_TURN.

        The winner streams a slow answer, so it is still running when the
        loser is checked. The rig's SQLite pool has one connection
        (base.yaml `max_conns: 1`), so the two mutation transactions run one
        after the other: the loser sees the winner's new turn as the latest
        one. GENERATION_IN_PROGRESS needs two mutation transactions in flight
        at once; only its error mapping is unit-tested (api/rest/error.rs).
        """
        _require_offline(request)
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Retryable turn.")
        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))

        results = [None, None]

        def send_retry(idx):
            results[idx] = retry(chat_id, rid)

        threads = [threading.Thread(target=send_retry, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)
        assert [t.is_alive() for t in threads] == [False, False], "a retry did not return"
        assert None not in results, results  # a retry raised in its thread

        by_status = sorted(results, key=lambda r: r.status_code)
        assert [r.status_code for r in by_status] == [200, 409], (
            f"expected one 200 and one 409, got {[r.status_code for r in results]}"
        )
        assert_problem(by_status[1], 409, "aborted", reason="NOT_LATEST_TURN")
        winner = parse_sse(by_status[0].text)
        new_rid = expect_stream_started(winner).data["request_id"]
        expect_done(winner)
        assert [(m["role"], m["request_id"]) for m in list_messages(chat_id)] == [
            ("user", new_rid), ("assistant", new_rid),
        ]

    @pytest.mark.timeout(30)
    def test_retry_while_retry_running_409_not_latest(self, request, chat, mock_provider):
        """A retry of the old turn while its first retry is streaming is 409 NOT_LATEST_TURN."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = complete_turn(chat_id, "Retryable turn.")

        mock_provider.set_next_scenario(slow_scenario(10, slow=0.3))
        with OpenStream(f"{turn_url(chat_id, rid)}/retry", None) as first:
            first.read_until_started()
            assert_problem(retry(chat_id, rid), 409, "aborted", reason="NOT_LATEST_TURN")
            expect_done(first.drain())


# ---------------------------------------------------------------------------
# Tests: replaced_by_request_id tracking
# ---------------------------------------------------------------------------

class TestReplacedByRequestId:
    """After retry, the original turn's replaced_by_request_id points to the new turn."""

    @pytest.mark.timeout(30)
    def test_replaced_by_request_id_set(self, chat):
        chat_id = chat["id"]
        rid1 = complete_turn(chat_id, "Original turn.")

        resp = retry(chat_id, rid1)
        assert resp.status_code == 200, f"Retry failed: {resp.status_code} {resp.text}"
        retry_events = parse_sse(resp.text)
        ss = expect_stream_started(retry_events)
        assert ss.data.get("is_new_turn") is True
        new_rid = ss.data["request_id"]
        expect_done(retry_events)

        rows = query_db(
            "SELECT replaced_by_request_id FROM chat_turns WHERE request_id = ?",
            (rid1,),
        )
        assert len(rows) == 1, f"Turn {rid1} not found in DB"
        assert uuid_from_db(rows[0]["replaced_by_request_id"]) == new_rid
