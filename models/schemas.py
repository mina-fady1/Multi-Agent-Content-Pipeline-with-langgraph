from typing import List, Optional, Dict, Any, Literal
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator

Service = Literal["web3-security", "pentesting", "agentic-security", "agentic-payments"]


def validate_slot_id(value: str) -> str:
    """IDs are portable filenames, including on case-insensitive Windows disks."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Slot IDs must be 1-80 ASCII letters, digits, underscores or hyphens, starting with a letter or digit")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(10)], *[f"LPT{i}" for i in range(10)]}:
        raise ValueError("Slot ID is a reserved Windows filename")
    return value

class Finding(BaseModel):
    id: str
    version: Optional[str] = None
    type: Optional[str] = "strategy"
    title: str
    impact: str
    tags: List[str] = Field(default_factory=list)
    category: List[str] = Field(default_factory=list)
    protocol: Optional[str] = None
    language: Optional[str] = None
    content: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)

    @property
    def description(self) -> str:
        return self.content.get("description", "")

    @property
    def recommendation(self) -> str:
        return self.content.get("recommendation", "")

    @property
    def location(self) -> str:
        return self.content.get("location", "")

class Slot(BaseModel):
    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: str
    type: Literal["tech-thread", "marketing-post"]
    service: Optional[Service] = None
    focus: Optional[str] = None
    status: Literal[
        "pending", "researched", "drafted", "reviewed", "accepted", "failed", "dropped"
    ] = "pending"
    resolved_topic: Optional[str] = None
    source_type: Optional[Literal["kb", "web"]] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    source_summary: Optional[str] = None
    source_provenance: Optional[Literal["kb", "live", "fixture"]] = None
    format: Optional[str] = None
    links: List[str] = Field(default_factory=list)
    draft_content: Optional[str] = None
    raw_draft_content: Optional[str] = None
    draft_tweets: Optional[List[str]] = None
    review_verdict: Optional[Literal["ACCEPT", "REJECT"]] = None
    review_feedback: Optional[str] = None
    review_score: Optional[float] = None
    review_violations: List[str] = Field(default_factory=list)
    retry_count: int = 0
    image_prompt: Optional[str] = None
    image_path: Optional[str] = None
    image_spec: Optional[Dict[str, Any]] = None
    rejection_reason: Optional[str] = None
    attempts: List["DraftAttempt"] = Field(default_factory=list)

    _validate_id = field_validator("id")(validate_slot_id)

class ResearchQueryLog(BaseModel):
    timestamp: str
    slot_id: str
    post_type: str
    service: Optional[str] = None
    query: str
    allowed_domains: List[str] = Field(default_factory=list)
    requested_domains: List[str] = Field(default_factory=list)
    mode: Literal["live", "demo"] = "live"
    status: Literal["success", "empty", "error", "fixture"] = "success"
    error: Optional[str] = None
    results_count: int = 0
    retrieved_urls: List[str] = Field(default_factory=list)
    fixture_urls: List[str] = Field(default_factory=list)
    notes: Optional[str] = None

class CodeQualityResult(BaseModel):
    passed: bool
    violations: List[str] = Field(default_factory=list)
    hedging_density: float = 0.0
    sentence_length_variance: float = 0.0
    rule_of_three_detected: bool = False
    banned_words_found: List[str] = Field(default_factory=list)
    banned_chars_found: List[str] = Field(default_factory=list)
    banned_constructions_found: List[str] = Field(default_factory=list)

class ReviewResult(BaseModel):
    verdict: Literal["ACCEPT", "REJECT"]
    score: float = Field(ge=0.0, le=10.0)
    feedback: str = ""
    violations: List[str] = Field(default_factory=list)

class DraftAttempt(BaseModel):
    attempt: int
    timestamp: str
    raw_draft_content: Optional[str] = None
    draft_content: Optional[str] = None
    draft_tweets: List[str] = Field(default_factory=list)
    feedback_received: Optional[str] = None
    code_quality: Optional[CodeQualityResult] = None
    review: Optional[ReviewResult] = None
    error: Optional[str] = None

class VisualSpec(BaseModel):
    title: str
    subtitle: str
    theme: str = "dark-cyber"
    accent_color: str = "#00FFA3"
    key_takeaway: str
    image_prompt: str
    evidence_label: str = "RESEARCH NOTE"

class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = 0.0
    pricing_complete: bool = True
    known_cost_estimate_usd: float = 0.0
    usage_estimated: bool = False
    model_usage: List[Dict[str, Any]] = Field(default_factory=list)
    pricing_assumptions: List[str] = Field(default_factory=list)

class RunSummary(BaseModel):
    run_id: str
    timestamp: str
    total_slots: int
    pieces_shipped: int
    pieces_dropped: int
    dropped_details: List[Dict[str, str]] = Field(default_factory=list)
    tokens_used: TokenUsage
    duration_seconds: float
    demo_mode: bool = False
    pieces_demo: int = 0
    provider: Optional[str] = None
    model: Optional[str] = None


Slot.model_rebuild()
