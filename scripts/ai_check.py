"""Check your AI provider before running the app (uses about 3 requests of your quota).

    python scripts/ai_check.py

1. lists the models your key can use (so you can copy exact IDs into AI_MODEL / AI_FALLBACK_MODEL),
2. checks the configured model can call a tool,
3. checks it can return schema-valid JSON,
4. checks it can look at an image (needed for the buyer-photo feature).
"""

import asyncio
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import openai_compat  # noqa: E402
from app.config import settings  # noqa: E402


async def main() -> int:
    print(f"Provider: {settings.ai_provider}  base URL: {settings.ai_base_url or '-'}")
    if settings.ai_provider == "anthropic":
        print("Claude is configured; this check is for OpenAI-compatible providers (Gemini, Groq, ...).")
        return 0
    if settings.ai_provider != "openai_compatible":
        print("No AI configured. Set GEMINI_API_KEY (or AI_BASE_URL + AI_API_KEY + AI_MODEL) in .env.")
        return 1
    print(f"Model: {settings.ai_model}   fallback: {settings.ai_fallback_model or '-'}\n")
    client = openai_compat.client()
    ok = True

    try:
        ids = sorted(m.id.removeprefix("models/") async for m in client.models.list())
        print(f"Models available to this key ({len(ids)}):")
        for i in ids:
            mark = " <- AI_MODEL" if i == settings.ai_model else " <- AI_FALLBACK_MODEL" if i == settings.ai_fallback_model else ""
            print(f"   {i}{mark}")
        for name, model in (("AI_MODEL", settings.ai_model), ("AI_FALLBACK_MODEL", settings.ai_fallback_model)):
            if model and model not in ids:
                ok = False
                print(f"!! {name}={model} is not in the list - copy an exact ID from above into .env")
    except Exception as exc:  # some providers don't implement /models
        print(f"(Could not list models: {exc})")

    usage = openai_compat.new_usage()
    tool = [{"type": "function", "function": {"name": "get_tracking", "description": "Get carrier tracking",
             "parameters": {"type": "object", "properties": {"tracking_number": {"type": "string"}}, "required": ["tracking_number"]}}}]
    try:
        r = await openai_compat.chat([{"role": "user", "content": "Check tracking number 9400111202555842761234 using the tool."}], usage, tools=tool, tool_choice="auto", max_tokens=300)
        calls = r.choices[0].message.tool_calls or []
        print(f"\n[{'OK' if calls else 'FAIL'}] tool calling: {calls[0].function.name + calls[0].function.arguments if calls else 'model did not call the tool'}")
        ok &= bool(calls)
    except Exception as exc:
        ok = False
        print(f"\n[FAIL] tool calling: {exc}")

    schema = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["FIGHT", "ACCEPT"]}, "win_probability": {"type": "integer"}},
              "required": ["verdict", "win_probability"], "additionalProperties": False}
    try:
        data = await openai_compat.structured("You are a dispute analyst.", "Parcel was delivered and signed for. Fight or accept?", schema, max_tokens=300)
        print(f"[OK] JSON output ({openai_compat._format_mode} mode): {json.dumps({k: v for k, v in data.items() if k != 'engine'})}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] JSON output: {exc}")

    photo = ROOT / "data" / "attachments" / "mugs-colour.png"
    try:
        b64 = base64.standard_b64encode(photo.read_bytes()).decode()
        r = await openai_compat.chat([{"role": "user", "content": [
            {"type": "text", "text": "In one sentence: how many mugs are there and is one a different colour?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}], usage, max_tokens=200)
        print(f"[OK] image input: {(r.choices[0].message.content or '').strip()[:160]}")
    except Exception as exc:
        print(f"[WARN] image input not supported by this model ({exc}). The agent still works, but can't look at buyer photos.")

    print(f"\nUsed {usage['turns']} request(s). {'All good - start the app.' if ok else 'Fix the items above, then re-run.'}")
    await openai_compat.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
