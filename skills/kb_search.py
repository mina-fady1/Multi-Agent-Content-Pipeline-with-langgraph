import json
import re
from pathlib import Path
from typing import List, Optional, Dict, Any
from config.settings import settings
from models.schemas import Finding

class KnowledgeBaseSkill:
    """
    Reusable skill for querying and retrieving findings from the vulnerability Knowledge Base.
    Shared by:
    - Research Agent (for topic selection and KB citation)
    - Tech Writer Agent (for deep code snippets, exploit mechanics, and remediation)
    """

    def __init__(self, kb_path: Optional[Path] = None):
        self.kb_path = Path(kb_path or settings.get_kb_path())
        self._findings: List[Finding] = []
        self._load_kb()

    def _load_kb(self) -> None:
        if not self.kb_path.exists():
            raise FileNotFoundError(f"Knowledge base file not found at {self.kb_path}")
        with open(self.kb_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_findings = data.get("findings", [])
        self._findings = [Finding(**item) for item in raw_findings]

    def get_all_findings(self) -> List[Finding]:
        return [finding.model_copy(deep=True) for finding in self._findings]

    def get_by_id(self, finding_id: str) -> Optional[Finding]:
        for f in self._findings:
            if f.id.strip().lower() == finding_id.strip().lower():
                return f.model_copy(deep=True)
        return None

    def search(self, query: str, limit: int = 5) -> List[Finding]:
        if limit < 1:
            return []
        query = query.strip()
        if not query:
            return [finding.model_copy(deep=True) for finding in self._findings[:limit]]
        if query.lower().startswith("kb:"):
            finding = self.get_by_id(query[3:].strip())
            return [finding] if finding else []
        if re.fullmatch(r"[A-Za-z]+-\d+", query):
            finding = self.get_by_id(query)
            return [finding] if finding else []
        stop_words = {"the", "and", "for", "with", "from", "into", "that", "this", "about", "are"}
        query_tokens = {token for token in re.findall(r"[a-z0-9_]+", query.lower())
                        if len(token) > 2 and token not in stop_words}
        if not query_tokens:
            return []

        scored: List[tuple[int, Finding]] = []
        for f in self._findings:
            score = 0
            text_corpus = f"{f.title} {' '.join(f.tags)} {' '.join(f.category)} {f.description} {f.protocol}".lower()
            for token in query_tokens:
                pattern = rf"\b{re.escape(token)}\b"
                if re.search(pattern, f.title.lower()):
                    score += 5
                if any(re.search(pattern, tag.lower()) for tag in f.tags):
                    score += 4
                if re.search(pattern, text_corpus):
                    score += 1
            if score > 0:
                scored.append((score, f))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1].model_copy(deep=True) for item in scored[:limit]]

    def get_unused_finding(self, used_ids: List[str], query: Optional[str] = None) -> Optional[Finding]:
        candidates = self.search(query, limit=len(self._findings)) if query and query.strip() else self._findings
        used = {finding_id.strip().lower() for finding_id in used_ids}
        for cand in candidates:
            if cand.id.strip().lower() not in used:
                return cand.model_copy(deep=True)
        return None

kb_skill = KnowledgeBaseSkill()
