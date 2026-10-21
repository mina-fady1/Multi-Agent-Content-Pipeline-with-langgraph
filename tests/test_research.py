import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from agents.research_agent import ResearchAgent
from config.settings import settings
from models.schemas import Slot
from skills.kb_search import KnowledgeBaseSkill
from skills.web_search import WebSearchSkill


class WebSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source_path = Path(self.temp.name) / "sources.json"
        self.write_sources(["owasp.org", "portswigger.net", "bleepingcomputer.com"])

    def write_sources(self, domains):
        self.source_path.write_text(json.dumps({
            "pentesting": {"docs": domains}, "_deny": ["reddit.com"],
        }), encoding="utf-8")

    def skill(self, demo_mode=False):
        return WebSearchSkill(self.source_path, demo_mode=demo_mode)

    def search(self, skill, **kwargs):
        return skill.search("security guidance", "test", "marketing-post", "pentesting", **kwargs)

    def test_missing_key_fails_without_fixture_evidence(self):
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", ""), patch("tavily.TavilyClient") as client:
            self.assertEqual(self.search(skill), [])
        client.assert_not_called()
        log = skill.get_query_logs()[0]
        self.assertEqual((log.status, log.mode, log.requested_domains), ("error", "live", []))
        self.assertIn("TAVILY_API_KEY", log.error)
        self.assertEqual(skill.get_retrieved_urls(), set())
        self.assertEqual(skill.get_fixture_urls(), set())

    def test_provider_failure_is_logged_and_api_key_redacted(self):
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "secret-key"), patch("tavily.TavilyClient") as client:
            client.return_value.search.side_effect = RuntimeError("timeout using secret-key")
            self.assertEqual(self.search(skill), [])
        log = skill.get_query_logs()[0]
        self.assertEqual(log.status, "error")
        self.assertIn("timeout", log.error)
        self.assertNotIn("secret-key", log.error)
        self.assertEqual(log.results_count, 0)
        self.assertEqual(skill.get_retrieved_urls(), set())

    def test_empty_results_do_not_fallback(self):
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            client.return_value.search.return_value = {"results": []}
            self.assertEqual(self.search(skill), [])
        self.assertEqual(skill.get_query_logs()[0].status, "empty")

    def test_demo_is_explicit_bounded_and_separate_from_live_evidence(self):
        skill = self.skill(demo_mode=True)
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            results = self.search(skill, max_results=1)
        client.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["provenance"], "fixture")
        self.assertEqual(skill.get_retrieved_urls(), set())
        self.assertEqual(skill.get_fixture_urls(), {results[0]["url"]})
        log = skill.get_query_logs()[0]
        self.assertEqual((log.mode, log.status), ("demo", "fixture"))
        self.assertEqual(log.retrieved_urls, [])
        self.assertEqual(log.fixture_urls, [results[0]["url"]])
        self.assertEqual(log.requested_domains, [])

    def test_domain_checks_use_real_hostname_and_reject_credentials(self):
        skill = self.skill()
        invalid = [
            "https://owasp.org:443@reddit.com/path", "https://user@owasp.org/path",
            "https://owasp.org.attacker.test/path", "ftp://owasp.org/path",
            "javascript:owasp.org", "https://owasp.org:bad/path", "https://owasp.org\\@reddit.com/path",
            "https://owasp.org/path\n", "https://reddit.com/path", "https:///owasp.org/path",
            "https://.owasp.org/a", "https://owasp.org../a", "https://-bad.owasp.org/a",
            "https://%0areddit.com.owasp.org/a",
        ]
        for url in invalid:
            with self.subTest(url=url):
                self.assertFalse(skill.is_domain_allowed(url, ["owasp.org", "reddit.com"]))
        self.assertTrue(skill.is_domain_allowed("https://docs.owasp.org:443/report", ["owasp.org"]))
        self.assertTrue(skill.is_domain_allowed("https://OWASP.ORG/report", ["owasp.org"]))

    def test_unknown_services_cannot_expand_allowlist(self):
        skill = self.skill()
        for service in ["agentic-securty", "", "_deny"]:
            with self.subTest(service=service), self.assertRaises(ValueError):
                skill.get_allowed_domains(service)

    def test_invalid_search_service_is_logged_before_it_is_rejected(self):
        skill = self.skill()
        with self.assertRaises(ValueError):
            skill.search("test", "test", "marketing-post", service="typo")
        log = skill.get_query_logs()[0]
        self.assertEqual((log.status, log.requested_domains, log.results_count), ("error", [], 0))
        self.assertIn("Unsupported research service", log.error)

    def test_empty_allowlist_is_logged_without_querying_the_provider(self):
        self.write_sources([])
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            self.assertEqual(self.search(skill), [])
        client.assert_not_called()
        self.assertEqual(skill.get_query_logs()[0].status, "error")

    def test_every_domain_batch_is_submitted_and_logged(self):
        domains = [f"host{i:02d}.example" for i in range(23)]
        self.write_sources(domains)
        skill = self.skill()

        def response(**kwargs):
            return {"results": [{"url": f"https://{d}/report", "title": d, "content": "Evidence"}
                                for d in kwargs["include_domains"]]}

        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            client.return_value.search.side_effect = response
            results = self.search(skill, max_results=2)
            submitted = [d for call in client.return_value.search.call_args_list
                         for d in call.kwargs["include_domains"]]
        self.assertEqual(submitted, domains)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["provenance"] == "live" for result in results))
        logs = skill.get_query_logs()
        self.assertEqual([len(log.requested_domains) for log in logs], [10, 10, 3])
        self.assertEqual([d for log in logs for d in log.requested_domains], domains)
        self.assertTrue(all(log.status == "success" and log.error is None for log in logs))

    def test_partial_batch_failures_remain_visible(self):
        self.write_sources([f"host{i:02d}.example" for i in range(11)])
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            client.return_value.search.side_effect = [
                RuntimeError("batch unavailable"),
                {"results": [{"url": "https://host10.example/a", "content": "Evidence"}]},
            ]
            results = self.search(skill)
        self.assertEqual(len(results), 1)
        self.assertEqual([log.status for log in skill.get_query_logs()], ["error", "success"])

    def test_malformed_empty_and_unallowed_results_are_not_evidence(self):
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            client.return_value.search.return_value = {"results": [
                None, "not an object", {"url": "https://owasp.org/empty", "content": " "},
                {"url": "https://reddit.com/a", "content": "Evidence"},
                {"url": "https://owasp.org/a", "content": "Evidence", "title": "Report"},
                {"url": "https://owasp.org/a", "content": "Duplicate"},
            ]}
            results = self.search(skill)
        self.assertEqual(len(results), 1)
        self.assertEqual(skill.get_retrieved_urls(), {"https://owasp.org/a"})

    def test_malformed_provider_payload_is_error(self):
        skill = self.skill()
        with patch.object(settings, "TAVILY_API_KEY", "fake"), patch("tavily.TavilyClient") as client:
            client.return_value.search.return_value = {"results": None}
            self.assertEqual(self.search(skill), [])
        self.assertEqual(skill.get_query_logs()[0].status, "error")

    def test_snapshots_cannot_mutate_research_history(self):
        skill = self.skill(demo_mode=True)
        results = self.search(skill)
        skill.get_fixture_urls().clear()
        snapshot = skill.get_query_logs()
        snapshot[0].fixture_urls.clear()
        snapshot.clear()
        self.assertEqual(len(skill.get_fixture_urls()), len(results))
        self.assertEqual(len(skill.get_query_logs()[0].fixture_urls), len(results))


class KnowledgeResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "kb.json"
        path.write_text(json.dumps({"findings": [
            {"id": "SS-03699", "title": "Missing signer verification", "impact": "High",
             "tags": ["signer"], "content": {"description": "Signing authority is unchecked."}},
            {"id": "SS-03706", "title": "Signature replay protection", "impact": "High",
             "tags": ["replay"], "content": {"description": "Repeated signatures are accepted."}},
        ]}), encoding="utf-8")
        self.kb = KnowledgeBaseSkill(path)

    def agent(self):
        return ResearchAgent(kb=self.kb, web_search=Mock(spec=WebSearchSkill))

    def test_unmatched_explicit_topic_or_id_never_autopicks(self):
        agent = self.agent()
        for focus in ["galactic astrophysics", "SS-99999", "kb:SS-99999", "xy", "the and"]:
            with self.subTest(focus=focus):
                slot = agent.research_slot(Slot(id="test", type="tech-thread", focus=focus))
                self.assertEqual(slot.status, "failed")
                self.assertIsNone(slot.source_id)
        self.assertEqual(agent.used_kb_ids, [])

    def test_marketing_kb_id_is_exact_and_case_insensitive(self):
        agent = self.agent()
        for focus in ["kb:SS-03706", "KB:ss-03706", "vulnerability class: SS-03706"]:
            with self.subTest(focus=focus):
                slot = agent.research_slot(Slot(id="test", type="marketing-post", focus=focus))
                self.assertEqual(slot.status, "researched")
                self.assertEqual(slot.source_id, "SS-03706")
                self.assertEqual(slot.source_provenance, "kb")
                self.assertEqual(slot.format, "short-tip")
        agent.web_search.search.assert_not_called()

    def test_unmatched_marketing_kb_request_does_not_become_web_search(self):
        agent = self.agent()
        for focus in ["kb:SS-99999", "kb:", "vulnerability class", "vulnerability class: galactic astrophysics"]:
            with self.subTest(focus=focus):
                slot = agent.research_slot(Slot(id="test", type="marketing-post", focus=focus))
                self.assertEqual(slot.status, "failed")
        agent.web_search.search.assert_not_called()

    def test_parallel_blank_slots_receive_unique_findings_and_exhaust_honestly(self):
        agent = self.agent()
        slots = [Slot(id=f"test{i}", type="tech-thread", focus=" ") for i in range(10)]
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(agent.research_slot, slots))
        researched = [slot for slot in results if slot.status == "researched"]
        self.assertEqual({slot.source_id for slot in researched}, {"SS-03699", "SS-03706"})
        self.assertEqual(len(researched), 2)
        self.assertEqual(len(agent.used_kb_ids), 2)
        self.assertEqual(sum(slot.status == "failed" for slot in results), 8)

    def test_separate_agents_have_fresh_search_and_kb_allocation_state(self):
        first, second = ResearchAgent(kb=self.kb), ResearchAgent(kb=self.kb)
        self.assertIsNot(first.web_search, second.web_search)
        first_slot = first.research_slot(Slot(id="first", type="tech-thread"))
        second_slot = second.research_slot(Slot(id="second", type="tech-thread"))
        self.assertEqual(first_slot.source_id, second_slot.source_id)
        self.assertEqual(second.web_search.get_retrieved_urls(), set())
        self.assertEqual(second.web_search.get_query_logs(), [])

    def test_reserved_explicit_topics_cannot_be_taken_by_parallel_blank_slots(self):
        agent = self.agent()
        slots = [Slot(id=f"blank{i}", type="tech-thread") for i in range(6)]
        slots.append(Slot(id="explicit", type="marketing-post", focus="kb:SS-03699"))
        agent.reserve_plan_topics(slots)
        with ThreadPoolExecutor(max_workers=7) as pool:
            results = list(pool.map(agent.research_slot, slots))
        self.assertEqual(results[-1].source_id, "SS-03699")
        auto_ids = [slot.source_id for slot in results[:-1] if slot.status == "researched"]
        self.assertEqual(auto_ids, ["SS-03706"])
        self.assertEqual(set(agent.used_kb_ids), {"SS-03699", "SS-03706"})

    def test_research_failure_clears_previous_source_metadata(self):
        agent = self.agent()
        slot = agent.research_slot(Slot(id="test", type="tech-thread", focus="SS-03699"))
        self.assertEqual(slot.source_id, "SS-03699")
        slot.focus = "galactic astrophysics"
        slot.links = ["https://example.com/old"]
        agent.research_slot(slot)
        self.assertEqual(slot.status, "failed")
        self.assertIsNone(slot.source_id)
        self.assertIsNone(slot.source_type)
        self.assertIsNone(slot.source_provenance)
        self.assertIsNone(slot.source_summary)
        self.assertIsNone(slot.resolved_topic)
        self.assertEqual(slot.links, [])

    def test_injected_search_is_used_and_provenance_preserved(self):
        agent = self.agent()
        agent.web_search.search.return_value = [{
            "title": "Source", "url": "https://owasp.org/a", "content": "Evidence", "provenance": "live",
        }]
        for post_type, focus in [("marketing-post", "news on signing"), ("tech-thread", "EXTERNAL: signing")]:
            with self.subTest(post_type=post_type):
                slot = agent.research_slot(Slot(id="test", type=post_type, focus=focus, service="pentesting"))
                self.assertEqual(slot.status, "researched")
                self.assertEqual(slot.source_provenance, "live")
                self.assertEqual(slot.source_url, "https://owasp.org/a")
        self.assertEqual(agent.web_search.search.call_count, 2)
        self.assertEqual(agent.web_search.search.call_args.kwargs["service"], "pentesting")

    def test_live_search_failure_drops_research_without_kb_fallback(self):
        agent = self.agent()
        agent.web_search.search.return_value = []
        slot = agent.research_slot(Slot(id="test", type="marketing-post", focus="New research"))
        self.assertEqual(slot.status, "failed")
        self.assertEqual(agent.used_kb_ids, [])
        self.assertIsNone(slot.source_type)

    def test_kb_read_results_do_not_mutate_shared_records(self):
        all_findings = self.kb.get_all_findings()
        all_findings[0].title = "Corrupted"
        self.kb.get_by_id("SS-03699").content["description"] = "Corrupted"
        self.kb.search("signer")[0].tags.append("Corrupted")
        original = self.kb.get_by_id("SS-03699")
        self.assertEqual(original.title, "Missing signer verification")
        self.assertEqual(original.description, "Signing authority is unchecked.")
        self.assertNotIn("Corrupted", original.tags)

    def test_kb_search_no_fallback_or_exhausted_recycling(self):
        self.assertEqual(self.kb.search("galactic astrophysics"), [])
        self.assertEqual(self.kb.search("SS-99999"), [])
        self.assertEqual(self.kb.search("kb:SS-03706")[0].id, "SS-03706")
        self.assertIsNone(self.kb.get_unused_finding([], query="galactic astrophysics"))
        self.assertIsNone(self.kb.get_unused_finding(["SS-03699", "SS-03706"]))


if __name__ == "__main__":
    unittest.main()
