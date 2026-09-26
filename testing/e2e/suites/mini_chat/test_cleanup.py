"""Tests for cleanup — chat deletion, attachment cleanup outbox events.

Background cleanup is not visible over HTTP. These tests check the effects:
DELETE status codes, 404 after deletion, attachment `cleanup_status` and the
cleanup outbox payloads (read directly from the DB).

Orphan-watchdog finalization is covered by unit tests (turn_repo.rs,
finalization_service.rs): the minimum watchdog timeout (90 s) does not fit
the E2E time budget.
"""

from __future__ import annotations

import io
import json
import time

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    DEFAULT_MODEL,
    STANDARD_MODEL,
    assert_problem,
    poll_until,
    query_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_chat(model: str | None = None) -> dict:
    body = {"model": model} if model else {}
    resp = httpx.post(f"{API_PREFIX}/chats", json=body, timeout=10)
    assert resp.status_code == 201, f"Create chat failed: {resp.status_code} {resp.text}"
    return resp.json()


def delete_chat(chat_id: str) -> httpx.Response:
    return httpx.delete(f"{API_PREFIX}/chats/{chat_id}", timeout=10)


def get_chat(chat_id: str) -> httpx.Response:
    return httpx.get(f"{API_PREFIX}/chats/{chat_id}", timeout=10)


def upload_file(
    chat_id: str,
    content: bytes = b"Hello, world!",
    filename: str = "test.txt",
    content_type: str = "text/plain",
) -> httpx.Response:
    return httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/attachments",
        files={"file": (filename, io.BytesIO(content), content_type)},
        timeout=60,
    )


def chat_cleanup_payloads(chat_id: str) -> list[dict]:
    """Chat soft-delete cleanup payloads for `chat_id` in the outbox body table."""
    rows = query_db(
        "SELECT payload FROM toolkit_outbox_body WHERE payload LIKE ?", (f"%{chat_id}%",),
    )
    payloads = [json.loads(r["payload"]) for r in rows]
    return [
        p for p in payloads
        if p.get("chat_id") == chat_id and p.get("reason") == "chat_soft_delete"
    ]


# ---------------------------------------------------------------------------
# Provider-side cleanup (mock provider, offline only)
# ---------------------------------------------------------------------------

def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("inspects the mock provider (offline mode)")


def _wait_for(predicate, what: str, timeout: float = 20.0, interval: float = 0.2):
    """Poll `predicate()` until it returns a truthy value; return that value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)


def _cleanup_status(attachment_id: str) -> str | None:
    rows = query_db("SELECT cleanup_status FROM attachments WHERE id = ?", (attachment_id,))
    assert len(rows) == 1, rows
    return rows[0]["cleanup_status"]


def _file_deletes(mock_provider) -> list[str]:
    """Paths of DELETE /files/{id} requests (not vector-store file removals)."""
    return [
        p for m, p in mock_provider.get_request_paths()
        if m == "DELETE" and "/files/" in p and "/vector_stores/" not in p
    ]


def _vector_store_rows(chat_id: str) -> list[dict]:
    return query_db(
        "SELECT vector_store_id FROM chat_vector_stores WHERE chat_id = ?", (chat_id,),
    )



def _chat_with_ready_docs(model: str, n: int) -> tuple[str, list[str]]:
    chat_id = create_chat(model)["id"]
    att_ids = []
    for i in range(n):
        resp = upload_file(chat_id, f"doc {i}".encode(), f"doc{i}.txt")
        assert resp.status_code == 201, resp.text
        att_ids.append(resp.json()["id"])
        poll_attachment_ready(chat_id, att_ids[-1])
    return chat_id, att_ids


def _wait_cleanup_terminal(att_ids: list[str]) -> dict[str, str]:
    """Wait until every attachment's cleanup_status is terminal; return them."""
    def statuses():
        st = {a: _cleanup_status(a) for a in att_ids}
        return st if all(v in ("done", "failed") for v in st.values()) else None
    return _wait_for(statuses, "terminal cleanup_status of every attachment")


def check_chat_cleanup_404_is_success(mock_provider, model: str) -> None:
    chat_id, (att_id,) = _chat_with_ready_docs(model, 1)

    mock_provider.set_fault("DELETE", "/files/", 404)
    assert delete_chat(chat_id).status_code == 204

    assert _wait_cleanup_terminal([att_id]) == {att_id: "done"}
    row = query_db("SELECT cleanup_attempts FROM attachments WHERE id = ?", (att_id,))[0]
    assert row["cleanup_attempts"] == 0, row
    # The one delete that was made got the 404.
    assert len(_file_deletes(mock_provider)) == 1, mock_provider.get_request_paths()


def check_attachment_cleanup_404_is_success(mock_provider, model: str) -> None:
    chat_id, (att_id,) = _chat_with_ready_docs(model, 1)

    mock_provider.set_fault("DELETE", "/files/", 404)
    resp = httpx.delete(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10)
    assert resp.status_code == 204

    _wait_for(lambda: _file_deletes(mock_provider), "the provider file delete", timeout=10.0)
    # The handler marks the row right after the provider answered.
    _wait_for(
        lambda: _cleanup_status(att_id) in ("done", "failed"), "terminal cleanup_status",
        timeout=5.0,
    )
    assert _cleanup_status(att_id) == "done"
    assert len(_file_deletes(mock_provider)) == 1, mock_provider.get_request_paths()


def check_vector_store_deleted_after_files(mock_provider, model: str) -> None:
    chat_id, att_ids = _chat_with_ready_docs(model, 2)
    vs_rows = _vector_store_rows(chat_id)
    assert len(vs_rows) == 1 and vs_rows[0]["vector_store_id"], vs_rows
    vs_id = vs_rows[0]["vector_store_id"]

    mock_provider.clear_captured_requests()
    assert delete_chat(chat_id).status_code == 204
    assert set(_wait_cleanup_terminal(att_ids).values()) == {"done"}
    _wait_for(lambda: not _vector_store_rows(chat_id), "chat_vector_stores row removal")

    paths = [(m, p.split("?")[0]) for m, p in mock_provider.get_request_paths()]
    file_deletes = [
        i for i, (m, p) in enumerate(paths)
        if m == "DELETE" and "/files/" in p and "/vector_stores/" not in p
    ]
    vs_deletes = [
        i for i, (m, p) in enumerate(paths)
        if m == "DELETE" and p.rstrip("/").endswith(f"/vector_stores/{vs_id}")
    ]
    assert len(file_deletes) == 2, paths
    assert len(vs_deletes) == 1, paths
    assert max(file_deletes) < vs_deletes[0], paths


# Provider-side cleanup for both storage backends (openai, azure).

class TestProviderCleanupOpenAI:
    """Provider-side cleanup of an OpenAI-backed chat (storage_backend = provider id)."""

    @pytest.fixture(autouse=True)
    def _offline_only(self, request):
        _require_offline(request)

    @pytest.mark.timeout(40)
    def test_chat_cleanup_provider_404_is_success(self, mock_provider):
        """19-03: the provider answers 404 to the file delete of a deleted chat:
        the attachment cleanup ends in `done`, not `failed` or retrying."""
        check_chat_cleanup_404_is_success(mock_provider, STANDARD_MODEL)

    @pytest.mark.timeout(40)
    def test_vector_store_deleted_after_files(self, mock_provider):
        """19-04: chat cleanup deletes every provider file before the chat
        vector store, then removes the chat_vector_stores row."""
        check_vector_store_deleted_after_files(mock_provider, STANDARD_MODEL)

    @pytest.mark.timeout(40)
    def test_attachment_cleanup_provider_404_is_success(self, mock_provider):
        """19-03: the provider answers 404 to the file delete of a deleted
        attachment: the attachment cleanup ends in `done`."""
        check_attachment_cleanup_404_is_success(mock_provider, STANDARD_MODEL)


class TestProviderCleanupAzure:
    """The same scenarios for an Azure-backed chat (storage_backend "azure")."""

    @pytest.fixture(autouse=True)
    def _offline_only(self, request):
        _require_offline(request)

    @pytest.mark.timeout(40)
    def test_chat_cleanup_provider_404_is_success(self, mock_provider):
        """19-03 (azure): see TestProviderCleanupOpenAI."""
        check_chat_cleanup_404_is_success(mock_provider, DEFAULT_MODEL)

    @pytest.mark.timeout(40)
    def test_vector_store_deleted_after_files(self, mock_provider):
        """19-04 (azure): see TestProviderCleanupOpenAI."""
        check_vector_store_deleted_after_files(mock_provider, DEFAULT_MODEL)

    @pytest.mark.timeout(40)
    def test_attachment_cleanup_provider_404_is_success(self, mock_provider):
        """19-03 (azure): see TestProviderCleanupOpenAI."""
        check_attachment_cleanup_404_is_success(mock_provider, DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCleanup:
    """Chat deletion — observable effects."""

    def test_deleted_chat_hides_chat_and_attachment(self, server):
        """After DELETE chat, the chat and its attachment return 404."""
        chat = create_chat()
        chat_id = chat["id"]

        upload_resp = upload_file(chat_id)
        assert upload_resp.status_code == 201
        attachment_id = upload_resp.json()["id"]
        att_url = f"{API_PREFIX}/chats/{chat_id}/attachments/{attachment_id}"
        assert httpx.get(att_url, timeout=10).status_code == 200

        assert delete_chat(chat_id).status_code == 204

        assert_problem(get_chat(chat_id), 404, "not_found")
        assert_problem(httpx.get(att_url, timeout=10), 404, "not_found")
        assert_problem(
            httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages", timeout=10), 404, "not_found",
        )


# ---------------------------------------------------------------------------
# DB helpers for cleanup worker scenarios
# ---------------------------------------------------------------------------

def poll_attachment_ready(chat_id: str, attachment_id: str, timeout: float = 30.0):
    """Poll until an attachment is ready; fail fast if upload failed."""
    resp = poll_until(
        lambda: httpx.get(
            f"{API_PREFIX}/chats/{chat_id}/attachments/{attachment_id}",
            timeout=10,
        ),
        until=lambda r: r.json()["status"] in ("ready", "failed"),
        timeout=timeout,
    )
    body = resp.json()
    assert body["status"] == "ready", (
        f"Attachment {attachment_id} upload failed (expected ready): {body}"
    )
    return resp


def get_attachment_rows(chat_id: str) -> list[dict]:
    """Query all attachment rows for a chat from DB."""
    return query_db(
        "SELECT id, cleanup_status, cleanup_attempts, last_cleanup_error, deleted_at "
        "FROM attachments WHERE chat_id = ?",
        (chat_id,),
    )


def get_outbox_messages(queue_name: str, limit: int = 50) -> list[dict]:
    """Query outbox messages for a given queue.

    Checks both incoming (not yet sequenced) and outgoing (sequenced) tables
    because the sequencer runs asynchronously.

    ToolKit outbox schema:
    - toolkit_outbox_partitions: queue -> partition_id mapping
    - toolkit_outbox_incoming / toolkit_outbox_outgoing: id, partition_id, body_id
    - toolkit_outbox_body: id, payload, payload_type, created_at
    """
    # Query both incoming (not yet sequenced) and outgoing (sequenced).
    outgoing = query_db(
        """
        SELECT b.payload, b.payload_type, b.created_at
        FROM toolkit_outbox_outgoing o
        JOIN toolkit_outbox_body b ON o.body_id = b.id
        JOIN toolkit_outbox_partitions p ON o.partition_id = p.id
        WHERE p.queue = ?
        ORDER BY b.created_at DESC LIMIT ?
        """,
        (queue_name, limit),
    )
    incoming = query_db(
        """
        SELECT b.payload, b.payload_type, b.created_at
        FROM toolkit_outbox_incoming i
        JOIN toolkit_outbox_body b ON i.body_id = b.id
        JOIN toolkit_outbox_partitions p ON i.partition_id = p.id
        WHERE p.queue = ?
        ORDER BY b.created_at DESC LIMIT ?
        """,
        (queue_name, limit),
    )
    return outgoing + incoming


# ---------------------------------------------------------------------------
# Cleanup worker E2E scenarios
# ---------------------------------------------------------------------------

class TestCleanupWorkerDB:
    """Cleanup worker — verify DB state transitions.

    These tests inspect the database directly to verify that:
    - Chat deletion marks attachments as cleanup_status = 'pending'
    - Chat cleanup outbox event is enqueued
    - Attachment deletion enqueues a per-attachment cleanup event
    """

    def test_chat_deletion_marks_attachments_for_cleanup(self, server):
        """DELETE chat → the attachment's cleanup ends in `done` after one
        successful provider delete (cleanup_attempts stays 0)."""
        chat = create_chat()
        chat_id = chat["id"]

        upload_resp = upload_file(chat_id)
        assert upload_resp.status_code == 201
        att_id = upload_resp.json()["id"]
        poll_attachment_ready(chat_id, att_id)
        assert _cleanup_status(att_id) is None

        assert delete_chat(chat_id).status_code == 204

        assert _wait_cleanup_terminal([att_id]) == {att_id: "done"}
        rows = get_attachment_rows(chat_id)
        assert [(r["cleanup_status"], r["cleanup_attempts"]) for r in rows] == [("done", 0)], rows

    def test_chat_deletion_enqueues_chat_cleanup_event(self, server):
        """DELETE chat → chat_cleanup outbox message is enqueued."""
        chat = create_chat()
        chat_id = chat["id"]

        # Upload an attachment (so there's work for the cleanup handler)
        upload_resp = upload_file(chat_id)
        assert upload_resp.status_code == 201
        att_id = upload_resp.json()["id"]
        poll_attachment_ready(chat_id, att_id)

        # Delete the chat
        del_resp = delete_chat(chat_id)
        assert del_resp.status_code == 204

        # The outbox body table stores ALL enqueued payloads durably,
        # regardless of processing state. Query it directly.
        import json
        all_bodies = query_db(
            "SELECT payload FROM toolkit_outbox_body ORDER BY id DESC LIMIT 50"
        )
        found = False
        for row in all_bodies:
            try:
                payload = json.loads(row["payload"])
                if payload.get("chat_id") == chat_id and payload.get("reason") == "chat_soft_delete":
                    found = True
                    assert "system_request_id" in payload
                    assert "chat_deleted_at" in payload
                    assert "tenant_id" in payload
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        assert found, (
            f"No chat_cleanup body found for chat_id={chat_id}. "
            f"Total bodies: {len(all_bodies)}"
        )

    def test_attachment_deletion_enqueues_cleanup_event(self, server):
        """DELETE attachment → per-attachment cleanup outbox event is enqueued."""
        chat = create_chat()
        chat_id = chat["id"]

        # Upload and wait for ready
        upload_resp = upload_file(chat_id)
        assert upload_resp.status_code == 201
        att_id = upload_resp.json()["id"]
        poll_attachment_ready(chat_id, att_id)

        # Delete the individual attachment
        del_resp = httpx.delete(
            f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}",
            timeout=10,
        )
        assert del_resp.status_code == 204

        # Query outbox body table directly (durable, unaffected by handler processing).
        import json
        all_bodies = query_db(
            "SELECT payload FROM toolkit_outbox_body ORDER BY id DESC LIMIT 50"
        )
        found = False
        for row in all_bodies:
            try:
                payload = json.loads(row["payload"])
                if payload.get("attachment_id") == att_id:
                    found = True
                    assert payload["event_type"] == "attachment_deleted"
                    assert payload["chat_id"] == chat_id
                    assert "provider_file_id" in payload
                    assert "storage_backend" in payload
                    break
            except (json.JSONDecodeError, KeyError):
                continue
        assert found, (
            f"No attachment_cleanup body found for attachment_id={att_id}. "
            f"Total bodies: {len(all_bodies)}"
        )

    def test_chat_deletion_with_multiple_attachments(self, server):
        """DELETE chat with 3 attachments → the cleanup of each one ends in `done`."""
        chat = create_chat()
        chat_id = chat["id"]

        att_ids = []
        for i in range(3):
            resp = upload_file(
                chat_id,
                content=f"File content {i}".encode(),
                filename=f"test_{i}.txt",
            )
            assert resp.status_code == 201
            att_ids.append(resp.json()["id"])

        # Wait for all to be ready
        for att_id in att_ids:
            poll_attachment_ready(chat_id, att_id)

        # Delete chat
        del_resp = delete_chat(chat_id)
        assert del_resp.status_code == 204

        # Every attachment's cleanup ends in `done`.
        assert _wait_cleanup_terminal(att_ids) == {a: "done" for a in att_ids}

    def test_second_delete_chat_404_single_cleanup_event(self, server):
        """A second DELETE of a chat is 404 and enqueues no second cleanup event."""
        chat = create_chat()
        chat_id = chat["id"]
        assert upload_file(chat_id).status_code == 201

        assert delete_chat(chat_id).status_code == 204
        assert_problem(delete_chat(chat_id), 404, "not_found")
        assert len(chat_cleanup_payloads(chat_id)) == 1

    def test_chat_without_attachments_still_enqueues(self, server):
        """DELETE empty chat → cleanup event still enqueued (handler handles gracefully)."""
        chat = create_chat()
        chat_id = chat["id"]

        del_resp = delete_chat(chat_id)
        assert del_resp.status_code == 204

        # Query outbox body table directly.
        import json
        all_bodies = query_db(
            "SELECT payload FROM toolkit_outbox_body ORDER BY id DESC LIMIT 50"
        )
        found = any(
            json.loads(row["payload"]).get("chat_id") == chat_id
            for row in all_bodies
            if row["payload"]
        )
        assert found, "Chat cleanup event should be enqueued even for empty chat"
