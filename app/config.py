"""Runtime configuration, read once from environment variables (and an optional .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    paypal_base_url: str
    anthropic_model: str
    anthropic_effort: str
    use_fallbacks: bool
    merchant_name: str
    data_dir: Path
    force_mock: bool
    max_ai_runs_per_hour: int
    demo_password: str
    easypost_api_key: str
    alert_webhook_url: str
    minutes_per_case: int
    # Provider-neutral AI settings: "anthropic" (Claude) or "openai_compatible" (Gemini, Groq,
    # GitHub Models, OpenRouter, Ollama, ... anything that speaks the OpenAI Chat Completions API).
    ai_provider: str
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    ai_fallback_model: str
    ai_reasoning_effort: str
    ai_concurrency: int

    @property
    def paypal_live(self) -> bool:
        """True when real PayPal sandbox credentials are configured."""
        return bool(self.paypal_client_id and self.paypal_client_secret) and not self.force_mock

    @property
    def ai_enabled(self) -> bool:
        return self.ai_provider in ("anthropic", "openai_compatible")

    @property
    def model_label(self) -> str:
        return self.anthropic_model if self.ai_provider == "anthropic" else self.ai_model


GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _resolve_ai() -> dict:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    api_key = os.getenv("AI_API_KEY", "").strip() or gemini_key
    base_url = os.getenv("AI_BASE_URL", "").strip() or (GEMINI_OPENAI_URL if gemini_key else "")
    has_claude = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    local = "localhost" in base_url or "127.0.0.1" in base_url  # Ollama & co. need no key
    provider = os.getenv("AI_PROVIDER", "").strip().lower()
    if not provider:
        provider = "openai_compatible" if base_url and (api_key or local) else "anthropic" if has_claude else "none"
    if provider == "openai_compatible" and not (base_url and (api_key or local)):
        provider = "none"
    is_gemini = "generativelanguage.googleapis.com" in base_url
    return {
        "ai_provider": provider,
        "ai_base_url": base_url,
        "ai_api_key": api_key or "not-needed",
        # Defaults match the Gemini free tier's two 500-requests/day Flash Lite models. Model IDs
        # change over time: run `python scripts/ai_check.py` to list the exact IDs your key can use.
        "ai_model": os.getenv("AI_MODEL", "").strip() or ("gemini-3.5-flash-lite" if is_gemini else ""),
        "ai_fallback_model": os.getenv("AI_FALLBACK_MODEL", "").strip() or ("gemini-3.1-flash-lite" if is_gemini and not os.getenv("AI_MODEL") else ""),
        "ai_reasoning_effort": os.getenv("AI_REASONING_EFFORT", "").strip().lower(),
        # Free tiers allow ~15 requests/minute, so investigate one dispute at a time by default.
        "ai_concurrency": int(os.getenv("AI_CONCURRENCY", "1" if provider == "openai_compatible" else "3")),
    }


def load_settings() -> Settings:
    return Settings(
        **_resolve_ai(),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", "").strip(),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"),
        anthropic_effort=os.getenv("ANTHROPIC_EFFORT", "medium"),
        use_fallbacks=_bool("ANTHROPIC_FALLBACKS", True),
        merchant_name=os.getenv("MERCHANT_NAME", "Kiln & Clay Studio"),
        data_dir=Path(os.getenv("DATA_DIR", str(ROOT / "data"))),
        force_mock=_bool("FORCE_MOCK", False),
        max_ai_runs_per_hour=int(os.getenv("MAX_AI_RUNS_PER_HOUR", "40")),
        demo_password=os.getenv("DEMO_PASSWORD", ""),
        easypost_api_key=os.getenv("EASYPOST_API_KEY", "").strip(),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", "").strip(),
        minutes_per_case=int(os.getenv("MINUTES_PER_CASE", "45")),
    )


settings = load_settings()
