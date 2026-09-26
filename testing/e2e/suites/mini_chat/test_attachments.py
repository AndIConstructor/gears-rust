"""E2E tests for the attachment API (upload, get, delete, send-message with attachments).

Run via: ~/projects/gears-rust-worktrees/scripts/run-tests.sh tests/test_attachments.py
"""

import io
import pathlib
import struct
import uuid
import zlib

import pytest
import httpx

from .conftest import (
    API_PREFIX,
    BARE_MODEL,
    DEFAULT_MODEL,
    STANDARD_MODEL,
    SSEEvent,
    assert_problem,
    exec_db,
    expect_done,
    expect_stream_started,
    parse_sse,
    poll_until,
    query_db,
    stream_message,
    uuid_from_db,
)
from .mock_provider.responses import SCENARIOS, Scenario

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"

# Storage internals that must never appear in attachment responses.
INTERNAL_ATTACHMENT_FIELDS = ("provider_file_id", "storage_backend", "vector_store_id")


# ---------------------------------------------------------------------------
# 10-01, 10-02: Upload and get attachment
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestUploadAndGet:
    """Upload a file; the 201 response and GET return the full detail."""

    def test_upload_and_get_attachment(self, provider_chat):
        chat_id = provider_chat["id"]
        content = b"This is a test document for RAG."

        # Upload
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("notes.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        body = resp.json()
        att_id = body["id"]
        assert body["filename"] == "notes.txt"
        assert body["content_type"] == "text/plain"
        assert body["size_bytes"] == len(content)
        assert body["kind"] == "document"
        # Upload is synchronous (ADR-0007): the 201 already reports `ready`.
        assert body["status"] == "ready", body

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10)
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["id"] == att_id
        assert detail["status"] == "ready", detail
        assert (detail["filename"], detail["content_type"], detail["size_bytes"], detail["kind"]) == (
            "notes.txt", "text/plain", len(content), "document",
        )

    def test_provider_storage_fields_not_exposed(self, provider_chat):
        """Neither the upload response nor GET exposes provider storage details."""
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("notes.txt", io.BytesIO(b"internal fields check"), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, resp.text
        detail = httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{resp.json()['id']}").json()
        for body in (resp.json(), detail):
            for field in INTERNAL_ATTACHMENT_FIELDS:
                assert field not in body, f"{field} exposed: {body}"

    def test_get_nonexistent_attachment_404(self, provider_chat):
        resp = httpx.get(f"{API_PREFIX}/chats/{provider_chat['id']}/attachments/{uuid.uuid4()}")
        assert_problem(resp, 404, "not_found")


# ---------------------------------------------------------------------------
# 10-05: Unsupported MIME → 400 invalid_argument (UNSUPPORTED_CONTENT_TYPE)
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestUploadInvalidType:
    """Upload an unsupported MIME type."""

    def test_upload_invalid_type_rejected(self, provider_chat):
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("archive.zip", io.BytesIO(b"PK\x03\x04fake zip"), "application/zip")},
            timeout=60,
        )
        assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "invalid_argument" in body.get("type", ""), (
            f"Expected canonical invalid_argument type, got: {body.get('type')}"
        )
        violations = body.get("context", {}).get("field_violations", [])
        assert any(v.get("reason") == "UNSUPPORTED_CONTENT_TYPE" for v in violations), (
            f"Expected UNSUPPORTED_CONTENT_TYPE field violation, got: {violations}"
        )


# ---------------------------------------------------------------------------
# 10-03: DELETE Attachment → 204, GET → 404
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestDeleteAndVerifyGone:
    """Upload, delete, GET returns 404."""

    def test_delete_and_verify_gone(self, provider_chat):
        chat_id = provider_chat["id"]

        # Upload and wait for ready
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("gone.txt", io.BytesIO(b"delete me"), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )

        # Delete
        resp = httpx.delete(
            f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}",
            timeout=10,
        )
        assert resp.status_code == 204

        # Verify gone
        resp = httpx.get(
            f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}",
            timeout=10,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 10-04: DELETE Referenced Attachment → 409
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestDeleteReferencedAttachment:
    """Upload, attach to a message, then delete → 409."""

    def test_delete_referenced_attachment_409(self, provider_chat):
        chat_id = provider_chat["id"]

        # Upload and wait for ready
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("ref.txt", io.BytesIO(b"referenced doc"), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready"

        # Send a message with this attachment
        status, events, raw = stream_message(
            chat_id,
            "Summarize the attached file.",
            attachment_ids=[att_id],
        )
        assert status == 200, f"Stream failed: {status} {raw[:500]}"
        expect_done(events)

        # Now try to delete — should be 409 (locked by message reference)
        resp = httpx.delete(
            f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}",
            timeout=10,
        )
        body = assert_problem(resp, 409, "already_exists")
        assert body["context"]["resource_name"] == "attachment_locked"

        # The attachment is kept.
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# 10-22: Stream with Document → file_search Tool Events
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestSendMessageWithAttachments:
    """Upload 2 files, send message with attachment_ids, verify stream completes."""

    def test_send_message_with_attachments(self, provider_chat):
        chat_id = provider_chat["id"]

        # Upload two files
        att_ids = []
        for i in range(2):
            resp = httpx.post(
                f"{API_PREFIX}/chats/{chat_id}/attachments",
                files={"file": (f"doc{i}.txt", io.BytesIO(f"Document {i}: The answer is {42 + i}.".encode()), "text/plain")},
                timeout=60,
            )
            assert resp.status_code == 201, f"Upload {i} failed: {resp.status_code}"
            att_id = resp.json()["id"]
            detail = poll_until(
                lambda cid=chat_id, aid=att_id: httpx.get(f"{API_PREFIX}/chats/{cid}/attachments/{aid}", timeout=10),
                until=lambda r: r.json()["status"] in ("ready", "failed"),
            ).json()
            assert detail["status"] == "ready"
            att_ids.append(att_id)

        # Send message referencing both attachments
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What answers are in the attached documents?", "attachment_ids": att_ids},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        expect_done(events)
        ss = expect_stream_started(events)
        assert ss.data.get("message_id")


# ---------------------------------------------------------------------------
# Citation format verification (supplements 10-22 with UUID mapping check)
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
@pytest.mark.online_only
class TestUploadSearchCitationFlow:
    """Upload file, send message triggering file search, verify SSE citations contain UUID."""

    def test_upload_search_citation_flow(self, provider_chat):
        chat_id = provider_chat["id"]

        # Upload a document with distinctive content
        content = (
            b"The capital of the fictional country Zembla is Kinbote City. "
            b"It was founded in 1742 by King Charles the Beloved."
        )
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("zembla.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready"

        # Send message that should trigger file search
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What is the capital of Zembla? Use the attached document.", "attachment_ids": [att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        expect_done(events)

        # The question names the document, so the model cites it.
        citation_events = [e for e in events if e.event == "citations"]
        assert len(citation_events) == 1, [e.event for e in events]
        file_citations = [c for c in citation_events[0].data["items"] if c["source"] == "file"]
        assert file_citations, citation_events[0].data
        for c in file_citations:
            assert c["attachment_id"] == att_id, c
            assert c["title"] == "zembla.txt", c


# ---------------------------------------------------------------------------
# Azure provider: upload, get, send-message with attachments
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
@pytest.mark.online_only
class TestProviderSendMessageWithAttachment:
    """Upload a file per provider, send message, verify stream completes."""

    def test_send_message_with_attachment(self, provider_chat):
        chat_id = provider_chat["id"]

        # Upload
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("doc.txt", io.BytesIO(b"The secret code is PROVIDER-42."), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready"

        # Send message referencing the attachment
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What is the secret code in the attached document?", "attachment_ids": [att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        expect_done(events)
        ss = expect_stream_started(events)
        assert ss.data.get("message_id")


# ---------------------------------------------------------------------------
# Dual-provider: same operation on OpenAI chat vs Azure chat
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestDualProviderUpload:
    """Upload the same content to an OpenAI chat and an Azure chat.
    Proves DispatchingFileStorage routes to the correct provider-specific impl."""

    def test_dual_provider_upload(self, chat_with_model):
        content = b"Dual-provider test document content."

        # OpenAI chat (STANDARD_MODEL = gpt-5.2 → provider_id "openai")
        openai_chat = chat_with_model(STANDARD_MODEL)
        openai_chat_id = openai_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{openai_chat_id}/attachments",
            files={"file": ("dual-oa.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        oa_att_id = resp.json()["id"]
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{openai_chat_id}/attachments/{oa_att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        assert resp.json()["status"] == "ready", f"Expected ready, got: {resp.json()}"

        # Azure chat (DEFAULT_MODEL = azure-gpt-4.1-mini → provider_id "azure_openai")
        azure_chat = chat_with_model(DEFAULT_MODEL)
        azure_chat_id = azure_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{azure_chat_id}/attachments",
            files={"file": ("dual-az.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        az_att_id = resp.json()["id"]
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{azure_chat_id}/attachments/{az_att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        assert resp.json()["status"] == "ready", f"Expected ready, got: {resp.json()}"


@pytest.mark.multi_provider
@pytest.mark.online_only
class TestDualProviderRAGStream:
    """Upload + send message on both OpenAI and Azure chats.
    Proves end-to-end RAG (file_search) works through both provider-specific
    file + vector store implementations in the same server instance."""

    def test_dual_provider_rag_stream(self, chat_with_model):
        content = b"The secret passphrase is DUAL-PROVIDER-42."
        question = "What is the secret passphrase in the attached document?"

        # OpenAI chat (STANDARD_MODEL = gpt-5.2) — routes through OpenAiFileStorage + OpenAiVectorStore
        openai_chat = chat_with_model(STANDARD_MODEL)
        openai_chat_id = openai_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{openai_chat_id}/attachments",
            files={"file": ("rag-oa.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        oa_att_id = resp.json()["id"]
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{openai_chat_id}/attachments/{oa_att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        assert resp.json()["status"] == "ready", f"Expected ready, got: {resp.json()}"
        resp = httpx.post(
            f"{API_PREFIX}/chats/{openai_chat_id}/messages:stream",
            json={"content": question, "attachment_ids": [oa_att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        oa_events = parse_sse(resp.text)
        ss = expect_stream_started(oa_events)
        assert ss.data.get("message_id")
        done = expect_done(oa_events)
        usage = done.data.get("usage", {})
        assert usage.get("input_tokens", 0) > 0, "Expected non-zero input_tokens"
        assert usage.get("output_tokens", 0) > 0, "Expected non-zero output_tokens"

        # Azure chat (DEFAULT_MODEL = azure-gpt-4.1-mini) — routes through AzureFileStorage + AzureVectorStore
        azure_chat = chat_with_model(DEFAULT_MODEL)
        azure_chat_id = azure_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{azure_chat_id}/attachments",
            files={"file": ("rag-az.txt", io.BytesIO(content), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        az_att_id = resp.json()["id"]
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{azure_chat_id}/attachments/{az_att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        assert resp.json()["status"] == "ready", f"Expected ready, got: {resp.json()}"
        resp = httpx.post(
            f"{API_PREFIX}/chats/{azure_chat_id}/messages:stream",
            json={"content": question, "attachment_ids": [az_att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        az_events = parse_sse(resp.text)
        ss = expect_stream_started(az_events)
        assert ss.data.get("message_id")
        done = expect_done(az_events)
        usage = done.data.get("usage", {})
        assert usage.get("input_tokens", 0) > 0, "Expected non-zero input_tokens"
        assert usage.get("output_tokens", 0) > 0, "Expected non-zero output_tokens"


# ---------------------------------------------------------------------------
# Helpers — minimal valid PNG
# ---------------------------------------------------------------------------

def make_minimal_png(width: int = 2, height: int = 2, color: tuple = (255, 0, 0)) -> bytes:
    """Generate a minimal valid PNG image (solid color, no external deps)."""
    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    # IHDR: width, height, bit depth 8, color type 2 (RGB)
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    # Raw image data: filter byte 0 + RGB pixels per row
    raw = b""
    for _ in range(height):
        raw += b"\x00" + bytes(color) * width
    idat_data = zlib.compress(raw)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr_data)
        + chunk(b"IDAT", idat_data)
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# Image upload and recognition
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestImageUploadAndSend:
    """Upload a PNG image, verify it reaches ready, send a message referencing it."""

    def test_image_upload_and_send(self, provider_chat, mock_provider):
        chat_id = provider_chat["id"]

        # Generate a small red PNG
        png_bytes = make_minimal_png(width=4, height=4, color=(255, 0, 0))

        # Upload
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("red.png", io.BytesIO(png_bytes), "image/png")},
            timeout=60,
        )
        assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
        body = resp.json()
        att_id = body["id"]
        assert body["kind"] == "image", f"Expected image kind, got: {body['kind']}"
        assert body["content_type"] == "image/png"

        # Poll until ready
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready", f"Expected ready, got: {detail}"
        assert detail["img_thumbnail"] is not None, "ready image must have a thumbnail"
        # An image is not indexed for file_search: no vector store is created.
        vector_store_calls = [p for p in mock_provider.get_request_paths() if "/vector_stores" in p[1]]
        assert vector_store_calls == []

        # Send a message referencing the image
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Describe the attached image. What color is it?", "attachment_ids": [att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        expect_done(events)
        ss = expect_stream_started(events)
        assert ss.data.get("message_id"), "Expected message_id in stream_started event"

        # Collect delta text to see what the LLM said
        delta_text = ""
        for ev in events:
            if ev.event == "delta" and isinstance(ev.data, dict):
                delta_text += ev.data.get("content", "")

        # The LLM should have produced some response
        assert len(delta_text) > 0, "Expected non-empty response from LLM"


@pytest.mark.multi_provider
@pytest.mark.online_only
class TestImageRecognition:
    """Upload a real cat photo (JPEG) per provider, ask the LLM what animal
    it is, verify the stream completes and the cat is recognized.

    Image inlining is wired — the LLM sees the image as multimodal input
    via the Responses API. The test hard-asserts cat recognition.
    """

    @staticmethod
    def _load_cat_image() -> bytes:
        cat_path = FIXTURES_DIR / "cat.jpg"
        assert cat_path.exists(), f"Fixture not found: {cat_path}"
        return cat_path.read_bytes()

    @staticmethod
    def _upload_image_and_ask(chat_id: str, image_bytes: bytes, filename: str,
                              content_type: str, provider_label: str):
        """Upload an image, poll until ready, send a question, check response."""
        # Upload
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": (filename, io.BytesIO(image_bytes), content_type)},
            timeout=60,
        )
        assert resp.status_code == 201, f"[{provider_label}] Upload failed: {resp.status_code} {resp.text}"
        body = resp.json()
        att_id = body["id"]
        assert body["kind"] == "image"

        # Poll until ready
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        detail = resp.json()
        assert detail["status"] == "ready", f"[{provider_label}] Expected ready, got: {detail}"

        # Ask the LLM to identify the animal
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={
                "content": "Describe exactly what you see in the attached image. If you cannot see any image, respond with exactly 'NO_IMAGE_VISIBLE'.",
                "attachment_ids": [att_id],
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"[{provider_label}] Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        done = expect_done(events)

        # Collect response text
        delta_text = ""
        for ev in events:
            if ev.event == "delta" and isinstance(ev.data, dict):
                delta_text += ev.data.get("content", "")

        assert len(delta_text) > 0, f"[{provider_label}] Expected non-empty response"

        # The LLM must see the image — if it responds with NO_IMAGE_VISIBLE
        # or doesn't mention a cat, image inlining is broken.
        response_lower = delta_text.lower()
        assert "no_image_visible" not in response_lower, (
            f"[{provider_label}] LLM cannot see the image — file_id not included "
            f"as multimodal input in the Responses API request (image inlining gap). "
            f"Response: {delta_text!r}"
        )
        recognized = any(w in response_lower for w in ("cat", "kitten", "feline"))
        assert recognized, (
            f"[{provider_label}] LLM responded but did not recognize the cat. "
            f"Response: {delta_text!r}"
        )
        assert detail["img_thumbnail"] is not None, f"[{provider_label}] ready image must have a thumbnail"

    def test_image_recognition_cat(self, provider_chat):
        cat_bytes = self._load_cat_image()
        self._upload_image_and_ask(provider_chat["id"], cat_bytes, "cat.jpg", "image/jpeg", provider_chat.get("model", "unknown"))


# ---------------------------------------------------------------------------
# Mixed document + image: both mechanisms must work simultaneously
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
@pytest.mark.online_only
class TestDocumentAndImageTogether:
    """Upload a document AND an image, send a message referencing both.

    The LLM must use file_search to read the document AND see the image
    via multimodal input. The question is designed so the correct answer
    requires information from BOTH sources.
    """

    def test_document_and_image_combined(self, provider_chat):
        chat_id = provider_chat["id"]

        # 1. Upload a document with a secret code word
        doc_content = (
            "CONFIDENTIAL REPORT\n"
            "The secret code word for this project is: FLAMINGO.\n"
            "Do not share this code word with anyone.\n"
        )
        doc_resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("secret-report.txt", io.BytesIO(doc_content.encode()), "text/plain")},
            timeout=60,
        )
        assert doc_resp.status_code == 201
        doc_id = doc_resp.json()["id"]
        doc_detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{doc_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert doc_detail["status"] == "ready"
        assert doc_detail["kind"] == "document"

        # 2. Upload the cat image
        cat_bytes = (pathlib.Path(__file__).parent / "fixtures" / "cat.jpg").read_bytes()
        img_resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("animal.jpg", io.BytesIO(cat_bytes), "image/jpeg")},
            timeout=60,
        )
        assert img_resp.status_code == 201
        img_id = img_resp.json()["id"]
        img_detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{img_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert img_detail["status"] == "ready"
        assert img_detail["kind"] == "image"

        # 3. Ask a question that requires BOTH sources
        #    - The document contains the code word "FLAMINGO"
        #    - The image contains a cat
        #    The LLM must mention both to prove it accessed both.
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={
                "content": (
                    "I attached a document and an image. "
                    "Tell me: 1) What is the secret code word from the document? "
                    "2) What animal is in the image? "
                    "Answer both questions."
                ),
                "attachment_ids": [doc_id, img_id],
            },
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text[:500]}"
        events = parse_sse(resp.text)
        done = expect_done(events)

        # Collect response text
        delta_text = ""
        for ev in events:
            if ev.event == "delta" and isinstance(ev.data, dict):
                delta_text += ev.data.get("content", "")

        assert len(delta_text) > 0, "Expected non-empty response"
        response_lower = delta_text.lower()

        # Must recognize the cat from the image (multimodal input) — hard assert
        has_cat = any(w in response_lower for w in ("cat", "kitten", "feline"))
        assert has_cat, (
            f"LLM did not recognize the cat from the image. "
            f"Image inlining (input_image) may not be working. Response: {delta_text!r}"
        )

        # Must mention the code word from the document (file_search).
        assert "flamingo" in response_lower, (
            f"LLM did not find 'FLAMINGO' from the document (file_search). "
            f"Response: {delta_text!r}"
        )


# ---------------------------------------------------------------------------
# Streaming upload: size enforcement and size_bytes accuracy
# ---------------------------------------------------------------------------

@pytest.mark.multi_provider
class TestUploadSizeEnforcement:
    """Upload size limit enforcement — files exceeding the configured limit
    are rejected with HTTP 400 ``out_of_range`` (FILE_TOO_LARGE).

    NOTE: these tests rely on the server's default config limits:
    - ``uploaded_file_max_size_kb``: 25600 (25 MB) for documents
    - ``uploaded_image_max_size_kb``: 5120 (5 MB) for images
    """

    def test_oversize_image_rejected(self, provider_chat):
        """Upload an image exceeding uploaded_image_max_size_kb (5 MB) → 400.

        Uses ~6 MB which is over the image limit but under the API gateway's
        global 16 MiB body limit, so our handler's streaming size check runs.
        """
        chat_id = provider_chat["id"]
        # 6 MB > 5 MB default image limit, but < 16 MiB gateway limit
        oversize_payload = b"\x89PNG" + b"\x00" * (6 * 1024 * 1024)
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("huge.png", io.BytesIO(oversize_payload), "image/png")},
            timeout=60,
        )
        assert resp.status_code == 400, (
            f"Expected 400 for oversize image, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "out_of_range" in body.get("type", ""), (
            f"Expected canonical out_of_range type, got: {body.get('type')}"
        )
        violations = body.get("context", {}).get("field_violations", [])
        assert any(v.get("reason") == "FILE_TOO_LARGE" for v in violations), (
            f"Expected FILE_TOO_LARGE field violation, got: {violations}"
        )

    def test_oversize_document_rejected(self, provider_chat):
        """Upload a document exceeding the per-kind handler limit (25 MB) → 400.

        Documents are capped at 25 MB by the per-kind handler size check.
        A 26 MB upload should be rejected by that handler before any further
        processing occurs.
        """
        chat_id = provider_chat["id"]
        # 26 MB > 25 MB per-kind handler limit
        oversize_payload = b"\x00" * (26 * 1024 * 1024)
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("huge.pdf", io.BytesIO(oversize_payload), "application/pdf")},
            timeout=60,
        )
        assert resp.status_code == 400, (
            f"Expected 400 for oversize document, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "out_of_range" in body.get("type", ""), (
            f"Expected canonical out_of_range type, got: {body.get('type')}"
        )
        violations = body.get("context", {}).get("field_violations", [])
        assert any(v.get("reason") == "FILE_TOO_LARGE" for v in violations), (
            f"Expected FILE_TOO_LARGE field violation, got: {violations}"
        )

    def test_document_within_limit_succeeds(self, provider_chat):
        """Upload a document just under the limit → succeeds."""
        chat_id = provider_chat["id"]
        # 1 MB — well under 25 MB
        payload = b"x" * (1 * 1024 * 1024)
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("medium.txt", io.BytesIO(payload), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201, (
            f"Expected 201 for within-limit doc, got {resp.status_code}: {resp.text}"
        )
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready"


@pytest.mark.multi_provider
class TestUploadSizeBytesAccuracy:
    """Verify that size_bytes in the attachment metadata matches the actual
    uploaded file size."""

    def test_size_bytes_matches_actual(self, provider_chat):
        """Upload a file of known size, verify size_bytes in GET response."""
        chat_id = provider_chat["id"]
        # Use a specific, non-round size to catch off-by-one issues
        payload = b"A" * 123_456
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("sized.txt", io.BytesIO(payload), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        detail = resp.json()
        assert detail["status"] == "ready"
        assert detail["size_bytes"] == 123_456, (
            f"Expected size_bytes=123456, got {detail['size_bytes']}"
        )


@pytest.mark.multi_provider
class TestUploadStreamingPipeline:
    """End-to-end test with a medium-sized file (~500 KB) through the full
    streaming upload pipeline: upload → ready → send message → SSE done."""

    @pytest.mark.online_only
    def test_medium_file_upload_and_stream(self, provider_chat):
        chat_id = provider_chat["id"]
        # 500 KB document
        payload = b"The quick brown fox. " * 25_000  # ~500 KB
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("medium_doc.txt", io.BytesIO(payload), "text/plain")},
            timeout=60,
        )
        assert resp.status_code == 201
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        ).json()
        assert detail["status"] == "ready", f"Expected ready, got: {detail}"

        # Send a message referencing the attachment
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Summarize the attached document briefly.", "attachment_ids": [att_id]},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200, f"Stream failed: {resp.status_code} {resp.text}"
        events = parse_sse(resp.text)

        expect_stream_started(events)
        done = expect_done(events)
        assert done.data["usage"]["input_tokens"] > 0


# ---------------------------------------------------------------------------
# Helpers for limit and failure scenarios
# ---------------------------------------------------------------------------

# RagConfig defaults (gears/mini-chat/mini-chat/src/config.rs), not overridden
# in config/base.yaml.
MAX_IMAGES_PER_MESSAGE = 4
MAX_DOCUMENTS_PER_CHAT = 50
MAX_TOTAL_UPLOAD_BYTES_PER_CHAT = 100 * 1_048_576
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024  # uploaded_file_max_size_kb 25600


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("uses the mock provider or seeds the DB (offline mode)")


def _upload(chat_id: str, filename: str, payload: bytes, content_type: str) -> httpx.Response:
    return httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/attachments",
        files={"file": (filename, io.BytesIO(payload), content_type)},
        timeout=60,
    )


def _upload_ready(chat_id: str, filename: str, payload: bytes, content_type: str) -> str:
    """Upload a file and wait until it is ready; return the attachment id."""
    resp = _upload(chat_id, filename, payload, content_type)
    assert resp.status_code == 201, f"upload failed: {resp.status_code} {resp.text}"
    att_id = resp.json()["id"]
    if resp.json()["status"] != "ready":
        resp = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
        )
        assert resp.json()["status"] == "ready", resp.json()
    return att_id


def _clone_attachment(src_id: str, *, size_bytes: int | None = None) -> str:
    """Insert a copy of attachment row `src_id` under a new id and a new
    provider file id (each upload has its own provider file); return the id."""
    cols = [r["name"] for r in query_db("PRAGMA table_info(attachments)")]
    exprs = []
    params: list = []
    new_id = str(uuid.uuid4())
    overrides = {"id": new_id, "provider_file_id": f"file-seed-{uuid.uuid4().hex[:12]}"}
    if size_bytes is not None:
        overrides["size_bytes"] = size_bytes
    for col in cols:
        if col in overrides:
            exprs.append("?")
            params.append(overrides[col])
        else:
            exprs.append(col)
    params.append(src_id)
    inserted = exec_db(
        f"INSERT INTO attachments ({', '.join(cols)}) "
        f"SELECT {', '.join(exprs)} FROM attachments WHERE id = ?",
        tuple(params),
    )
    assert inserted == 1
    return new_id


def _file_upload_calls(mock_provider) -> list[tuple[str, str]]:
    """POST /files requests (provider file uploads) seen by the mock."""
    return [
        (m, p) for m, p in mock_provider.get_request_paths()
        if m == "POST" and p.split("?")[0].endswith("/files") and "/vector_stores/" not in p
    ]


def _chunked_multipart(filename: str, content_type: str, payload: bytes,
                       chunk_size: int = 64 * 1024):
    """A multipart body as a generator (httpx sends it chunked, without
    Content-Length); returns (Content-Type header, body generator)."""
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        for i in range(0, len(payload), chunk_size):
            yield payload[i:i + chunk_size]
        yield tail

    return f"multipart/form-data; boundary={boundary}", body()


def _upload_chunked(chat_id: str, filename: str, payload: bytes, content_type: str) -> httpx.Response:
    ct, body = _chunked_multipart(filename, content_type, payload)
    resp = httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/attachments",
        content=body, headers={"Content-Type": ct}, timeout=60,
    )
    assert resp.request.headers.get("transfer-encoding") == "chunked"
    assert "content-length" not in resp.request.headers
    return resp


# ---------------------------------------------------------------------------
# 04-11: Too many images in one message
# ---------------------------------------------------------------------------

class TestTooManyImages:
    """More ready images than `max_images_per_message` in one message."""

    def test_too_many_images_rejected(self, request, chat, mock_provider):
        """04-11: max_images_per_message + 1 images → 400 out_of_range
        TOO_MANY_IMAGES; the provider is not called."""
        _require_offline(request)
        chat_id = chat["id"]  # vision-capable default model
        att_ids = [
            _upload_ready(chat_id, f"img{i}.png", make_minimal_png(color=(i * 40, 0, 0)), "image/png")
            for i in range(MAX_IMAGES_PER_MESSAGE + 1)
        ]

        mock_provider.clear_captured_requests()
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Compare these images.", "attachment_ids": att_ids},
            headers={"Accept": "text/event-stream"}, timeout=30,
        )
        assert_problem(resp, 400, "out_of_range", field_reason="TOO_MANY_IMAGES")
        assert mock_provider.get_captured_requests() == [], "provider must not be called"


# ---------------------------------------------------------------------------
# 10-16: provider upload failure → 503, attachment failed with error_code
# ---------------------------------------------------------------------------

class TestUploadProviderFailure:
    """The provider Files API fails during the upload."""

    def test_upload_failure_marks_attachment_failed(self, request, chat, mock_provider):
        """10-16: provider 500 on POST /files → 503 service_unavailable with
        Retry-After; the inserted row is visible with status failed and an error_code."""
        _require_offline(request)
        chat_id = chat["id"]
        mock_provider.set_fault("POST", "/files", 500)

        resp = _upload(chat_id, "fail.txt", b"provider will fail", "text/plain")
        assert_problem(resp, 503, "service_unavailable")
        assert resp.headers.get("Retry-After", "").isdigit(), resp.headers

        # The Problem carries no attachment id; the row is found in the DB.
        rows = query_db("SELECT id FROM attachments WHERE chat_id = ?", (chat_id,))
        assert len(rows) == 1, rows
        att_id = uuid_from_db(rows[0]["id"])
        detail = httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["status"] == "failed", body
        assert body.get("error_code"), body


# ---------------------------------------------------------------------------
# 10-17, 10-19: chunked uploads (no Content-Length) — streaming size counter
# ---------------------------------------------------------------------------

class TestChunkedUpload:
    """Uploads without Content-Length are measured while streaming."""

    def test_chunked_oversize_image_rejected(self, request, chat, mock_provider):
        """10-17: a chunked image over uploaded_image_max_size_kb (5 MB) passes
        the Content-Length pre-check and is stopped by the streaming counter:
        400 out_of_range FILE_TOO_LARGE; nothing reaches the provider."""
        _require_offline(request)
        payload = b"\x89PNG" + b"\x00" * (MAX_IMAGE_BYTES - 4 + 1)
        resp = _upload_chunked(chat["id"], "big.png", payload, "image/png")
        assert_problem(resp, 400, "out_of_range", field_reason="FILE_TOO_LARGE")
        assert _file_upload_calls(mock_provider) == []

    def test_chunked_upload_within_limit_ready(self, request, chat):
        """10-19: a chunked document within the limit → 201, ready, exact size_bytes."""
        _require_offline(request)
        payload = b"chunked upload line\n" * 10_000  # 200 KB
        resp = _upload_chunked(chat["id"], "chunked.txt", payload, "text/plain")
        assert resp.status_code == 201, f"{resp.status_code} {resp.text}"
        body = resp.json()
        assert body["status"] == "ready", body
        assert body["size_bytes"] == len(payload)


# ---------------------------------------------------------------------------
# 10-26, 10-27: per-chat document count and storage limits
# ---------------------------------------------------------------------------

class TestPerChatLimits:
    """Per-chat limits, reached by seeding attachment rows in the DB."""

    def test_document_limit_exceeded(self, request, chat, mock_provider):
        """10-26: a chat with max_documents_per_chat (50) documents rejects the
        next document: 429 resource_exhausted, violation document_limit."""
        _require_offline(request)
        chat_id = chat["id"]
        src = _upload_ready(chat_id, "seed.txt", b"seed document", "text/plain")
        for _ in range(MAX_DOCUMENTS_PER_CHAT - 1):
            _clone_attachment(src)

        mock_provider.clear_captured_requests()
        resp = _upload(chat_id, "one-more.txt", b"over the limit", "text/plain")
        assert_problem(resp, 429, "resource_exhausted", violation_subject="document_limit")
        assert _file_upload_calls(mock_provider) == []

    def test_storage_limit_exceeded(self, request, chat, mock_provider):
        """10-27: a chat whose attachments already total max_total_upload_mb_per_chat
        (100 MB, as 4 files at the 25 MB per-file limit) rejects the next
        upload: 429 resource_exhausted, violation storage_limit."""
        _require_offline(request)
        chat_id = chat["id"]
        src = _upload_ready(chat_id, "seed.txt", b"seed document", "text/plain")
        for _ in range(MAX_TOTAL_UPLOAD_BYTES_PER_CHAT // MAX_DOCUMENT_BYTES):
            _clone_attachment(src, size_bytes=MAX_DOCUMENT_BYTES)

        mock_provider.clear_captured_requests()
        resp = _upload(chat_id, "one-more.txt", b"over the limit", "text/plain")
        assert_problem(resp, 429, "resource_exhausted", violation_subject="storage_limit")
        assert _file_upload_calls(mock_provider) == []


# ---------------------------------------------------------------------------
# 05-07, 10-34, 01-06: citations and images in the provider exchange
# ---------------------------------------------------------------------------

def _provider_file_id(attachment_id: str) -> str:
    rows = query_db("SELECT provider_file_id FROM attachments WHERE id = ?", (attachment_id,))
    assert len(rows) == 1 and rows[0]["provider_file_id"], rows
    return rows[0]["provider_file_id"]


def _file_search_scenario(citations: list[dict]) -> Scenario:
    """The mock `FILESEARCH:*` scenario with the given file citations."""
    base = SCENARIOS["FILESEARCH:*"]
    return Scenario(events=list(base.events), usage=base.usage, citations=citations)


class TestFileCitationMapping:
    """File citations reach the client with the attachment id, not the provider file id."""

    def test_file_citation_maps_to_attachment_id(self, request, chat, mock_provider):
        """05-07: a `file_citation` for the provider file of an attachment is sent
        as `{source: file, attachment_id: <attachment UUID>, title: <filename>}`;
        a citation of an unknown provider file is dropped; no provider file id
        appears in the stream."""
        _require_offline(request)
        chat_id = chat["id"]
        att_id = _upload_ready(chat_id, "zembla.txt", b"Zembla notes.", "text/plain")
        file_id = _provider_file_id(att_id)
        mock_provider.set_next_scenario(_file_search_scenario([
            {"type": "file_citation", "file_id": file_id, "title": "provider-name.txt",
             "start_index": 0, "end_index": 13, "text": "Based on docs"},
            {"type": "file_citation", "file_id": "file-unknown-0123456789",
             "title": "other.txt", "start_index": 0, "end_index": 5, "text": "Based"},
        ]))

        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "FILESEARCH: what is in the notes?"},
            headers={"Accept": "text/event-stream"}, timeout=30,
        )
        assert resp.status_code == 200, resp.text
        events = parse_sse(resp.text)
        expect_done(events)
        citations = [e.data for e in events if e.event == "citations"]
        assert citations == [{"items": [{
            "source": "file",
            "title": "zembla.txt",
            "attachment_id": att_id,
            "snippet": "Based on docs",
            "span": {"start": 0, "end": 13},
        }]}]
        assert file_id not in resp.text


class TestImageInProviderRequest:
    """Image attachments in the provider request, and on a model without vision."""

    def test_image_sent_as_input_image(self, request, chat, mock_provider):
        """10-34 (offline part): the user message of the provider request carries
        the text followed by `input_image` with the image's provider file id."""
        _require_offline(request)
        chat_id = chat["id"]  # vision-capable default model
        att_id = _upload_ready(chat_id, "red.png", make_minimal_png(color=(255, 0, 0)), "image/png")
        file_id = _provider_file_id(att_id)

        mock_provider.clear_captured_requests()
        status, events, raw = stream_message(
            chat_id, "What color is the image?", attachment_ids=[att_id],
        )
        assert status == 200, raw
        expect_done(events)

        captured = mock_provider.get_captured_requests()
        assert len(captured) == 1, captured
        user_items = [i for i in captured[0]["input"] if i.get("role") == "user"]
        assert user_items[-1]["content"] == [
            {"type": "input_text", "text": "What color is the image?"},
            {"type": "input_image", "file_id": file_id},
        ]

    def test_image_on_model_without_vision_400(self, request, chat_with_model, mock_provider):
        """01-06: an image attachment in a chat whose model has no VISION_INPUT
        (gpt-5-bare) is 400 invalid_argument VISION_NOT_SUPPORTED; no turn is
        created and the provider is not called."""
        _require_offline(request)
        chat_id = chat_with_model(BARE_MODEL)["id"]
        att_id = _upload_ready(chat_id, "red.png", make_minimal_png(), "image/png")

        mock_provider.clear_captured_requests()
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Describe the image.", "attachment_ids": [att_id]},
            headers={"Accept": "text/event-stream"}, timeout=30,
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="VISION_NOT_SUPPORTED")
        assert mock_provider.get_captured_requests() == []
        assert query_db("SELECT id FROM chat_turns WHERE chat_id = ?", (chat_id,)) == []


# ---------------------------------------------------------------------------
# DELETE of an unknown attachment
# ---------------------------------------------------------------------------

class TestDeleteMissingAttachment:
    """DELETE /attachments/{id} for an attachment that does not exist."""

    def test_delete_unknown_attachment_404(self, chat):
        resp = httpx.delete(f"{API_PREFIX}/chats/{chat['id']}/attachments/{uuid.uuid4()}")
        assert_problem(resp, 404, "not_found")
