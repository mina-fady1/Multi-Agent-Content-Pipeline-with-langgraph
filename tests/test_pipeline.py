import csv
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError
from agents.orchestrator import Orchestrator
from agents.visual_agent import VisualAgent
from config.settings import settings
from models.paths import output_path
from models.schemas import Slot

ACCEPT = json.dumps({"verdict": "ACCEPT", "score": 8.2, "feedback": "", "violations": []})
REJECT = json.dumps({"verdict": "REJECT", "score": 4, "feedback": "Rewrite the unsupported claim.", "violations": ["Grounding"]})
GOOD_POST = "Validate signer permissions before dispatch."


def response(text):
    return AIMessage(content=text, usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})


def thread_response(messages):
    prompt = messages[0].content
    source = re.search(r"Vulnerability ID: (SS-[0-9]+)", prompt).group(1)
    return response("\n\n".join([
        "1/ Check signer permissions before a cross-program call.",
        "2/ Validate every target account before dispatch.",
        "3/ Restrict the actions available to each role.",
        "4/ Test that unauthorized instructions fail.",
        f"5/ Keep permission checks explicit. [KB: {source}]",
    ]))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "run"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("agents.orchestrator.console", Mock()))
        self.stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("Unexpected network request")))
        self.marketing = Mock()
        self.marketing.invoke.return_value = response(GOOD_POST)
        self.tech = Mock()
        self.tech.invoke.side_effect = thread_response
        self.review = Mock()
        self.review.invoke.return_value = response(ACCEPT)
        self.marketing_factory = self.stack.enter_context(patch("agents.marketing_writer.get_chat_model", return_value=(self.marketing, "ollama")))
        self.tech_factory = self.stack.enter_context(patch("agents.tech_writer.get_chat_model", return_value=(self.tech, "ollama")))
        self.review_factory = self.stack.enter_context(patch("agents.reviewer_agent.get_chat_model", return_value=(self.review, "ollama")))

    def slot(self, sid="piece", **kwargs):
        return Slot(id=sid, type="marketing-post", focus="kb:SS-03699", **kwargs)

    def load(self, name):
        return json.loads((self.out/name).read_text(encoding="utf-8"))

    def test_success_exports_reviewed_content_and_real_image(self):
        o = Orchestrator(provider="ollama", model_name="chosen-local-model")
        summary = o.run_slots([self.slot()], self.out)
        self.assertEqual((summary.pieces_shipped, summary.pieces_dropped), (1, 0))
        self.assertEqual(summary.model, "chosen-local-model")
        self.marketing_factory.assert_called_with("ollama", "chosen-local-model")
        self.review_factory.assert_called_with("ollama", "chosen-local-model")
        self.assertEqual(summary.tokens_used.total_tokens, 30)
        self.assertEqual(self.load("artifact_manifest.json")["status"], "complete")
        self.assertEqual(self.load("posts.json")[0]["post_text"], GOOD_POST)
        from PIL import Image
        with Image.open(self.out/"images/piece.png") as image:
            self.assertEqual(image.size, (1200, 675))
        self.assertFalse((self.out/".pipeline.lock").exists())

    def test_invalid_review_retries_and_preserves_both_attempts(self):
        self.review.invoke.side_effect = [response("Service unavailable"), response(ACCEPT)]
        o = Orchestrator(provider="ollama")
        summary = o.run_slots([self.slot()], self.out)
        self.assertEqual(summary.pieces_shipped, 1)
        attempts = self.load("drafts.json")[0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["review"]["verdict"], "REJECT")
        self.assertIn("Invalid reviewer response", attempts[0]["review"]["violations"])
        self.assertEqual(attempts[1]["review"]["verdict"], "ACCEPT")
        self.assertIn("invalid JSON", attempts[1]["feedback_received"])
        self.assertTrue(attempts[0]["code_quality"]["passed"])
        self.assertEqual(attempts[0]["raw_draft_content"], GOOD_POST)
        self.assertEqual(o.last_slots[0].retry_count, 1)

    def test_two_invalid_reviews_drop_and_do_not_render(self):
        self.review.invoke.return_value = response("[]")
        summary = Orchestrator(provider="ollama").run_slots([self.slot()], self.out)
        self.assertEqual((summary.pieces_shipped, summary.pieces_dropped), (0, 1))
        self.assertEqual(self.marketing.invoke.call_count, 2)
        self.assertEqual(self.load("final_content.json"), [])
        self.assertFalse((self.out/"images/piece.png").exists())

    def test_long_draft_is_rewritten_without_losing_first_attempt(self):
        long_post = ("Validate signer permissions before dispatch. " * 9).strip()
        self.marketing.invoke.side_effect = [response(long_post), response(GOOD_POST)]
        summary = Orchestrator(provider="ollama").run_slots([self.slot()], self.out)
        self.assertEqual(summary.pieces_shipped, 1)
        self.assertEqual(self.review.invoke.call_count, 1)
        attempts = self.load("drafts.json")[0]["attempts"]
        self.assertEqual(attempts[0]["draft_content"], long_post)
        self.assertFalse(attempts[0]["code_quality"]["passed"])
        self.assertIn(long_post, self.marketing.invoke.call_args_list[1].args[0][0].content)
        self.assertEqual(attempts[1]["draft_content"], GOOD_POST)

    def test_missing_citation_is_not_silently_published(self):
        self.tech.invoke.side_effect = None
        self.tech.invoke.return_value = response("\n".join(f"{i}/ Validate account ownership before dispatch." for i in range(1, 6)))
        summary = Orchestrator(provider="ollama").run_slots([Slot(id="thread", type="tech-thread", focus="SS-03699")], self.out)
        self.assertEqual(summary.pieces_dropped, 1)
        self.review.invoke.assert_not_called()
        self.assertIn("cite its KB finding", self.load("review_report.json")[0]["rejection_reason"])

    def test_writer_exception_is_isolated_in_parallel(self):
        self.marketing.invoke.side_effect = [RuntimeError("provider unavailable"), response(GOOD_POST)]
        summary = Orchestrator(provider="ollama").run_slots([self.slot("first"), self.slot("second")], self.out)
        self.assertEqual((summary.pieces_shipped, summary.pieces_dropped), (1, 1))
        failed = next(r for r in self.load("review_report.json") if r["status"] == "dropped")
        self.assertIn("provider unavailable", failed["attempts"][0]["error"])
        self.assertEqual([r["id"] for r in self.load("resolved_plan.json")], ["first", "second"])

    def test_missing_live_evidence_stops_before_writing(self):
        slot = Slot(id="research", type="marketing-post", service="pentesting", focus="npm security")
        with patch.object(settings, "TAVILY_API_KEY", ""):
            summary = Orchestrator(provider="ollama").run_slots([slot], self.out)
        self.assertEqual(summary.pieces_dropped, 1)
        self.marketing.invoke.assert_not_called()
        self.assertEqual(self.load("research_log.json")[0]["status"], "error")

    def test_demo_is_separate_from_live_publication_outputs(self):
        slot = Slot(id="demo", type="marketing-post", service="agentic-security", focus="prompt injection")
        o = Orchestrator(provider="ollama", demo_mode=True)
        summary = o.run_slots([slot], self.out)
        self.assertEqual((summary.pieces_shipped, summary.pieces_demo, summary.pieces_dropped), (0, 1, 0))
        self.assertEqual(self.load("posts.json"), [])
        self.assertEqual(self.load("final_content.json"), [])
        with (self.out/"posts.csv").open(newline="", encoding="utf-8") as stream:
            self.assertEqual(list(csv.reader(stream)), [["Post Text", "Image"]])
        demo = self.load("demo_content.json")[0]
        self.assertEqual(demo["source_provenance"], "fixture")
        self.assertEqual(demo["image_spec"]["evidence_label"], "DEMO FIXTURE")
        self.assertEqual(o.services.research.web_search.get_retrieved_urls(), set())
        self.assertTrue(self.load("research_log.json")[0]["fixture_urls"])

    def test_news_demo_url_is_checked_against_explicit_fixture_evidence(self):
        slot = Slot(id="marketing_1", type="marketing-post", service="agentic-security", focus="prompt injection")
        def news(messages):
            url = re.search(r"Include this exact source URL: (https?://\S+)\. Budget", messages[0].content).group(1)
            return response("Check agent tool permissions. " + url)
        self.marketing.invoke.side_effect = news
        summary = Orchestrator(provider="ollama", demo_mode=True).run_slots([slot], self.out)
        self.assertEqual(summary.pieces_demo, 1)
        self.assertEqual(summary.pieces_shipped, 0)

    def test_repeated_runs_reset_kb_logs_and_token_accounting(self):
        o = Orchestrator(provider="ollama")
        first = o.run_slots([self.slot()], self.out)
        original_services = o.services
        original_services.research.web_search._retrieved_urls_this_run.add("https://old.example/report")
        second = o.run_slots([self.slot()], self.out)
        self.assertIsNot(original_services, o.services)
        self.assertEqual((first.tokens_used.total_tokens, second.tokens_used.total_tokens), (30, 30))
        self.assertEqual(self.load("research_log.json"), [])
        self.assertEqual(o.services.research.web_search.get_retrieved_urls(), set())
        blank = Slot(id="blank", type="tech-thread")
        o.run_slots([blank], self.out)
        first_id = o.last_slots[0].source_id
        o.run_slots([blank], self.out)
        self.assertEqual(o.last_slots[0].source_id, first_id)

    def test_same_slot_object_can_be_rerun_without_old_history(self):
        o = Orchestrator(provider="ollama")
        o.run_slots([self.slot()], self.out)
        previous = o.last_slots[0]
        o.run_slots([previous], self.out)
        self.assertEqual(len(o.last_slots[0].attempts), 1)
        self.assertIsNot(previous, o.last_slots[0])

    def test_dropped_rerun_removes_only_managed_posts_and_images(self):
        o = Orchestrator(provider="ollama")
        o.run_slots([self.slot()], self.out)
        (self.out/"posts/notes.txt").write_text("Keep this user note", encoding="utf-8")
        self.review.invoke.return_value = response(REJECT)
        summary = o.run_slots([self.slot()], self.out)
        self.assertEqual(summary.pieces_dropped, 1)
        self.assertFalse((self.out/"posts/piece.json").exists())
        self.assertFalse((self.out/"images/piece.png").exists())
        self.assertTrue((self.out/"posts/notes.txt").exists())
        self.assertEqual(self.load("posts.json"), [])

    def test_export_failure_tracks_partial_image_for_next_run_cleanup(self):
        o = Orchestrator(provider="ollama")
        with patch.object(o, "_export_artifacts", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                o.run_slots([self.slot()], self.out)
        self.assertTrue((self.out/"images/piece.png").exists())
        self.assertEqual(self.load("artifact_manifest.json")["status"], "failed")
        self.assertIn("images/piece.png", self.load("artifact_manifest.json")["generated_files"])
        self.review.invoke.return_value = response(REJECT)
        o.run_slots([self.slot()], self.out)
        self.assertFalse((self.out/"images/piece.png").exists())

    def test_legacy_exports_are_reconciled_on_first_new_run(self):
        self.out.mkdir()
        (self.out/"posts").mkdir()
        (self.out/"images").mkdir()
        (self.out/"posts.json").write_text(json.dumps([{"slot_id": "old"}]), encoding="utf-8")
        (self.out/"posts/old.json").write_text("{}", encoding="utf-8")
        (self.out/"images/old.png").write_bytes(b"old")
        Orchestrator(provider="ollama").run_slots([self.slot()], self.out)
        self.assertFalse((self.out/"posts/old.json").exists())
        self.assertFalse((self.out/"images/old.png").exists())

    def test_output_lock_blocks_week_and_single_slot_without_overwriting(self):
        self.out.mkdir()
        lock = self.out/".pipeline.lock"
        lock.write_text("another run", encoding="utf-8")
        o = Orchestrator(provider="ollama")
        with self.assertRaisesRegex(RuntimeError, "locked"):
            o.run_slots([self.slot()], self.out)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            o.run_slot(self.slot(), self.out)
        self.assertEqual(lock.read_text(), "another run")
        self.marketing.invoke.assert_not_called()

    def test_invalid_ids_services_and_mutations_are_rejected(self):
        for sid in ("../../victim", "a/b", "a\\b", "C:escape", "CON", "LPT1", "", ".", "a."):
            with self.subTest(sid=sid), self.assertRaises(ValidationError):
                self.slot(sid)
        with self.assertRaises(ValidationError):
            self.slot(service="agentic-securty")
        slot = self.slot()
        with self.assertRaises(ValidationError):
            slot.id = "../victim"

    def test_construct_bypass_cannot_escape_visual_or_post_output(self):
        slot = Slot.model_construct(id="../../victim", type="marketing-post", status="accepted")
        with self.assertRaises(ValueError):
            VisualAgent().generate_visual(slot, self.out)
        with self.assertRaises(ValueError):
            Orchestrator(provider="ollama")._export_artifacts([slot], self.out)
        self.assertFalse((Path(self.temp.name)/"victim.png").exists())
        self.assertFalse((Path(self.temp.name)/"victim.json").exists())

    def test_duplicate_and_case_colliding_ids_fail_before_any_output(self):
        for ids in (("same", "same"), ("Same", "same")):
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, "Duplicate"):
                Orchestrator(provider="ollama").run_slots([self.slot(i) for i in ids], self.out)
        self.assertFalse(self.out.exists())
        self.marketing.invoke.assert_not_called()

    def test_plan_validation_and_unknown_overrides(self):
        plan = Path(self.temp.name)/"plan.json"
        o = Orchestrator(provider="ollama")
        for payload in ({}, {"slots": []}, {"slots": [{"id": "x", "type": "marketing-post", "service": "typo"}]},
                        {"slots": [{"id": "x", "type": "marketing-post", "status": "accepted"}]}):
            plan.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                o.load_plan(plan)
        plan.write_text(json.dumps({"slots": [{"id": "x", "type": "marketing-post"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unknown slot"):
            o.load_plan(plan, {"missing": "topic"})
        self.assertEqual(o.load_plan(plan, {"x": "kb:SS-03699"})[0].focus, "kb:SS-03699")

    def test_output_path_rejects_escape_and_external_symlink(self):
        with self.assertRaises(ValueError):
            output_path(self.out, "..", "victim.json")
        self.out.mkdir()
        external = Path(self.temp.name)/"outside"
        external.mkdir()
        try:
            (self.out/"images").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit creating symlinks")
        with self.assertRaises(ValueError):
            output_path(self.out, "images", "piece.png")

    def test_failed_atomic_replace_leaves_previous_file_and_no_latest_fallback(self):
        self.out.mkdir()
        target = self.out/"posts.json"
        target.write_text("old", encoding="utf-8")
        with patch("agents.orchestrator.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                Orchestrator(provider="ollama")._safe_write_json(target, [])
        self.assertEqual(target.read_text(), "old")
        self.assertFalse((self.out/"posts_latest.json").exists())
        self.assertEqual(list(self.out.glob(".pipeline-*.tmp")), [])

    def test_independent_orchestrators_keep_provider_and_tracker_state(self):
        before = settings.DEFAULT_PROVIDER
        a = Orchestrator(provider="ollama", model_name="first")
        b = Orchestrator(provider="gemini", model_name="second")
        a._start_run()
        b._start_run()
        self.assertEqual(a.services.marketing_writer.model_name, "first")
        self.assertEqual(b.services.marketing_writer.model_name, "second")
        self.assertIsNot(a.services.tracker, b.services.tracker)
        self.assertIsNot(a.services.research, b.services.research)
        self.assertEqual(settings.DEFAULT_PROVIDER, before)


class ImportAndCliTests(unittest.TestCase):
    def test_public_modules_import_in_fresh_processes(self):
        for module in ("graph.pipeline", "agents.orchestrator", "agents.research_agent", "agents.llm_factory"):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-B", "-c", f"import {module}"], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_week_cli_forwards_model_and_demo_flags(self):
        import run_week
        fake = Mock()
        fake.run_week.return_value = SimpleNamespace(pieces_dropped=0)
        with patch.object(sys, "argv", ["run_week.py", "--provider", "ollama", "--model", "selected", "--demo-research"]), patch.object(run_week, "Orchestrator", return_value=fake) as factory:
            self.assertEqual(run_week.main(), 0)
        factory.assert_called_once_with(provider="ollama", model_name="selected", demo_mode=True)

    def test_piece_cli_forwards_model_and_demo_flags(self):
        import run_piece
        fake = Mock()
        fake.run_piece.return_value = (Slot(id="single_piece", type="tech-thread", status="dropped", rejection_reason="expected"), SimpleNamespace(pieces_dropped=1))
        with patch.object(sys, "argv", ["run_piece.py", "--type", "tech-thread", "--topic", "SS-03699", "--model", "selected", "--provider", "ollama", "--demo-research"]), patch.object(run_piece, "Orchestrator", return_value=fake) as factory, patch.object(run_piece, "console", Mock()):
            self.assertEqual(run_piece.main(), 1)
        factory.assert_called_once_with(provider="ollama", model_name="selected", demo_mode=True)


if __name__ == "__main__":
    unittest.main()
