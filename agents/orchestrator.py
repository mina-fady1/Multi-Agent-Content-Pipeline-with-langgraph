"""Validate plans, isolate run dependencies, and publish traceable artifacts."""
import concurrent.futures
import csv
import io
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional

from rich.console import Console
from rich.table import Table
from config.settings import settings
from models.schemas import Slot, RunSummary, validate_slot_id
from models.paths import output_path
from graph.pipeline import PipelineServices, create_slot_pipeline_graph

console = Console()


class Orchestrator:
    def __init__(self, provider: Optional[str] = None, model_name: Optional[str] = None,
                 demo_mode: bool = False):
        self.provider = (provider or settings.DEFAULT_PROVIDER).lower()
        if self.provider not in {"gemini", "ollama"}:
            raise ValueError("Provider must be gemini or ollama")
        self.model_name = model_name or (settings.GEMINI_MODEL if self.provider == "gemini" else settings.OLLAMA_MODEL)
        self.demo_mode = demo_mode
        self._run_lock = RLock()
        self.services = None
        self.graph = None
        self.last_slots = []

    def _start_run(self):
        self.services = PipelineServices.create(self.provider, self.model_name, self.demo_mode)
        self.graph = create_slot_pipeline_graph(self.services)
        self.last_slots = []

    @staticmethod
    def _validate_slots(slots):
        if not slots:
            raise ValueError("A plan must contain at least one slot")
        seen = set()
        for slot in slots:
            validate_slot_id(slot.id)
            key = slot.id.casefold()
            if key in seen:
                raise ValueError(f"Duplicate slot ID: {slot.id}")
            seen.add(key)

    def load_plan(self, plan_path: Path, set_overrides=None):
        data = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
            raise ValueError("Plan must be an object containing a slots array")
        slots = []
        for item in data["slots"]:
            if not isinstance(item, dict) or set(item) - {"id", "type", "service", "focus", "format"}:
                raise ValueError("Plan slots may contain only id, type, service, focus and format")
            slots.append(Slot(**item))
        self._validate_slots(slots)
        unknown = set(set_overrides or {}) - {slot.id for slot in slots}
        if unknown:
            raise ValueError(f"Overrides reference unknown slot IDs: {', '.join(sorted(unknown))}")
        for slot in slots:
            if set_overrides and slot.id in set_overrides:
                slot.focus = set_overrides[slot.id]
        return slots

    @staticmethod
    def _fresh_slot(slot):
        # Do not reuse an earlier run's drafts, status or review when a caller reruns a Slot.
        return Slot(id=slot.id, type=slot.type, service=slot.service, focus=slot.focus, format=slot.format)

    def _execute_slot(self, slot, output_dir):
        state = {"slot": slot, "output_dir": output_dir, "retrieved_urls": set(), "fixture_urls": set(),
                 "code_quality": None, "review_result": None, "feedback": None, "retry_count": 0}
        try:
            return self.graph.invoke(state)["slot"]
        except Exception as exc:
            slot.status = "dropped"
            slot.rejection_reason = f"Execution error in graph: {exc}"
            if slot.attempts and not slot.attempts[-1].error:
                slot.attempts[-1].error = slot.rejection_reason
            return slot

    def run_slot(self, slot: Slot, output_dir: Path) -> Slot:
        """Execute one isolated slot; use run_piece to also export and summarize it."""
        with self._run_lock, self._output_lock(Path(output_dir)):
            self._validate_slots([slot])
            self._start_run()
            return self._execute_slot(self._fresh_slot(slot), Path(output_dir))

    def run_week(self, plan_path: Path, output_dir: Path, set_overrides=None,
                 parallel: bool = True, max_workers: int = 3) -> RunSummary:
        slots = self.load_plan(plan_path, set_overrides)
        return self.run_slots(slots, output_dir, parallel=parallel, max_workers=max_workers)

    def run_piece(self, slot: Slot, output_dir: Path):
        with self._run_lock:
            summary = self.run_slots([slot], output_dir, parallel=False)
            return self.last_slots[0], summary

    @contextmanager
    def _output_lock(self, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_path(output_dir, ".pipeline.lock")
        try:
            handle = path.open("x", encoding="utf-8")
        except FileExistsError:
            raise RuntimeError(f"Output directory is already locked: {output_dir}") from None
        try:
            with handle:
                handle.write(json.dumps({"pid": os.getpid(), "timestamp": datetime.now(timezone.utc).isoformat()}))
            yield
        finally:
            path.unlink(missing_ok=True)

    def run_slots(self, slots, output_dir: Path, parallel=True, max_workers=3):
        self._validate_slots(slots)
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        fresh = [self._fresh_slot(slot) for slot in slots]
        output_dir = Path(output_dir).resolve()
        with self._run_lock, self._output_lock(output_dir):
            self._start_run()
            self.services.research.reserve_plan_topics(fresh)
            started = time.monotonic()
            previous = self._managed_artifacts(output_dir)
            # Record intended paths before workers can create partial artifacts.
            previous.update(f"images/{slot.id}.png" for slot in fresh)
            previous.update(f"posts/{slot.id}.json" for slot in fresh)
            self._write_manifest(output_dir, previous, "running")
            try:
                if parallel and len(fresh) > 1:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                        ordered = list(executor.map(lambda s: self._execute_slot(s, output_dir), fresh))
                else:
                    ordered = [self._execute_slot(slot, output_dir) for slot in fresh]
                self.last_slots = ordered
                self._export_artifacts(ordered, output_dir, previous=previous)
                self._export_csv(ordered, output_path(output_dir, "posts.csv"))
                accepted = [s for s in ordered if s.status == "accepted"]
                dropped = [s for s in ordered if s.status != "accepted"]
                summary = RunSummary(
                    run_id=output_dir.name, timestamp=datetime.now(timezone.utc).isoformat(),
                    total_slots=len(ordered), pieces_shipped=sum(self._publishable(s) for s in ordered),
                    pieces_dropped=len(dropped), pieces_demo=len(accepted) if self.demo_mode else 0,
                    dropped_details=[{"slot_id": s.id, "type": s.type,
                                      "reason": s.rejection_reason or "No accepted content"} for s in dropped],
                    tokens_used=self.services.tracker.get_summary(), duration_seconds=round(time.monotonic()-started, 2),
                    demo_mode=self.demo_mode, provider=self.provider, model=self.model_name,
                )
                self._safe_write_json(output_path(output_dir, "run_summary.json"), summary.model_dump())
                managed = self._managed_artifacts(output_dir)
                self._write_manifest(output_dir, managed, "complete")
                self._print_summary_table(summary, ordered)
                return summary
            except Exception as exc:
                self._write_manifest(output_dir, self._managed_artifacts(output_dir), "failed", error=str(exc))
                raise

    def _publishable(self, slot):
        return slot.status == "accepted" and not self.demo_mode and slot.source_provenance != "fixture"

    def _managed_artifacts(self, output_dir):
        manifest = output_path(output_dir, "artifact_manifest.json")
        paths = set()
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            candidates = data.get("generated_files", [])
            for value in candidates:
                candidate = Path(value)
                if len(candidate.parts) != 2:
                    raise ValueError("Invalid generated artifact path in manifest")
                folder, filename = candidate.parts
                suffix = ".json" if folder == "posts" else ".png" if folder == "images" else None
                if suffix is None or not filename.endswith(suffix):
                    raise ValueError("Invalid generated artifact type in manifest")
                validate_slot_id(filename[:-len(suffix)])
                output_path(output_dir, folder, filename)
                paths.add(f"{folder}/{filename}")
        else:
            # Migrate the previous export format, deleting only named generated artifacts.
            legacy = output_path(output_dir, "posts.json")
            if legacy.exists():
                for item in json.loads(legacy.read_text(encoding="utf-8")):
                    sid = validate_slot_id(item["slot_id"])
                    for folder, suffix in (("posts", ".json"), ("images", ".png")):
                        output_path(output_dir, folder, sid + suffix)
                        paths.add(f"{folder}/{sid}{suffix}")
        return paths

    def _write_manifest(self, output_dir, generated, status, error=None):
        self._safe_write_json(output_path(output_dir, "artifact_manifest.json"), {
            "status": status, "demo_mode": self.demo_mode,
            "timestamp": datetime.now(timezone.utc).isoformat(), "generated_files": sorted(generated),
            "error": error,
        })

    def _export_artifacts(self, slots, output_dir, previous=None):
        self._validate_slots(slots)
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        previous = self._managed_artifacts(output_dir) if previous is None else previous
        generated = set()
        resolved = [{k: getattr(s, k) for k in ("id", "type", "service", "focus", "resolved_topic",
                    "source_type", "source_id", "source_url", "source_provenance", "status")} for s in slots]
        self._safe_write_json(output_path(output_dir, "resolved_plan.json"), resolved)
        logs = self.services.research.web_search.get_query_logs() if self.services else []
        self._safe_write_json(output_path(output_dir, "research_log.json"), [q.model_dump() for q in logs])
        drafts = [{"id": s.id, "type": s.type, "raw_draft_content": s.raw_draft_content,
                   "draft_content": s.draft_content, "draft_tweets": s.draft_tweets, "retry_count": s.retry_count,
                   "attempts": [a.model_dump() for a in s.attempts]} for s in slots]
        reviews = [{"id": s.id, "status": s.status, "verdict": s.review_verdict, "score": s.review_score,
                    "feedback": s.review_feedback, "violations": s.review_violations,
                    "rejection_reason": s.rejection_reason, "attempts": [a.model_dump() for a in s.attempts]} for s in slots]
        self._safe_write_json(output_path(output_dir, "drafts.json"), drafts)
        self._safe_write_json(output_path(output_dir, "review_report.json"), reviews)
        final, demo, posts = [], [], []
        for slot in slots:
            if slot.status != "accepted":
                continue
            if slot.image_path:
                expected = output_path(output_dir, "images", f"{slot.id}.png")
                if Path(slot.image_path).resolve() != expected:
                    raise ValueError("Image path does not match the slot artifact destination")
                generated.add(f"images/{slot.id}.png")
            piece = {"id": slot.id, "type": slot.type, "topic": slot.resolved_topic, "status": slot.status,
                     "content": slot.draft_content, "tweets": slot.draft_tweets, "image_path": slot.image_path,
                     "image_prompt": slot.image_prompt, "image_spec": slot.image_spec,
                     "source_provenance": slot.source_provenance}
            if not self._publishable(slot):
                demo.append(piece)
                continue
            final.append(piece)
            post = {"slot_id": slot.id, "type": slot.type, "service": slot.service, "format": slot.format,
                    "topic": slot.resolved_topic, "post_text": slot.draft_content, "tweets": slot.draft_tweets,
                    "image": slot.image_path, "links": slot.links, "source_type": slot.source_type,
                    "source_id": slot.source_id, "source_url": slot.source_url,
                    "source_provenance": slot.source_provenance, "review_score": slot.review_score}
            self._safe_write_json(output_path(output_dir, "posts", f"{slot.id}.json"), post)
            generated.add(f"posts/{slot.id}.json")
            posts.append(post)
        self._safe_write_json(output_path(output_dir, "final_content.json"), final)
        self._safe_write_json(output_path(output_dir, "demo_content.json"), demo)
        self._safe_write_json(output_path(output_dir, "posts.json"), posts)
        for obsolete in sorted(previous - generated):
            # Resolve again immediately before unlinking, including symlink containment.
            output_path(output_dir, *Path(obsolete).parts).unlink(missing_ok=True)
        self._write_manifest(output_dir, generated, "exported")

    @staticmethod
    def _atomic_write(path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=".pipeline-", suffix=".tmp", delete=False) as handle:
                tmp = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _safe_write_json(self, path, data):
        self._atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))

    def _export_csv(self, slots, csv_path):
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["Post Text", "Image"])
        for slot in slots:
            if self._publishable(slot):
                writer.writerow([slot.draft_content or "", slot.image_path or ""])
        self._atomic_write(csv_path, buffer.getvalue())

    def _print_summary_table(self, summary, slots):
        table = Table(title="Content pipeline run")
        for title in ("Slot", "Type", "Status", "Score", "Reason / Image"):
            table.add_column(title)
        for slot in slots:
            table.add_row(slot.id, slot.type,
                          "demo" if self.demo_mode and slot.status == "accepted" else slot.status,
                          str(slot.review_score) if slot.review_score is not None else "-",
                          slot.image_path or slot.rejection_reason or "-")
        console.print(table)
        cost = summary.tokens_used.estimated_cost_usd
        cost_text = f"${cost:.4f}" if cost is not None else "unknown (unpriced model)"
        console.print(f"Shipped: {summary.pieces_shipped} | Demo: {summary.pieces_demo} | "
                      f"Dropped: {summary.pieces_dropped} | Tokens: {summary.tokens_used.total_tokens} | "
                      f"Estimated model cost: {cost_text} | Duration: {summary.duration_seconds}s")
