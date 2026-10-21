import re
from typing import Optional

from langchain_core.messages import HumanMessage

from agents.llm_factory import extract_response_text, get_chat_model, token_tracker
from config.settings import settings
from models.schemas import Slot


class MarketingWriterAgent:
    """Write one complete post grounded in the supplied research and policies."""

    def __init__(self, provider: Optional[str] = None, model_name: Optional[str] = None, tracker=None):
        self.provider = provider
        self.model_name = model_name
        self.tracker = tracker if tracker is not None else token_tracker
        self._load_instructions()

    def _load_instructions(self) -> None:
        inst_file = settings.BASE_DIR / "instructions" / "marketing_writer.md"
        self.instructions = inst_file.read_text(encoding="utf-8") if inst_file.exists() else ""
        bv_file = settings.BRAND_VOICE_FILE
        self.brand_voice = bv_file.read_text(encoding="utf-8") if bv_file.exists() else ""
        mr_file = settings.MARKETING_RULES_FILE
        self.marketing_rules = mr_file.read_text(encoding="utf-8") if mr_file.exists() else ""

    def write(self, slot: Slot, feedback: Optional[str] = None) -> Slot:
        source_context = (
            f"Service Line: {slot.service}\nTopic: {slot.resolved_topic}\n"
            f"Source: {slot.source_url or 'Knowledge Base ' + (slot.source_id or '')}\n"
            f"Research Evidence: {slot.source_summary}\n"
        )
        revision = ""
        if feedback:
            revision = (
                f"\n--- PREVIOUS DRAFT ---\n{slot.draft_content or ''}\n"
                f"--- REQUIRED REVISION ---\n{feedback}\n"
                "Rewrite the complete post, fixing every issue. Preserve required citations.\n"
                "CRITICAL: If the previous draft was over 280 characters, shorten it to a single short sentence (under 90 characters) before the URL so text + URL is under 220 characters.\n"
            )
        format_name = slot.format or "short-tip"
        url_instruction = ""
        if format_name in {"news-reaction", "learning-resource"}:
            if not slot.source_url:
                raise ValueError(f"The {format_name} format requires a researched source URL.")
            format_instruction = (
                f"Include this exact source URL: {slot.source_url}. Budget its full length at the end of the post. "
                f"Because the URL is {len(slot.source_url)} chars long, write exactly ONE short sentence (10 to 14 words, under 95 characters) "
                f"stating that contract audits miss off-chain agent tool vulnerabilities and SanadSquad secures the full stack, followed by the URL. "
                f"Text + URL MUST be strictly under 220 characters total."
            )
            url_instruction = f"MANDATORY LINK: The post must end with this exact URL: {slot.source_url}\n"
        elif format_name == "differentiation":
            format_instruction = "Name a specific coverage gap from the evidence. Explain why contract audits miss it. NO URLs/links. Keep text under 220 characters."
        elif format_name == "service-overview":
            format_instruction = "Explain what the service covers from the evidence. NO URLs/links. Keep text under 220 characters."
        else:
            format_instruction = "Write one to two concise sentences on a single practical observation. NO URLs/links. Keep text under 220 characters."
        prompt = (
            f"--- MARKETING WRITER INSTRUCTIONS ---\n{self.instructions}\n\n"
            f"--- BRAND VOICE ---\n{self.brand_voice}\n\n"
            f"--- MARKETING RULES ---\n{self.marketing_rules}\n\n"
            "--- OUTPUT REQUIREMENTS ---\n"
            "Write exactly ONE standalone post in 1 to 2 sentences. Target 12 to 18 words total.\n"
            "STRICT LIMIT: The entire post (including URL if required) MUST be under 230 characters total. Hard maximum is 280.\n"
            f"Selected format: {format_name}.\n{format_instruction}\n"
            f"{url_instruction}"
            "CORE POSITIONING:\n"
            "1. State the factual mechanism directly from RESEARCH EVIDENCE (do not contradict the evidence).\n"
            "2. Contrast why traditional contract audits miss it (Solidity audits only check contracts, missing off-chain infra, agent prompts, or payment policies).\n"
            "3. Conclude with how SanadSquad audits the relevant service line.\n"
            "--- ACCURACY & BRAND VOICE REQUIREMENTS ---\n"
            "- Factual claims MUST strictly follow RESEARCH EVIDENCE. If evidence states 'the agent does not hold a wallet', state that explicitly.\n"
            "- Do NOT use em dashes ('—') or banned vocabulary (e.g. 'demands', 'leverage', 'furthermore', 'crucial', 'drain funds').\n"
            "- Calm, authoritative, technical stance. No sensationalism.\n"
            f"--- RESEARCH EVIDENCE ---\n{source_context}\n{revision}\n"
            "Output ONLY the complete post text. No preamble or surrounding quotes."
        )
        llm, active_provider = get_chat_model(self.provider, self.model_name)
        response = llm.invoke([HumanMessage(content=prompt)])
        self.tracker.record_usage(
            response, prompt_text=prompt, provider=active_provider,
            model_name=self.model_name or (
                settings.GEMINI_MODEL if active_provider == "gemini" else settings.OLLAMA_MODEL
            ),
        )
        slot.raw_draft_content = extract_response_text(response)
        post = re.sub(r"<think>.*?</think>", "", slot.raw_draft_content, flags=re.DOTALL).strip()
        if len(post) >= 2 and post.startswith('"') and post.endswith('"'):
            post = post[1:-1].strip()
        slot.links = [url.rstrip(".,;:'\"") for url in re.findall(r"https?://[^\s)\]>]+", post)]
        slot.draft_content = post
        slot.draft_tweets = [post]
        slot.status = "drafted"
        return slot


marketing_writer = MarketingWriterAgent()
