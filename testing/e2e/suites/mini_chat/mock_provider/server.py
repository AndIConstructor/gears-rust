# Updated: 2026-04-16 by Constructor Tech
"""Mock LLM provider HTTP server — speaks OpenAI Responses API + Files API."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .responses import (
    Scenario, build_summary_response, extract_last_user_message, match_scenario,
)
from .sse_builder import build_sse_stream

_response_counter = 0
_counter_lock = threading.Lock()


def _next_response_id() -> str:
    global _response_counter
    with _counter_lock:
        _response_counter += 1
        return f"resp_mock_{_response_counter}"


class _Handler(BaseHTTPRequestHandler):
    """Handle Responses API (SSE) and Files API (JSON)."""

    def log_message(self, format, *args):
        pass

    def _log_path(self):
        server: MockProviderServer = self.server  # type: ignore[assignment]
        server.log_request_path(self.command, self.path)

    def _inject_fault(self) -> bool:
        """Answer with a fault registered via `set_fault`; True if one matched."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        fault = server.take_fault(self.command, self.path)
        if fault is None:
            return False
        status, body = fault
        self._json_response(status, body)
        return True

    def do_POST(self):
        self._log_path()
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"

        if self._inject_fault():
            return
        # Vector-store paths first: `/vector_stores/{id}/files` also contains "/files".
        if "responses" in self.path:
            self._handle_responses(raw)
        elif "/vector_stores/" in self.path and "/files" in self.path:
            self._handle_vector_store_file_add(raw)
        elif "/vector_stores" in self.path:
            self._handle_vector_store_create(raw)
        elif "/files" in self.path and "/content" not in self.path:
            self._handle_file_upload(raw)
        else:
            self.send_error(404, "Not found")

    def do_GET(self):
        self._log_path()
        if self._inject_fault():
            return
        if "/vector_stores/" in self.path and "/files/" in self.path:
            self._handle_vector_store_file_get()
        elif "/vector_stores/" in self.path:
            self._handle_vector_store_get()
        elif "/files/" in self.path and "/content" in self.path:
            self._handle_file_content()
        elif "/files/" in self.path:
            self._handle_file_get()
        else:
            self.send_error(404, "Not found")

    def do_DELETE(self):
        self._log_path()
        if self._inject_fault():
            return
        if "/vector_stores/" in self.path and "/files/" in self.path:
            self._handle_vector_store_file_delete()
        elif "/vector_stores/" in self.path:
            self._handle_vector_store_delete()
        elif "/files/" in self.path:
            self._handle_file_delete()
        else:
            self.send_error(404, "Not found")

    # ── Responses API ───────────────────────────────────────────────────

    def _handle_responses(self, raw: bytes):
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        model = body.get("model", "unknown")
        response_id = _next_response_id()

        server: MockProviderServer = self.server  # type: ignore[assignment]
        server.capture_request(body)
        if body.get("stream") is False:
            # Non-streaming requests come from background work (thread
            # summary), so they never consume a test's queued scenario.
            self._json_response(200, build_summary_response(body, model, response_id))
            return
        try:
            scenario = server._override_queue.get_nowait()
        except queue.Empty:
            user_input = extract_last_user_message(body)
            scenario = match_scenario(user_input)

        if scenario.header_delay:
            # Silent before the status line: trips the gateway's upstream
            # read timeout (OAGW `proxy_timeout_secs`).
            time.sleep(scenario.header_delay)

        # HTTP-level error — return JSON instead of SSE
        if scenario.http_error_status is not None:
            error_body = scenario.http_error_body if scenario.http_error_body is not None else {
                "error": {"message": "Mock error", "type": "mock_error"}
            }
            self._json_response(scenario.http_error_status, error_body)
            return

        sse_bytes = build_sse_stream(scenario, model, response_id, request_body=body)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        if scenario.slow or scenario.initial_delay:
            # Stream events with delays so tests can disconnect mid-stream
            try:
                self.wfile.flush()
            except BrokenPipeError:
                return
            time.sleep(scenario.initial_delay)
            for chunk in sse_bytes.split(b"\n\n"):
                if chunk:
                    try:
                        self.wfile.write(chunk + b"\n\n")
                        self.wfile.flush()
                        time.sleep(scenario.slow)
                    except BrokenPipeError:
                        return
        else:
            self.wfile.write(sse_bytes)

    # ── Files API ───────────────────────────────────────────────────────

    def _handle_file_upload(self, raw: bytes):
        """POST /v1/files — accept any upload, return a file object."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        file_id = f"file-mock-{uuid.uuid4().hex[:12]}"
        file_obj = {
            "id": file_id,
            "object": "file",
            "bytes": len(raw),
            "created_at": int(time.time()),
            "filename": "upload.bin",
            "purpose": "assistants",
            "status": "processed",
        }
        with server._state_lock:
            server._files[file_id] = file_obj
        self._json_response(200, file_obj)

    def _handle_file_get(self):
        """GET /v1/files/{file_id} — return stored file object."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        file_id = self.path.rstrip("/").split("/")[-1]
        # Strip query params
        file_id = file_id.split("?")[0]
        with server._state_lock:
            file_obj = server._files.get(file_id)
        if file_obj:
            self._json_response(200, file_obj)
        else:
            self._json_response(404, {"error": {"message": f"No such file: {file_id}"}})

    def _handle_file_content(self):
        """GET /v1/files/{file_id}/content — return dummy content."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(b"mock file content")

    def _handle_file_delete(self):
        """DELETE /v1/files/{file_id}."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        file_id = self.path.rstrip("/").split("/")[-1]
        file_id = file_id.split("?")[0]
        with server._state_lock:
            deleted = server._files.pop(file_id, None) is not None
        self._json_response(200, {"id": file_id, "object": "file", "deleted": deleted})

    # ── Vector Stores API ───────────────────────────────────────────────

    def _handle_vector_store_create(self, raw: bytes):
        """POST /v1/vector_stores — create a mock vector store."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id = f"vs_mock_{uuid.uuid4().hex[:12]}"
        vs_obj = {
            "id": vs_id,
            "object": "vector_store",
            "name": "mock-store",
            "status": "completed",
            "file_counts": {"in_progress": 0, "completed": 0, "failed": 0, "cancelled": 0, "total": 0},
            "created_at": int(time.time()),
        }
        with server._state_lock:
            server._vector_stores[vs_id] = vs_obj
        self._json_response(200, vs_obj)

    def _handle_vector_store_get(self):
        """GET /v1/vector_stores/{vs_id}."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id = self.path.rstrip("/").split("/")[-1]
        vs_id = vs_id.split("?")[0]
        with server._state_lock:
            vs_obj = server._vector_stores.get(vs_id)
        if vs_obj:
            self._json_response(200, vs_obj)
        else:
            self._json_response(404, {"error": {"message": f"No such vector_store: {vs_id}"}})

    def _handle_vector_store_delete(self):
        """DELETE /v1/vector_stores/{vs_id}."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id = self.path.rstrip("/").split("/")[-1]
        vs_id = vs_id.split("?")[0]
        with server._state_lock:
            deleted = server._vector_stores.pop(vs_id, None) is not None
        self._json_response(200, {"id": vs_id, "object": "vector_store", "deleted": deleted})

    def _vector_store_file_ids(self) -> tuple[str, str | None]:
        """(vs_id, file_id) from `/…/vector_stores/{vs_id}/files[/{file_id}]`."""
        parts = self.path.split("?")[0].rstrip("/").split("/")
        i = parts.index("vector_stores")
        vs_id = parts[i + 1]
        file_id = parts[i + 3] if len(parts) > i + 3 else None
        return vs_id, file_id

    def _vector_store_file_obj(self, vs_id: str, file_id: str) -> dict:
        return {
            "id": file_id,
            "object": "vector_store.file",
            "vector_store_id": vs_id,
            "status": "completed",
            "created_at": int(time.time()),
        }

    def _handle_vector_store_file_add(self, raw: bytes):
        """POST /v1/vector_stores/{vs_id}/files — attach a file to a vector store."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id, _ = self._vector_store_file_ids()
        try:
            file_id = json.loads(raw).get("file_id", "")
        except (json.JSONDecodeError, AttributeError):
            file_id = ""
        with server._state_lock:
            vs_obj = server._vector_stores.get(vs_id)
            if vs_obj is not None:
                vs_obj.setdefault("file_ids", []).append(file_id)
        if vs_obj is None:
            self._json_response(404, {"error": {"message": f"No such vector_store: {vs_id}"}})
            return
        self._json_response(200, self._vector_store_file_obj(vs_id, file_id))

    def _handle_vector_store_file_get(self):
        """GET /v1/vector_stores/{vs_id}/files/{file_id}."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id, file_id = self._vector_store_file_ids()
        with server._state_lock:
            vs_obj = server._vector_stores.get(vs_id)
            found = vs_obj is not None and file_id in vs_obj.get("file_ids", [])
        if found:
            self._json_response(200, self._vector_store_file_obj(vs_id, file_id))
        else:
            self._json_response(404, {"error": {"message": f"No such vector_store file: {file_id}"}})

    def _handle_vector_store_file_delete(self):
        """DELETE /v1/vector_stores/{vs_id}/files/{file_id} — detach, keep the file."""
        server: MockProviderServer = self.server  # type: ignore[assignment]
        vs_id, file_id = self._vector_store_file_ids()
        with server._state_lock:
            vs_obj = server._vector_stores.get(vs_id)
            deleted = vs_obj is not None and file_id in vs_obj.get("file_ids", [])
            if deleted:
                vs_obj["file_ids"].remove(file_id)
        self._json_response(
            200, {"id": file_id, "object": "vector_store.file.deleted", "deleted": deleted},
        )

    # ── Helpers ─────────────────────────────────────────────────────────

    def _json_response(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MockProviderServer(ThreadingHTTPServer):
    """Threaded mock LLM provider with per-test scenario override support."""

    name = "mock-provider"
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self._override_queue: queue.Queue[Scenario] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._files: dict[str, dict] = {}
        self._vector_stores: dict[str, dict] = {}
        self._captured_requests: list[dict] = []
        self._request_paths: list[tuple[str, str]] = []
        self._capture_lock = threading.Lock()
        # One-shot HTTP faults: [method, path_contains, status, body, remaining].
        self._faults: list[list] = []
        self._fault_lock = threading.Lock()
        # Guards _files and _vector_stores: handler threads outlive per-test
        # fixtures, and the in-flight delete path is not atomic.
        self._state_lock = threading.Lock()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def capture_request(self, body: dict) -> None:
        """Store a request body for later inspection (thread-safe)."""
        with self._capture_lock:
            self._captured_requests.append(body)

    def get_last_request(self) -> dict | None:
        """Return the most recent captured request body, or None."""
        with self._capture_lock:
            return self._captured_requests[-1] if self._captured_requests else None

    def get_captured_requests(self) -> list[dict]:
        """Return all captured Responses API request bodies, oldest first."""
        with self._capture_lock:
            return list(self._captured_requests)

    def log_request_path(self, method: str, path: str) -> None:
        """Record the method and path of every request (thread-safe)."""
        with self._capture_lock:
            self._request_paths.append((method, path))

    def get_request_paths(self) -> list[tuple[str, str]]:
        """Return (method, path) of every request since the last clear."""
        with self._capture_lock:
            return list(self._request_paths)

    def get_post_paths(self) -> list[str]:
        """Paths of the POST requests (uploads, vector store writes, Responses
        calls) since the last clear. Leaves out the DELETEs of a background
        cleanup that an earlier test started, which may still be running."""
        return [p for m, p in self.get_request_paths() if m == "POST"]

    def clear_captured_requests(self) -> None:
        """Clear all captured request bodies and request paths."""
        with self._capture_lock:
            self._captured_requests.clear()
            self._request_paths.clear()

    def set_fault(
        self, method: str, path_contains: str, status: int,
        body: dict | None = None, count: int = 1,
    ) -> None:
        """Answer the next `count` `method` requests whose path contains
        `path_contains` with `status` and a JSON `body`, before normal handling."""
        if body is None:
            body = {"error": {"message": f"Mock fault {status}", "type": "mock_fault"}}
        with self._fault_lock:
            self._faults.append([method.upper(), path_contains, status, body, count])

    def take_fault(self, method: str, path: str) -> tuple[int, dict] | None:
        """Consume one matching fault; return (status, body) or None."""
        with self._fault_lock:
            for fault in self._faults:
                f_method, f_path, status, body, _ = fault
                if f_method == method and f_path in path:
                    fault[4] -= 1
                    if fault[4] <= 0:
                        self._faults.remove(fault)
                    return status, body
        return None

    def clear_override_scenarios(self) -> None:
        """Drop queued per-request overrides and faults left by previous tests."""
        with self._fault_lock:
            self._faults.clear()
        while True:
            try:
                self._override_queue.get_nowait()
            except queue.Empty:
                return

    def clear_state(self) -> None:
        """Drop file/vector_store state left by previous tests (thread-safe)."""
        with self._state_lock:
            self._files.clear()
            self._vector_stores.clear()

    def set_next_scenario(self, scenario: Scenario) -> None:
        """Override the scenario for the next request (consumed once, thread-safe)."""
        self._override_queue.put(scenario)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


class _DummyMockProvider:
    """No-op stand-in used in online mode."""

    name = "mock-provider"
    port = None

    def set_next_scenario(self, scenario: Scenario) -> None:
        pass

    def set_fault(self, method: str, path_contains: str, status: int,
                  body: dict | None = None, count: int = 1) -> None:
        pass

    def get_last_request(self) -> dict | None:
        return None

    def get_captured_requests(self) -> list[dict]:
        return []

    def get_request_paths(self) -> list[tuple[str, str]]:
        return []

    def get_post_paths(self) -> list[str]:
        return []

    def clear_captured_requests(self) -> None:
        pass

    def clear_override_scenarios(self) -> None:
        pass

    def clear_state(self) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


DummyMockProvider = _DummyMockProvider
