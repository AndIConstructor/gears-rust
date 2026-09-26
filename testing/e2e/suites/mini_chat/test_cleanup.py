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

import httpx

from .conftest import (
    API_PREFIX,
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
        """DELETE chat → all attachments get cleanup_status set (not NULL)."""
        chat = create_chat()
        chat_id = chat["id"]

        # Upload an attachment and wait until it's ready
        upload_resp = upload_file(chat_id)
        assert upload_resp.status_code == 201
        att_id = upload_resp.json()["id"]
        poll_attachment_ready(chat_id, att_id)

        # Delete the chat
        del_resp = delete_chat(chat_id)
        assert del_resp.status_code == 204

        # Verify: attachment cleanup_status was set by the delete TX.
        # It may already be 'done' if the outbox handler processed it quickly.
        rows = get_attachment_rows(chat_id)
        assert len(rows) >= 1, f"Expected at least 1 attachment row, got {len(rows)}"
        for row in rows:
            assert row["cleanup_status"] in ("pending", "done", "failed"), (
                f"Attachment {row['id']} should have cleanup_status set, "
                f"got {row['cleanup_status']!r}"
            )

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
        """DELETE chat with 3 attachments → all get cleanup_status set."""
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

        # All 3 attachments should have cleanup_status set (may already be 'done')
        rows = get_attachment_rows(chat_id)
        cleanup_rows = [r for r in rows if r["cleanup_status"] in ("pending", "done", "failed")]
        assert len(cleanup_rows) == 3, (
            f"Expected 3 attachments with cleanup_status set, got {len(cleanup_rows)} "
            f"(total rows: {len(rows)})"
        )

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
