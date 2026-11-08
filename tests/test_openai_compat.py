"""The OpenAI-compatible provider adapter (Gemini & co.), exercised through the real OpenAI SDK
against a mocked HTTP transport."""

import asyncio
import dataclasses
import importlib
import json

import httpx2 as httpx  # the OpenAI SDK 3.x is built on httpx2
import openai
import pytest

from app import agent, openai_compat
from app.mock_paypal import MockPayPal
from app.store import store
from app.tools import ToolContext

FINAL = {
    "dispute_type": "Not as described - transit damage", "summary": "s", "recommendation": "ACCEPT", "win_probability": 15,
    "reasoning": "r", "key_facts": [{"fact": "Box crushed", "source": "photo", "favors": "BUYER"}], "evidence": [],
    "tracking_carrier": "UPS", "tracking_number": "1Z999AA10123456784", "refund_id": "", "offer_type": "NONE",
    "offer_amount": 0, "response_note": "n", "buyer_message": "b", "risks": [], "missing_evidence": [],
    "process_fix": "Use dividers", "buyer_language": "English",
}


def completion(message: dict, model: str = "gemini-3.5-flash-lite") -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


@pytest.fixture
def gemini(monkeypatch):
    """Point the adapter at a fake Gemini endpoint; return (requests seen, set_handler)."""
    cfg = dataclasses.replace(openai_compat.settings, ai_provider="openai_compatible", ai_model="gemini-3.5-flash-lite",
                              ai_fallback_model="gemini-3.1-flash-lite", ai_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                              ai_api_key="k", ai_reasoning_effort="")
    monkeypatch.setattr(openai_compat, "settings", cfg)
    monkeypatch.setattr(agent, "settings", cfg)
    monkeypatch.setattr(openai_compat, "_format_mode", None)
    seen, state = [], {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return state["fn"](body, len(seen))

    http = openai.DefaultAsyncHttpx2Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_compat, "_client", openai.AsyncOpenAI(base_url=cfg.ai_base_url, api_key="k", http_client=http, max_retries=0))
    return seen, state


def test_investigation_loop_with_tools_images_and_json_fallbacks(gemini):
    seen, state = gemini

    def respond(body, n):
        if n == 1:
            assert {t["function"]["name"] for t in body["tools"]} >= {"get_dispute", "view_attachment"}
            assert "additionalProperties" not in json.dumps(body["tools"])  # loosened for Gemini
            return httpx.Response(200, json=completion({"content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "get_dispute", "arguments": json.dumps({"dispute_id": "PP-D-27488"})},
                 "extra_content": {"google": {"thought_signature": "SIG1"}}},
                {"id": "t2", "type": "function", "function": {"name": "view_attachment", "arguments": json.dumps({"ref": "box-corner.png"})}},
            ]}))
        if n == 2:
            msgs = body["messages"]
            assistant = next(m for m in msgs if m["role"] == "assistant")
            assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "SIG1"  # echoed back
            assert [m["tool_call_id"] for m in msgs if m["role"] == "tool"] == ["t1", "t2"]
            image_turn = msgs[-1]
            assert image_turn["role"] == "user" and image_turn["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
            return httpx.Response(200, json=completion({"content": "DONE"}))
        if n == 3:
            assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"] is True
            return httpx.Response(400, json={"error": {"message": "Invalid JSON payload: unknown field additionalProperties"}})
        if n == 4:
            assert "additionalProperties" not in json.dumps(body["response_format"])
            broken = {k: v for k, v in FINAL.items() if k != "process_fix"}
            return httpx.Response(200, json=completion({"content": "```json\n" + json.dumps(broken) + "\n```"}))
        if n == 5:
            assert "process_fix is missing" in body["messages"][-1]["content"]
            return httpx.Response(200, json=completion({"content": json.dumps(FINAL)}))
        raise AssertionError(f"unexpected request {n}")

    state["fn"] = respond
    store.load()
    steps = []
    result = asyncio.run(agent.investigate("PP-D-27488", ToolContext(MockPayPal(), []), steps.append))
    assert result["recommendation"] == "ACCEPT" and result["process_fix"] == "Use dividers"
    assert [s["tool"] for s in steps] == ["get_dispute", "view_attachment"]
    assert result["trace"][1]["attachment"] == "/api/attachments/box-corner.png"
    assert result["engine"]["kind"] == "openai_compatible" and result["engine"]["provider"] == "Google Gemini"
    assert result["engine"]["turns"] == 4  # 400s don't count
    assert openai_compat._format_mode == "json_schema_loose"  # remembered for next time


def test_fallback_model_used_when_primary_quota_is_exhausted(gemini):
    seen, state = gemini

    def respond(body, n):
        if body["model"] == "gemini-3.5-flash-lite":
            return httpx.Response(429, json={"error": {"message": "Resource has been exhausted (e.g. check quota)."}})
        return httpx.Response(200, json=completion({"content": json.dumps({"detected_language": "Spanish", "translation": "Hello"})}, model=body["model"]))

    state["fn"] = respond
    data = asyncio.run(agent.translate("Hola"))
    assert data["translation"] == "Hello" and data["engine"]["model"] == "gemini-3.1-flash-lite"
    assert [b["model"] for b in seen] == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def test_both_models_exhausted_gives_clear_error(gemini):
    _, state = gemini
    state["fn"] = lambda body, n: httpx.Response(429, json={"error": {"message": "quota"}})
    with pytest.raises(RuntimeError, match="daily quota"):
        asyncio.run(agent.translate("Hola"))


def test_validator_and_json_extraction():
    schema = agent.ANALYSIS_SCHEMA
    assert openai_compat.validate(FINAL, schema) == []
    bad = {**FINAL, "recommendation": "MAYBE", "win_probability": "high"}
    errs = openai_compat.validate(bad, schema)
    assert any("recommendation" in e for e in errs) and any("win_probability" in e for e in errs)
    assert openai_compat.extract_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert openai_compat.extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_config_auto_detects_gemini_and_local_servers(monkeypatch):
    from app import config

    for key in ("AI_PROVIDER", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_FALLBACK_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY", "AI_CONCURRENCY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    ai = config._resolve_ai()
    assert ai["ai_provider"] == "openai_compatible" and "generativelanguage" in ai["ai_base_url"]
    assert ai["ai_model"] == "gemini-3.5-flash-lite" and ai["ai_fallback_model"] == "gemini-3.1-flash-lite" and ai["ai_concurrency"] == 1

    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:11434/v1")  # Ollama, no key needed
    monkeypatch.setenv("AI_MODEL", "llama3.2")
    ai = config._resolve_ai()
    assert ai["ai_provider"] == "openai_compatible" and ai["ai_fallback_model"] == ""

    monkeypatch.setenv("AI_BASE_URL", "https://api.groq.com/openai/v1")  # hosted, but no key -> off
    assert config._resolve_ai()["ai_provider"] == "none"
