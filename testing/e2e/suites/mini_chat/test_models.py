"""Tests for the models endpoint."""

import pytest
import httpx

from .conftest import API_PREFIX, DEFAULT_MODEL, DISABLED_MODEL, STANDARD_MODEL, assert_problem


@pytest.mark.multi_provider
class TestListModels:
    """GET /v1/models"""

    def test_list_models(self, server):
        resp = httpx.get(f"{API_PREFIX}/models")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert len(body["items"]) >= 1
        assert DEFAULT_MODEL in [m["model_id"] for m in body["items"]]

    def test_catalog_models_present(self, server):
        """All models from mini-chat.yaml catalog should appear."""
        resp = httpx.get(f"{API_PREFIX}/models")
        model_ids = {m["model_id"] for m in resp.json()["items"]}
        assert DEFAULT_MODEL in model_ids
        assert STANDARD_MODEL in model_ids

    def test_model_has_required_fields(self, server):
        resp = httpx.get(f"{API_PREFIX}/models")
        for m in resp.json()["items"]:
            assert "model_id" in m
            assert "display_name" in m
            assert "tier" in m, "model must have tier"
            assert "context_window" in m, "model must have context_window"


@pytest.mark.multi_provider
class TestGetModel:
    """GET /v1/models/{model_id}"""

    def test_get_nonexistent_model(self, server):
        resp = httpx.get(f"{API_PREFIX}/models/fake-model-xyz")
        assert_problem(resp, 404, "not_found")

    def test_internal_fields_not_exposed(self, server):
        """11-06: Internal fields must not be in model response."""
        resp = httpx.get(f"{API_PREFIX}/models/{DEFAULT_MODEL}")
        assert resp.status_code == 200
        body = resp.json()
        for field in (
            "provider_id",
            "provider_model_id",
            "input_tokens_credit_multiplier_micro",
            "output_tokens_credit_multiplier_micro",
        ):
            assert field not in body, f"Internal field '{field}' exposed in model response"

    def test_extended_response_fields(self, server):
        """11-08: Model response should include extended fields."""
        resp = httpx.get(f"{API_PREFIX}/models/{DEFAULT_MODEL}")
        assert resp.status_code == 200
        body = resp.json()
        for field in ("context_window", "tier", "multimodal_capabilities", "description"):
            assert field in body, f"Extended field '{field}' missing from model response"


class TestDisabledModel:
    """A catalog entry with `enabled: false` is invisible to users."""

    def test_disabled_model_not_listed(self, server):
        ids = {m["model_id"] for m in httpx.get(f"{API_PREFIX}/models").json()["items"]}
        assert DISABLED_MODEL not in ids

    def test_get_disabled_model_404(self, server):
        assert_problem(httpx.get(f"{API_PREFIX}/models/{DISABLED_MODEL}"), 404, "not_found")
