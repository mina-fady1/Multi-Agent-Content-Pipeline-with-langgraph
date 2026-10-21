"""Model construction, text extraction, and per-run token accounting."""
import math
from threading import Lock
from typing import Optional, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from config.settings import settings
from models.schemas import TokenUsage


def extract_response_text(response: BaseMessage) -> str:
    """Extract display text without leaking a Python repr or reasoning blocks."""
    content = response.content
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "".join(parts)
    else:
        raise ValueError("Model response does not contain supported text content")
    if not text.strip():
        raise ValueError("Model returned an empty or non-text response")
    return text


class TokenTracker:
    """Thread-safe counters owned by one run, with model-specific estimates."""

    def __init__(self):
        self._lock = Lock()
        self.provider = settings.DEFAULT_PROVIDER
        self._usage = {}

    def reset(self, provider: Optional[str] = None) -> None:
        with self._lock:
            self.provider = provider or settings.DEFAULT_PROVIDER
            self._usage = {}

    def record_usage(self, response: BaseMessage, prompt_text: str = "", provider: Optional[str] = None,
                     model_name: Optional[str] = None) -> None:
        provider = provider or self.provider
        model_name = model_name or (settings.GEMINI_MODEL if provider == "gemini" else settings.OLLAMA_MODEL)
        metadata = getattr(response, "usage_metadata", None)
        complete = isinstance(metadata, dict) and all(
            isinstance(metadata.get(key), int) and not isinstance(metadata.get(key), bool) and metadata[key] >= 0
            for key in ("input_tokens", "output_tokens", "total_tokens")
        )
        if complete:
            prompt = metadata["input_tokens"]
            completion = metadata["output_tokens"]
            total = metadata["total_tokens"]
        else:
            prompt = math.ceil(len(prompt_text) / 4)
            try:
                completion = math.ceil(len(extract_response_text(response)) / 4)
            except ValueError:
                completion = 0
            total = prompt + completion
        with self._lock:
            counts = self._usage.setdefault((provider, model_name), {"prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "usage_estimated": False})
            counts["prompt_tokens"] += prompt
            counts["completion_tokens"] += completion
            counts["total_tokens"] += total
            counts["usage_estimated"] |= not complete

    def get_summary(self) -> TokenUsage:
        with self._lock:
            usage = {key: dict(value) for key, value in self._usage.items()}
        rows = []
        known_cost = 0.0
        complete = True
        assumptions = set()
        for (provider, model), counts in sorted(usage.items()):
            rates = (0.0, 0.0) if provider == "ollama" else settings.GEMINI_TEXT_PRICING.get(model) if provider == "gemini" else None
            cost = None
            if rates is None:
                complete = False
                assumptions.add(f"No verified pricing configured for {provider}/{model}; total cost is unknown.")
            else:
                cost = (counts["prompt_tokens"] * rates[0] + counts["completion_tokens"] * rates[1]) / 1_000_000
                known_cost += cost
                assumptions.add("Ollama estimate excludes hardware and electricity." if provider == "ollama" else
                                "Gemini standard paid text rates; excludes caching, batch discounts and external search charges.")
            rows.append({"provider": provider, "model": model, **counts,
                         "input_usd_per_million": rates[0] if rates else None,
                         "output_usd_per_million": rates[1] if rates else None,
                         "estimated_cost_usd": round(cost, 8) if cost is not None else None})
        return TokenUsage(
            prompt_tokens=sum(x["prompt_tokens"] for x in rows),
            completion_tokens=sum(x["completion_tokens"] for x in rows),
            total_tokens=sum(x["total_tokens"] for x in rows),
            estimated_cost_usd=round(known_cost, 8) if complete else None,
            pricing_complete=complete, known_cost_estimate_usd=round(known_cost, 8),
            usage_estimated=any(x["usage_estimated"] for x in rows),
            model_usage=rows, pricing_assumptions=sorted(assumptions),
        )


# Compatibility only; the orchestrator injects a separate tracker for every run.
token_tracker = TokenTracker()


def get_chat_model(provider: Optional[str] = None, model_name: Optional[str] = None,
                   temperature: float = 0.2) -> Tuple[BaseChatModel, str]:
    chosen_provider = (provider or settings.DEFAULT_PROVIDER).lower()
    if chosen_provider == "gemini":
        if not settings.GEMINI_API_KEY:
            raise ValueError("Gemini requires GEMINI_API_KEY; select --provider ollama for local execution")
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model_name or settings.GEMINI_MODEL,
                    google_api_key=settings.GEMINI_API_KEY, temperature=temperature), "gemini"
    if chosen_provider == "ollama":
        from langchain_ollama import ChatOllama
        return ChatOllama(model=model_name or settings.OLLAMA_MODEL, base_url=settings.OLLAMA_BASE_URL,
                          temperature=temperature, reasoning=False, num_ctx=8192), "ollama"
    raise ValueError(f"Unknown LLM provider: {chosen_provider}. Use 'gemini' or 'ollama'.")
