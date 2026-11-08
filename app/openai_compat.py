"""AI provider adapter for any OpenAI-compatible Chat Completions API.

That covers Google Gemini (free tier, via its OpenAI-compatible endpoint), Groq, GitHub Models,
OpenRouter, Ollama and others, so switching provider is configuration only (AI_BASE_URL, AI_API_KEY,
AI_MODEL). Behaviour mirrors the Claude path in agent.py:

  * the same 8 tools and live trace, photos passed as images after the tool results;
  * the final verdict as JSON, validated against the same schema. Providers differ in how strictly
    they support JSON-schema output, so this tries json_schema -> simplified json_schema ->
    json_object -> plain instructions, remembers what worked, and re-asks once if the JSON is invalid;
  * free-tier friendliness: SDK retries with backoff on 429, then the fallback model
    (AI_FALLBACK_MODEL, e.g. a second Gemini Flash Lite with its own daily quota).
"""

import json
import re
import time
import uuid
from urllib.parse import urlparse

import openai

from .config import settings

_client: openai.AsyncOpenAI | None = None
_format_mode: str | None = None  # remembered once a response_format style works with this provider
MODES = ("json_schema", "json_schema_loose", "json_object", "prompt")


def client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(base_url=settings.ai_base_url, api_key=settings.ai_api_key, max_retries=4, timeout=180)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def provider_name() -> str:
    host = urlparse(settings.ai_base_url).hostname or "local"
    return "Google Gemini" if "generativelanguage" in host else host


# ---- schema helpers --------------------------------------------------------------------------


def loosen(schema):
    """Drop JSON-Schema keywords some providers reject (additionalProperties, strict)."""
    if isinstance(schema, dict):
        return {k: loosen(v) for k, v in schema.items() if k not in ("additionalProperties", "strict")}
    if isinstance(schema, list):
        return [loosen(v) for v in schema]
    return schema


def validate(value, schema, path: str = "$") -> list[str]:
    """Minimal validator for the schema subset used in agent.py (object/array/string/integer/number/enum)."""
    errors: list[str] = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return [f"{path} must be an object"]
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    elif t == "array":
        if not isinstance(value, list):
            return [f"{path} must be an array"]
        for i, item in enumerate(value):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string" and not isinstance(value, str):
        errors.append(f"{path} must be a string")
    elif t == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
        if not (isinstance(value, float) and value.is_integer()):
            errors.append(f"{path} must be an integer")
    elif t == "number" and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
        errors.append(f"{path} must be a number")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    return errors


def extract_json(text: str):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _response_format(mode: str, schema: dict) -> dict | None:
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
    if mode == "json_schema_loose":
        return {"type": "json_schema", "json_schema": {"name": "result", "schema": loosen(schema)}}
    if mode == "json_object":
        return {"type": "json_object"}
    return None


# ---- transport -------------------------------------------------------------------------------


async def chat(messages: list[dict], usage: dict, **params):
    """One completion with the primary model, falling back to AI_FALLBACK_MODEL when the primary is
    rate-limited / out of daily quota (after the SDK's own retries)."""
    models = [settings.ai_model] + ([settings.ai_fallback_model] if settings.ai_fallback_model else [])
    if settings.ai_reasoning_effort:
        params["reasoning_effort"] = settings.ai_reasoning_effort
    last: Exception | None = None
    for model in models:
        try:
            resp = await client().chat.completions.create(model=model, messages=messages, **params)
        except openai.RateLimitError as exc:
            last = exc
            continue
        usage["turns"] += 1
        if resp.usage:
            usage["input_tokens"] += resp.usage.prompt_tokens or 0
            usage["output_tokens"] += resp.usage.completion_tokens or 0
        usage["model"] = resp.model or model
        return resp
    raise RuntimeError(f"AI provider rate limit / daily quota reached for {', '.join(models)}: {last}")


async def structured_from(messages: list[dict], schema: dict, usage: dict, max_tokens: int = 6000) -> dict:
    """Get a JSON object matching `schema`, adapting to what the provider supports."""
    global _format_mode
    instructions = (
        "Respond with only a JSON object (no prose, no code fences) that matches this JSON Schema exactly, "
        f"including every required field:\n{json.dumps(loosen(schema))}"
    )
    msgs = messages + [{"role": "user", "content": instructions}]
    modes = [_format_mode] if _format_mode else list(MODES)
    for i, mode in enumerate(modes):
        params = {"max_tokens": max_tokens}
        fmt = _response_format(mode, schema)
        if fmt:
            params["response_format"] = fmt
        try:
            resp = await chat(msgs, usage, **params)
        except openai.BadRequestError:
            if _format_mode or i == len(modes) - 1:
                raise
            continue  # provider rejected this response_format style; try the next one
        _format_mode = mode
        text = resp.choices[0].message.content or ""
        for attempt in range(2):
            try:
                data = extract_json(text)
                errors = validate(data, schema)
            except (json.JSONDecodeError, ValueError) as exc:
                data, errors = None, [f"not valid JSON: {exc}"]
            if not errors:
                return data
            if attempt == 1:
                break
            retry = msgs + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "That JSON is invalid: " + "; ".join(errors[:12]) + ". Return the corrected JSON object only."},
            ]
            text = (await chat(retry, usage, **params)).choices[0].message.content or ""
        raise RuntimeError(f"The model's JSON did not match the schema: {'; '.join(errors[:6])}")
    raise RuntimeError("The provider rejected every JSON output mode.")


def new_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "turns": 0, "model": settings.ai_model}


async def structured(system: str, payload, schema: dict, max_tokens: int = 6000) -> dict:
    usage = new_usage()
    content = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    data = await structured_from([{"role": "system", "content": system}, {"role": "user", "content": content}], schema, usage, max_tokens)
    data["engine"] = {"kind": "openai_compatible", "provider": provider_name(), "model": usage["model"]}
    return data


# ---- the investigation loop ------------------------------------------------------------------


def _tools(tool_defs: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": loosen(t["input_schema"])}}
        for t in tool_defs
    ]


def _args(raw: str, tool_def: dict) -> dict:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    for key in tool_def["input_schema"].get("required", []):
        args.setdefault(key, "")  # some models omit empty optional strings
    return args


async def investigate(dispute_id: str, ctx, emit, *, system_prompt: str, schema: dict, tool_defs: list[dict],
                      task: str, run_step, clean) -> dict:
    by_name = {t["name"]: t for t in tool_defs}
    messages: list[dict] = [
        {"role": "system", "content": system_prompt + "\n\nUse the tools to investigate. When you have gathered enough evidence, reply with the single word DONE (no JSON yet)."},
        {"role": "user", "content": task},
    ]
    trace: list[dict] = []
    usage = new_usage()
    started = time.monotonic()
    tools = _tools(tool_defs)

    for _ in range(12):
        resp = await chat(messages, usage, tools=tools, tool_choice="auto", max_tokens=4000)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        # Echo the assistant turn back unchanged (model_dump keeps provider extras such as Gemini's
        # thought signatures on tool calls, which multi-turn function calling needs).
        assistant = msg.model_dump(exclude_none=True)
        for key in ("annotations", "audio", "refusal", "function_call"):
            if not assistant.get(key):
                assistant.pop(key, None)  # OpenAI-only extras other providers may reject
        assistant.setdefault("content", msg.content or "")
        for call in assistant.get("tool_calls", []) or []:
            call.setdefault("id", f"call_{uuid.uuid4().hex[:12]}")
        messages.append(assistant)
        if not calls:
            break
        images = []
        for call, dumped in zip(calls, assistant["tool_calls"]):
            name = call.function.name
            if name not in by_name:
                messages.append({"role": "tool", "tool_call_id": dumped["id"], "content": f"Error: unknown tool {name}"})
                continue
            text, blob, _ = await run_step(name, _args(call.function.arguments, by_name[name]), ctx, emit, trace)
            messages.append({"role": "tool", "tool_call_id": dumped["id"], "content": text})
            if blob and blob["kind"] == "image":
                images.append((name, blob))
        if images:
            # Chat Completions only allows images in user turns, so attach them right after the results.
            parts = [{"type": "text", "text": "Here are the attachments you opened with view_attachment. Look at them carefully."}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}} for _, b in images]
            messages.append({"role": "user", "content": parts})

    messages.append({"role": "user", "content": "Now write the final analysis."})
    analysis = clean(await structured_from(messages, schema, usage, max_tokens=8000))
    analysis["engine"] = {
        "kind": "openai_compatible",
        "provider": provider_name(),
        "model": usage["model"],
        "seconds": round(time.monotonic() - started, 1),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "turns": usage["turns"],
    }
    analysis["trace"] = trace
    return analysis
