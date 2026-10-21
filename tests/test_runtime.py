import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw
from langchain_core.messages import AIMessage

from agents.llm_factory import TokenTracker, extract_response_text, get_chat_model
from config.settings import settings
from models.schemas import Slot
from skills.image_prompt_builder import ImagePromptBuilderSkill


def response_with_usage(input_tokens, output_tokens):
    return AIMessage(content="Validated content.", usage_metadata={
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    })


class ResponseAndModelTests(unittest.TestCase):
    def test_text_blocks_preserve_order_without_reasoning_or_container_repr(self):
        response = AIMessage(content=[
            {"type": "reasoning", "reasoning": "hidden internal reasoning"},
            {"type": "text", "text": "1/ First tweet.\n"},
            "2/ Second tweet.\n",
            {"type": "text", "text": "3/ Third tweet."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ])
        self.assertEqual(extract_response_text(response), "1/ First tweet.\n2/ Second tweet.\n3/ Third tweet.")
        self.assertEqual(extract_response_text(AIMessage(content=" Plain text. ")), " Plain text. ")

    def test_empty_and_unsupported_responses_fail_explicitly(self):
        for content in ("", " \n ", [], [{"type": "reasoning", "reasoning": "hidden"}], None, {"text": "unsupported"}):
            with self.subTest(content=content), self.assertRaises(ValueError):
                extract_response_text(SimpleNamespace(content=content))

    def test_explicit_gemini_model_reaches_provider_constructor(self):
        constructor = Mock(return_value=object())
        with patch.dict(sys.modules, {"langchain_google_genai": SimpleNamespace(ChatGoogleGenerativeAI=constructor)}), \
                patch.object(settings, "GEMINI_API_KEY", "offline-test-key"):
            model, provider = get_chat_model("gemini", "gemini-test-model", temperature=0.1)
        self.assertIs(model, constructor.return_value)
        self.assertEqual(provider, "gemini")
        self.assertEqual(constructor.call_args.kwargs["model"], "gemini-test-model")
        self.assertEqual(constructor.call_args.kwargs["temperature"], 0.1)

    def test_explicit_ollama_model_reaches_provider_constructor(self):
        constructor = Mock(return_value=object())
        with patch.dict(sys.modules, {"langchain_ollama": SimpleNamespace(ChatOllama=constructor)}):
            model, provider = get_chat_model("ollama", "local-test-model")
        self.assertIs(model, constructor.return_value)
        self.assertEqual(provider, "ollama")
        self.assertEqual(constructor.call_args.kwargs["model"], "local-test-model")

    def test_missing_key_does_not_silently_switch_provider(self):
        with patch.object(settings, "GEMINI_API_KEY", ""), self.assertRaisesRegex(ValueError, "GEMINI_API_KEY"):
            get_chat_model("gemini", "gemini-test-model")
        with self.assertRaisesRegex(ValueError, "Unknown LLM provider"):
            get_chat_model("unsupported")


class TokenTrackerTests(unittest.TestCase):
    def test_default_flash_rate_matches_model_specific_paid_text_pricing(self):
        tracker = TokenTracker()
        tracker.record_usage(response_with_usage(1_000_000, 1_000_000), provider="gemini", model_name="gemini-2.5-flash")
        summary = tracker.get_summary()
        self.assertAlmostEqual(summary.estimated_cost_usd, 2.8)
        self.assertTrue(summary.pricing_complete)
        self.assertFalse(summary.usage_estimated)
        self.assertEqual(summary.model_usage[0]["model"], "gemini-2.5-flash")
        self.assertTrue(summary.pricing_assumptions)

    def test_unknown_models_do_not_report_a_guessed_or_zero_total_cost(self):
        tracker = TokenTracker()
        tracker.record_usage(response_with_usage(100, 100), provider="gemini", model_name="unknown-model")
        summary = tracker.get_summary()
        self.assertFalse(summary.pricing_complete)
        self.assertIsNone(summary.estimated_cost_usd)
        self.assertIsNone(summary.model_usage[0]["estimated_cost_usd"])

    def test_mixed_models_keep_costs_and_usage_separate(self):
        tracker = TokenTracker()
        tracker.record_usage(response_with_usage(1_000_000, 1_000_000), provider="gemini", model_name="gemini-2.5-flash")
        tracker.record_usage(response_with_usage(20, 10), provider="ollama", model_name="local-model")
        tracker.record_usage(response_with_usage(10, 10), provider="gemini", model_name="unknown-model")
        summary = tracker.get_summary()
        self.assertEqual(summary.total_tokens, 2_000_050)
        self.assertEqual(len(summary.model_usage), 3)
        self.assertAlmostEqual(summary.known_cost_estimate_usd, 2.8)
        self.assertIsNone(summary.estimated_cost_usd)

    def test_parallel_usage_updates_are_not_lost_and_reset_isolated(self):
        tracker = TokenTracker()
        response = response_with_usage(11, 7)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda _: tracker.record_usage(response, provider="ollama", model_name="local"), range(400)))
        summary = tracker.get_summary()
        self.assertEqual(summary.prompt_tokens, 4400)
        self.assertEqual(summary.completion_tokens, 2800)
        self.assertEqual(summary.total_tokens, 7200)
        self.assertEqual(TokenTracker().get_summary().total_tokens, 0)
        tracker.reset("ollama")
        self.assertEqual(tracker.get_summary().total_tokens, 0)

    def test_missing_usage_is_labelled_as_an_estimate(self):
        tracker = TokenTracker()
        tracker.record_usage(AIMessage(content=[{"type": "text", "text": "abcd"}]), prompt_text="abcdefgh",
                             provider="ollama", model_name="local")
        summary = tracker.get_summary()
        self.assertTrue(summary.usage_estimated)
        self.assertEqual((summary.prompt_tokens, summary.completion_tokens, summary.total_tokens), (2, 1, 3))
        self.assertEqual(summary.estimated_cost_usd, 0)


class VisualTests(unittest.TestCase):
    def setUp(self):
        self.builder = ImagePromptBuilderSkill()

    @staticmethod
    def accepted_slot(**kwargs):
        fields = dict(id="visual", type="marketing-post", service="agentic-security", status="accepted",
                      resolved_topic="Signer permissions at tool boundaries", source_provenance="live",
                      source_summary="An unrelated raw-source fragment that must not appear in the visual.",
                      draft_content="Validate signer permissions before executing a tool request.",
                      draft_tweets=["Validate signer permissions before executing a tool request."])
        fields.update(kwargs)
        return Slot(**fields)

    def test_visual_evidence_labels_follow_provenance(self):
        for provenance, label in (("kb", "KB FINDING"), ("live", "WEB RESEARCH"),
                                  ("fixture", "DEMO FIXTURE"), (None, "RESEARCH NOTE")):
            with self.subTest(provenance=provenance):
                slot = self.accepted_slot(source_provenance=provenance)
                spec = self.builder.build_spec(slot)
                self.assertEqual(spec.evidence_label, label)
                self.assertNotIn("VERIFIED AUDIT", spec.model_dump_json())
                self.builder.validate_spec_alignment(slot, spec)

    def test_visual_takeaway_uses_accepted_content_and_removes_numbering(self):
        slot = self.accepted_slot(type="tech-thread", source_provenance="kb",
                                  draft_tweets=["1/ Validate signer permissions. [KB: SS-03699]"])
        spec = self.builder.build_spec(slot)
        self.assertEqual(spec.key_takeaway, "Validate signer permissions.")
        self.assertNotIn(slot.source_summary, spec.key_takeaway)

    def test_visual_alignment_rejects_changed_claim_title_service_or_label(self):
        slot = self.accepted_slot()
        original = self.builder.build_spec(slot)
        changes = {"title": "Invented exploit", "key_takeaway": "Guaranteed security.",
                   "subtitle": "Other service", "accent_color": "#FFFFFF", "evidence_label": "VERIFIED AUDIT"}
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.builder.validate_spec_alignment(slot, original.model_copy(update={field: value}))
        with self.assertRaises(ValueError):
            self.builder.validate_spec_alignment(self.accepted_slot(status="dropped"), original)

    def test_actual_render_produces_1200_by_675_png(self):
        slot = self.accepted_slot()
        spec = self.builder.build_spec(slot)
        with tempfile.TemporaryDirectory() as directory:
            path = self.builder.render_card_image(spec, Path(directory) / "card.png")
            with Image.open(path) as image:
                self.assertEqual(image.size, (1200, 675))
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.getpixel((25, 25)), (0, 229, 255))

    def test_pixel_layout_contains_long_words_and_unicode_within_each_box(self):
        draw = ImageDraw.Draw(Image.new("RGB", (1200, 675)))
        for text in ("SignerPermissions" * 80, "Audit boundaries " * 60,
                     "\u00c9xecution \u00c5udit \u00f6wnership " * 50):
            for width, height, size in ((1080, 155, 42), (1020, 123, 26), (440, 30, 20)):
                with self.subTest(text=text[:20], box=(width, height)):
                    font, lines, line_height = self.builder._fit_text(draw, text, width, height, size)
                    self.assertLessEqual(len(lines) * line_height, height)
                    for line in lines:
                        left, top, right, bottom = draw.textbbox((0, 0), line, font=font, anchor="lt")
                        # Rendering offsets the glyph bearing so its ink starts at the box edge.
                        self.assertLessEqual(right - left, width)
                        self.assertLessEqual(bottom - top, line_height)


if __name__ == "__main__":
    unittest.main()
