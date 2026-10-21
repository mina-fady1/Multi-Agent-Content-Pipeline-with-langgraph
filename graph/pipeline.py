"""A slot graph with dependencies owned by a single pipeline run."""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TypedDict, Optional, Set, Dict, Any

from langgraph.graph import StateGraph, END
from models.schemas import Slot, CodeQualityResult, ReviewResult, DraftAttempt
from agents.llm_factory import TokenTracker
from agents.research_agent import ResearchAgent
from agents.tech_writer import TechWriterAgent
from agents.marketing_writer import MarketingWriterAgent
from agents.reviewer_agent import ReviewerAgent
from agents.visual_agent import VisualAgent
from skills.quality_checker import QualityCheckerSkill
from skills.web_search import WebSearchSkill


@dataclass
class PipelineServices:
    research: ResearchAgent
    tech_writer: TechWriterAgent
    marketing_writer: MarketingWriterAgent
    reviewer: ReviewerAgent
    visual: VisualAgent
    quality: QualityCheckerSkill
    tracker: TokenTracker
    demo_mode: bool = False

    @classmethod
    def create(cls, provider=None, model_name=None, demo_mode=False):
        tracker = TokenTracker()
        tracker.reset(provider)
        return cls(
            research=ResearchAgent(web_search=WebSearchSkill(demo_mode=demo_mode)),
            tech_writer=TechWriterAgent(provider=provider, model_name=model_name, tracker=tracker),
            marketing_writer=MarketingWriterAgent(provider=provider, model_name=model_name, tracker=tracker),
            reviewer=ReviewerAgent(provider=provider, model_name=model_name, tracker=tracker),
            visual=VisualAgent(), quality=QualityCheckerSkill(), tracker=tracker, demo_mode=demo_mode,
        )


class SlotPipelineGraphState(TypedDict):
    slot: Slot
    output_dir: Path
    retrieved_urls: Set[str]
    fixture_urls: Set[str]
    code_quality: Optional[CodeQualityResult]
    review_result: Optional[ReviewResult]
    feedback: Optional[str]
    retry_count: int


def research_node(state, services):
    slot = services.research.research_slot(state["slot"])
    logs = [log for log in services.research.web_search.get_query_logs() if log.slot_id == slot.id]
    retrieved = {url for log in logs for url in log.retrieved_urls}
    fixture = {url for log in logs for url in log.fixture_urls}
    if slot.source_provenance == "fixture" and not services.demo_mode:
        slot.status = "failed"
        slot.rejection_reason = "Fixture evidence is permitted only in explicit demo mode"
    return {"slot": slot, "retrieved_urls": retrieved, "fixture_urls": fixture}


def research_routing_decision(state):
    return "write" if state["slot"].status == "researched" else "drop"


def write_node(state, services):
    slot = state["slot"]
    attempt = DraftAttempt(attempt=state["retry_count"] + 1,
                           timestamp=datetime.now(timezone.utc).isoformat(),
                           feedback_received=state.get("feedback"))
    slot.attempts.append(attempt)
    slot.raw_draft_content = None
    writer = services.tech_writer if slot.type == "tech-thread" else services.marketing_writer
    try:
        writer.write(slot, feedback=state.get("feedback"))
        attempt.raw_draft_content = slot.raw_draft_content
        attempt.draft_content = slot.draft_content
        attempt.draft_tweets = list(slot.draft_tweets or [])
    except Exception as exc:
        attempt.raw_draft_content = slot.raw_draft_content
        attempt.error = f"Writing failed: {exc}"
        raise
    return {"slot": slot, "code_quality": None, "review_result": None}


def code_quality_node(state, services):
    slot = state["slot"]
    # Demo sources are separately identified and never enter live retrieval logs.
    evidence_urls = set(state["retrieved_urls"])
    if services.demo_mode:
        evidence_urls.update(state["fixture_urls"])
    required_links = [slot.source_url] if slot.source_url and slot.format in {"news-reaction", "learning-resource"} else []
    result = services.quality.check_piece(
        text=slot.draft_content or "", post_type=slot.type,
        retrieved_urls=evidence_urls, tweets=slot.draft_tweets,
        source_id=slot.source_id if slot.type == "tech-thread" else None,
        source_url=slot.source_url if slot.type == "tech-thread" else None,
        required_links=required_links,
    )
    slot.attempts[-1].code_quality = result.model_copy(deep=True)
    return {"code_quality": result, "slot": slot}


def review_node(state, services):
    slot = state["slot"]
    result = services.reviewer.review(slot, code_check=state["code_quality"])
    # A model can never override the deterministic gate.
    if not state["code_quality"].passed and result.verdict == "ACCEPT":
        result = ReviewResult(verdict="REJECT", score=0, feedback="Fix the code quality violations.",
                              violations=state["code_quality"].violations)
    slot.review_verdict = result.verdict
    slot.review_score = result.score
    slot.review_feedback = result.feedback
    slot.review_violations = list(result.violations)
    slot.status = "accepted" if result.verdict == "ACCEPT" else "reviewed"
    slot.attempts[-1].review = result.model_copy(deep=True)
    return {"review_result": result, "slot": slot}


def review_routing_decision(state):
    result = state.get("review_result")
    if result and result.verdict == "ACCEPT" and state["code_quality"].passed:
        return "visual"
    return "retry_write" if state.get("retry_count", 0) < 1 else "drop"


def retry_prep_node(state):
    slot = state["slot"]
    review = state.get("review_result")
    feedback = review.feedback if review else "Revise for quality and grounding."
    if review and review.violations:
        feedback += "\n" + "\n".join(review.violations)
    slot.retry_count = state.get("retry_count", 0) + 1
    return {"slot": slot, "feedback": feedback, "retry_count": slot.retry_count}


def visual_node(state, services):
    slot = services.visual.generate_visual(state["slot"], output_dir=state["output_dir"])
    return {"slot": slot}


def drop_node(state):
    slot = state["slot"]
    slot.status = "dropped"
    if not slot.rejection_reason:
        slot.rejection_reason = slot.review_feedback or "Research or quality review failed"
        if slot.review_violations:
            slot.rejection_reason += " (Violations: " + ", ".join(slot.review_violations) + ")"
    return {"slot": slot}


def create_slot_pipeline_graph(services: Optional[PipelineServices] = None):
    services = services or PipelineServices.create()
    workflow = StateGraph(SlotPipelineGraphState)
    for name, node in (("research", research_node), ("write", write_node),
                       ("code_quality", code_quality_node), ("review", review_node), ("visual", visual_node)):
        workflow.add_node(name, partial(node, services=services))
    workflow.add_node("retry_prep", retry_prep_node)
    workflow.add_node("drop", drop_node)
    workflow.set_entry_point("research")
    workflow.add_conditional_edges("research", research_routing_decision, {"write": "write", "drop": "drop"})
    workflow.add_edge("write", "code_quality")
    workflow.add_edge("code_quality", "review")
    workflow.add_conditional_edges("review", review_routing_decision,
                                   {"visual": "visual", "retry_write": "retry_prep", "drop": "drop"})
    workflow.add_edge("retry_prep", "write")
    workflow.add_edge("visual", END)
    workflow.add_edge("drop", END)
    return workflow.compile()
