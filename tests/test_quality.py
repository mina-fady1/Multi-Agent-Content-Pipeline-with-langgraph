import importlib
import json
import unittest
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage

from agents.marketing_writer import MarketingWriterAgent
from agents.reviewer_agent import ReviewerAgent
from agents.tech_writer import TechWriterAgent
from models.schemas import CodeQualityResult, Slot
from skills.quality_checker import QualityCheckerSkill


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.checker = QualityCheckerSkill()

    def check_post(self, text, urls=None, **kwargs):
        return self.checker.check_piece(text, "marketing-post", urls or set(), **kwargs)

    def test_blank_or_citation_only_content_is_rejected(self):
        for text in ("", " \n ", "...", "1/", "[KB: SS-03699]", "https://trusted.example/report"):
            with self.subTest(text=text):
                self.assertFalse(self.check_post(text, {"https://trusted.example/report"}).passed)

    def test_number_only_tweets_are_rejected(self):
        result = self.checker.check_piece("", "tech-thread", set(), [f"{n}/" for n in range(1, 6)])
        self.assertFalse(result.passed)

    def test_url_prefixes_queries_and_sibling_paths_are_not_retrieval(self):
        cases = (
            ("https://trusted.example/report", "https://trusted.example/report/fabricated"),
            ("https://trusted.example/report", "https://trusted.example/report-fabricated"),
            ("https://trusted.example", "https://trusted.example.attacker.test/report"),
            ("https://trusted.example/report?version=1", "https://trusted.example/report?version=2"),
            ("https://trusted.example/report/part", "https://trusted.example/report"),
        )
        for retrieved, cited in cases:
            with self.subTest(cited=cited):
                result = self.check_post(f"Read the incident analysis: {cited}", {retrieved})
                self.assertTrue(any("Unretrieved link" in item for item in result.violations))

    def test_url_equivalence_is_limited_to_same_document(self):
        result = self.check_post(
            "Read the incident analysis: HTTPS://TRUSTED.EXAMPLE:443/report#root-cause",
            {"https://trusted.example/report"},
        )
        self.assertTrue(result.passed, result.violations)
        self.assertEqual(self.checker.normalize_url("https://trusted.example"), "https://trusted.example/")

    def test_credentials_and_invalid_urls_cannot_be_authorized(self):
        for url in ("https://trusted.example:443@attacker.test/report", "ftp://trusted.example/report",
                    "https://trusted.example:bad/report", "https://trusted.example\\@attacker.test/report"):
            with self.subTest(url=url):
                self.assertIsNone(self.checker.normalize_url(url))

    def test_required_marketing_link_is_checked_without_adding_it(self):
        url = "https://trusted.example/report"
        result = self.check_post("Validate signer access before execution.", {url}, required_links=[url])
        self.assertFalse(result.passed)
        self.assertTrue(any("Required source citation missing" in item for item in result.violations))
        self.assertTrue(self.check_post(f"Read the incident analysis: {url}", {url}, required_links=[url]).passed)

    def test_kb_thread_requires_the_exact_finding_id(self):
        tweets = [f"{n}/ Validate the transaction owner before execution." for n in range(1, 6)]
        for citation in ("", " [KB: SS-036990]", " [KB: OTHER-SS-03699]"):
            with self.subTest(citation=citation):
                result = self.checker.check_piece(
                    "", "tech-thread", set(), tweets[:-1] + [tweets[-1] + citation], source_id="SS-03699"
                )
                self.assertFalse(result.passed)
        result = self.checker.check_piece(
            "", "tech-thread", set(), tweets[:-1] + [tweets[-1] + " [KB: SS-03699]"], source_id="SS-03699"
        )
        self.assertTrue(result.passed, result.violations)

    def test_external_technical_thread_requires_source_url(self):
        url = "https://trusted.example/report"
        tweets = [f"{n}/ Validate signer access." for n in range(1, 6)]
        self.assertFalse(self.checker.check_piece("", "tech-thread", {url}, tweets, source_url=url).passed)
        tweets[-1] += f" {url}"
        self.assertTrue(self.checker.check_piece("", "tech-thread", {url}, tweets, source_url=url).passed)

    def test_three_item_repetition_includes_oxford_and_plain_comma(self):
        for text in (
            "Review keys, accounts, and permissions. Inspect roles, balances, and transfers.",
            "Review keys, accounts and permissions. Inspect roles, balances and transfers.",
            "Review keys, accounts or permissions. Inspect roles, balances or transfers.",
        ):
            with self.subTest(text=text):
                result = self.check_post(text)
                self.assertTrue(result.rule_of_three_detected)
                self.assertFalse(result.passed)
        self.assertFalse(self.check_post("Review keys, accounts, and permissions.").rule_of_three_detected)

    def test_280_character_limit_is_enforced_on_complete_text(self):
        self.assertTrue(self.check_post("a" * 280).passed)
        self.assertFalse(self.check_post("a" * 281).passed)


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.reviewer = ReviewerAgent(tracker=Mock())
        self.accept = {"verdict": "ACCEPT", "score": 8.5, "feedback": "", "violations": []}

    def test_invalid_json_never_becomes_approval(self):
        for raw in ("", "Service unavailable", "null", "[]", "1", "true", "ACCEPT", "REJECT",
                    '{"verdict": "ACCEPT"}', "Prefix " + json.dumps(self.accept)):
            with self.subTest(raw=raw):
                result = self.reviewer._parse_json_result(raw)
                self.assertEqual(result.verdict, "REJECT")
                self.assertIn("Invalid reviewer response", result.violations)

    def test_invalid_schema_fields_never_become_approval(self):
        changes = (
            {"verdict": "accept"}, {"verdict": True}, {"score": "8.5"}, {"score": True},
            {"score": -1}, {"score": 0}, {"score": 11}, {"score": float("nan")},
            {"score": float("inf")}, {"feedback": None}, {"feedback": "Unresolved factual issue"},
            {"violations": "bad"}, {"violations": [1]}, {"violations": ["Fabricated fact"]},
            {"extra": True},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.reviewer._parse_json_result(json.dumps(self.accept | change)).verdict, "REJECT")
        for missing in self.accept:
            payload = self.accept.copy()
            payload.pop(missing)
            self.assertEqual(self.reviewer._parse_json_result(json.dumps(payload)).verdict, "REJECT")

    def test_duplicate_verdicts_are_rejected(self):
        raw = '{"verdict":"REJECT","verdict":"ACCEPT","score":8.5,"feedback":"","violations":[]}'
        self.assertEqual(self.reviewer._parse_json_result(raw).verdict, "REJECT")

    def test_valid_json_and_whole_json_fence_are_accepted(self):
        raw = json.dumps(self.accept)
        for content in (raw, "```json\n" + raw + "\n```"):
            self.assertEqual(self.reviewer._parse_json_result(content).verdict, "ACCEPT")
        rejected = self.reviewer._parse_json_result(json.dumps({
            "verdict": "REJECT", "score": 4, "feedback": "Remove the unsupported number.",
            "violations": ["Unsupported number"],
        }))
        self.assertEqual(rejected.feedback, "Remove the unsupported number.")

    def test_gate_failure_skips_the_model(self):
        slot = Slot(id="review", type="marketing-post", draft_content="")
        with patch("agents.reviewer_agent.get_chat_model") as factory:
            result = self.reviewer.review(slot, CodeQualityResult(passed=False, violations=["Empty text"]))
        factory.assert_not_called()
        self.assertEqual(result.verdict, "REJECT")
        self.assertEqual(slot.status, "reviewed")

    def test_reviewer_uses_all_policies_and_structured_text(self):
        self.reviewer.instructions = "review-policy-marker"
        self.reviewer.rubric = "rubric-policy-marker"
        self.reviewer.brand_voice = "brand-policy-marker"
        self.reviewer.marketing_rules = "marketing-policy-marker"
        llm = Mock()
        raw = json.dumps(self.accept)
        llm.invoke.return_value = AIMessage(content=[{"type": "text", "text": raw}])
        slot = Slot(id="review", type="marketing-post", draft_content="Validate signer access.")
        with patch("agents.reviewer_agent.get_chat_model", return_value=(llm, "ollama")):
            result = self.reviewer.review(slot, CodeQualityResult(passed=True))
        self.assertEqual(result.verdict, "ACCEPT")
        prompt = llm.invoke.call_args.args[0][0].content
        for marker in (self.reviewer.instructions, self.reviewer.rubric, self.reviewer.brand_voice, self.reviewer.marketing_rules):
            self.assertIn(marker, prompt)

    def test_empty_or_nontext_review_rejects(self):
        for content in ("", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]):
            llm = Mock()
            llm.invoke.return_value = AIMessage(content=content)
            slot = Slot(id="review", type="marketing-post", draft_content="Validate signer access.")
            with patch("agents.reviewer_agent.get_chat_model", return_value=(llm, "ollama")):
                result = self.reviewer.review(slot, CodeQualityResult(passed=True))
            self.assertEqual(result.verdict, "REJECT")


class WriterTests(unittest.TestCase):
    def invoke_writer(self, writer, slot, response, feedback=None):
        module = importlib.import_module(writer.__class__.__module__)
        llm = Mock()
        llm.invoke.return_value = AIMessage(content=response)
        with patch.object(module, "get_chat_model", return_value=(llm, "ollama")) as factory:
            result = writer.write(slot, feedback=feedback)
        factory.assert_called_once_with(writer.provider, writer.model_name)
        return result, llm.invoke.call_args.args[0][0].content

    def test_marketing_retains_overlong_text_and_raw_response(self):
        writer = MarketingWriterAgent(provider="ollama", model_name="test-model", tracker=Mock())
        raw = "Validate signer access. " * 20 + "Complete final sentence."
        slot, _ = self.invoke_writer(writer, Slot(id="m", type="marketing-post"), raw)
        self.assertEqual(slot.raw_draft_content, raw)
        self.assertEqual(slot.draft_content, raw)
        self.assertFalse(QualityCheckerSkill().check_piece(raw, slot.type, set(), slot.draft_tweets).passed)
        self.assertEqual(writer.tracker.record_usage.call_args.kwargs["model_name"], "test-model")
        self.assertEqual(writer.tracker.record_usage.call_args.kwargs["provider"], "ollama")

    def test_marketing_required_link_is_not_silently_appended(self):
        writer = MarketingWriterAgent(tracker=Mock())
        slot = Slot(id="m", type="marketing-post", format="news-reaction", source_url="https://trusted.example/report")
        slot, _ = self.invoke_writer(writer, slot, "Validate signer access before execution.")
        self.assertEqual(slot.links, [])
        result = QualityCheckerSkill().check_piece(
            slot.draft_content, slot.type, {slot.source_url}, slot.draft_tweets, required_links=[slot.source_url]
        )
        self.assertFalse(result.passed)

    def test_marketing_prompt_contains_policies_prior_draft_and_feedback(self):
        writer = MarketingWriterAgent(tracker=Mock())
        writer.instructions = "writer-instruction-marker"
        writer.brand_voice = "writer-brand-marker"
        writer.marketing_rules = "writer-marketing-marker"
        slot = Slot(id="m", type="marketing-post", draft_content="Previous draft marker.")
        _, prompt = self.invoke_writer(writer, slot, [{"type": "text", "text": "Validate signer access."}], "Feedback marker.")
        for marker in (writer.instructions, writer.brand_voice, writer.marketing_rules, "Previous draft marker.", "Feedback marker."):
            self.assertIn(marker, prompt)
        self.assertEqual(slot.draft_content, "Validate signer access.")

    def test_technical_thread_preserves_trailing_citation_and_long_tweet(self):
        writer = TechWriterAgent(tracker=Mock())
        raw = "\n".join([f"{n}/ Validate signer access." for n in range(1, 5)])
        raw += "\n5/ " + "Check ownership. " * 25 + "https://trusted.example/report"
        slot = Slot(id="t", type="tech-thread", source_type="web", source_url="https://trusted.example/report")
        slot, _ = self.invoke_writer(writer, slot, [{"type": "text", "text": raw}])
        self.assertEqual(len(slot.draft_tweets), 5)
        self.assertGreater(len(slot.draft_tweets[-1]), 280)
        self.assertTrue(slot.draft_tweets[-1].endswith(slot.source_url))
        self.assertEqual(slot.raw_draft_content, raw)

    def test_technical_prompt_contains_policies_prior_draft_and_feedback(self):
        writer = TechWriterAgent(tracker=Mock())
        writer.instructions = "tech-instruction-marker"
        writer.brand_voice = "tech-brand-marker"
        slot = Slot(id="t", type="tech-thread", source_type="web", source_url="https://trusted.example/report", draft_content="Previous tech marker.")
        _, prompt = self.invoke_writer(writer, slot, "1/ Complete text.", "Tech feedback marker.")
        for marker in (writer.instructions, writer.brand_voice, "Previous tech marker.", "Tech feedback marker."):
            self.assertIn(marker, prompt)

    def test_bracket_numbering_is_parsed_without_losing_text(self):
        tweets = TechWriterAgent(tracker=Mock())._parse_tweets("[1] First complete tweet.\n[2] Second complete tweet.")
        self.assertEqual(tweets, ["1/ First complete tweet.", "2/ Second complete tweet."])

    def test_empty_writer_response_fails_instead_of_creating_numbered_blank(self):
        writer = TechWriterAgent(tracker=Mock())
        slot = Slot(id="t", type="tech-thread", source_type="web", source_url="https://trusted.example/report")
        with self.assertRaises(ValueError):
            self.invoke_writer(writer, slot, "")
        self.assertEqual(writer._parse_tweets(""), [])


if __name__ == "__main__":
    unittest.main()
