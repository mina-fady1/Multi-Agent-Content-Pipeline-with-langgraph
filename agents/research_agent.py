import re
from threading import Lock
from typing import List, Optional

from models.schemas import Slot, Finding
from skills.kb_search import KnowledgeBaseSkill, kb_skill
from skills.web_search import WebSearchSkill


class ResearchAgent:
    """Resolve topics using run-local research state and explicit provenance."""

    def __init__(
        self, web_search: Optional[WebSearchSkill] = None,
        kb: Optional[KnowledgeBaseSkill] = None,
    ):
        self.web_search = web_search if web_search is not None else WebSearchSkill()
        self.kb = kb if kb is not None else kb_skill
        self.used_kb_ids: List[str] = []
        self._reserved_kb_ids: set[str] = set()
        self._kb_lock = Lock()

    @staticmethod
    def _fail(slot: Slot, reason: str) -> Slot:
        slot.status = "failed"
        slot.rejection_reason = reason
        return slot

    def research_slot(self, slot: Slot) -> Slot:
        try:
            # A repeated invocation must not retain the previous source or links
            # when the new lookup fails or switches between web and KB evidence.
            slot.source_type = None
            slot.source_provenance = None
            slot.source_id = None
            slot.source_url = None
            slot.source_summary = None
            slot.resolved_topic = None
            slot.links = []
            slot.rejection_reason = None
            if slot.type == "tech-thread":
                return self._research_tech_thread(slot)
            if slot.type == "marketing-post":
                return self._research_marketing_post(slot)
            return self._fail(slot, f"Unknown slot type: {slot.type}")
        except Exception as exc:
            return self._fail(slot, f"Research failed with error: {exc}")

    def _resolve_explicit_finding(self, focus: str) -> Optional[Finding]:
        if focus.lower().startswith("kb:"):
            return self.kb.get_by_id(focus[3:].strip())
        finding = self.kb.get_by_id(focus)
        if finding is None and not re.fullmatch(r"[A-Za-z]+-\d+", focus):
            matches = self.kb.search(focus, limit=1)
            finding = matches[0] if matches else None
        return finding

    def reserve_plan_topics(self, slots: List[Slot]) -> None:
        """Keep automatic slots from racing with explicit KB topics in a plan."""
        with self._kb_lock:
            for slot in slots:
                focus = (slot.focus or "").strip()
                if not focus:
                    continue
                if slot.type == "tech-thread":
                    if re.search(r"https?://|\bexternal\b", focus, re.IGNORECASE):
                        continue
                elif slot.type == "marketing-post":
                    if not (focus.lower().startswith("kb:") or re.search(
                        r"\bvulnerability\s+class\b", focus, re.IGNORECASE
                    )):
                        continue
                    focus = re.sub(r"\bvulnerability\s+class\b\s*:?", "", focus,
                                   flags=re.IGNORECASE).strip()
                    if not focus:
                        continue
                finding = self._resolve_explicit_finding(focus)
                if finding is not None:
                    self._reserved_kb_ids.add(finding.id)

    def _select_finding(self, focus: str) -> Optional[Finding]:
        """Only a blank focus permits automatic allocation; allocation is atomic."""
        with self._kb_lock:
            if not focus:
                finding = self.kb.get_unused_finding(self.used_kb_ids + list(self._reserved_kb_ids))
            else:
                finding = self._resolve_explicit_finding(focus)
            if finding is not None and finding.id not in self.used_kb_ids:
                self.used_kb_ids.append(finding.id)
            return finding

    @staticmethod
    def _apply_web_source(slot: Slot, result: dict) -> Slot:
        slot.source_type = "web"
        slot.source_provenance = result["provenance"]
        slot.source_id = None
        slot.source_url = result["url"]
        slot.resolved_topic = result["title"]
        slot.source_summary = result["content"]
        slot.status = "researched"
        return slot

    def _research_tech_thread(self, slot: Slot) -> Slot:
        focus = (slot.focus or "").strip()
        use_external = bool(re.search(r"https?://|\bexternal\b", focus, re.IGNORECASE))
        if use_external:
            query = re.sub(r"\bexternal\b\s*:?", "", focus, flags=re.IGNORECASE).strip()
            results = self.web_search.search(
                query=query or "smart contract vulnerability post-mortem",
                slot_id=slot.id, post_type=slot.type,
                service=slot.service or "web3-security", max_results=3,
            )
            if not results:
                return self._fail(slot, "Web research returned no allowed sources for tech thread.")
            return self._apply_web_source(slot, results[0])

        finding = self._select_finding(focus)
        if finding is None:
            reason = (f"No Knowledge Base finding matches explicit focus {focus!r}."
                      if focus else "No unused findings are available in the Knowledge Base.")
            return self._fail(slot, reason)
        slot.source_type = "kb"
        slot.source_provenance = "kb"
        slot.source_id = finding.id
        slot.source_url = None
        slot.resolved_topic = f"{finding.title} [{finding.protocol} / {finding.language}]"
        slot.service = "web3-security"
        slot.source_summary = (
            f"Vulnerability ID: {finding.id} ({finding.impact} impact). "
            f"Tags: {', '.join(finding.tags)}. Description: {finding.description} "
            f"Recommendation: {finding.recommendation}"
        )
        slot.status = "researched"
        return slot

    def _research_marketing_post(self, slot: Slot) -> Slot:
        focus = (slot.focus or "").strip()
        service = slot.service or "agentic-security"
        slot.service = service
        is_vuln_class = bool(re.search(r"\bvulnerability\s+class\b", focus, re.IGNORECASE))
        if focus.lower().startswith("kb:") or is_vuln_class:
            clean_focus = re.sub(r"\bvulnerability\s+class\b\s*:?", "", focus,
                                 flags=re.IGNORECASE).strip()
            finding = self._select_finding(clean_focus) if clean_focus else None
            if finding is None:
                return self._fail(slot, f"No Knowledge Base finding matches explicit focus {focus!r}.")
            slot.source_type = "kb"
            slot.source_provenance = "kb"
            slot.source_id = finding.id
            slot.source_url = None
            slot.format = slot.format or "short-tip"
            slot.resolved_topic = f"Vulnerability Class: {finding.title}"
            slot.source_summary = f"Audit finding {finding.id}: {finding.description}"
            slot.status = "researched"
            return slot

        if not slot.format:
            if slot.id == "marketing_1" or "news" in focus.lower():
                slot.format = "news-reaction"
            elif slot.id == "marketing_2" or service == "agentic-payments":
                slot.format = "differentiation"
            else:
                slot.format = "short-tip"
        default_queries = {
            "agentic-payments": "x402 protocol architecture and agent payment rails",
            "pentesting": "web3 supply chain npm package vulnerability incident",
            "agentic-security": "prompt injection reaching agent tool use and signing capabilities",
        }
        query = focus or default_queries.get(service, f"latest security risks in {service.replace('-', ' ')}")
        results = self.web_search.search(
            query=query, slot_id=slot.id, post_type=slot.type, service=service, max_results=3,
        )
        if not results:
            return self._fail(slot, f"No allowed web sources found for service '{service}'.")
        self._apply_web_source(slot, results[0])
        if slot.format in {"news-reaction", "learning-resource"}:
            slot.links = [results[0]["url"]]
        return slot


# Compatibility import only; pipeline runs instantiate their own context.
research_agent = ResearchAgent()
