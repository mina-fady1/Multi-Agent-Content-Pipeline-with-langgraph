import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import List, Dict, Any, Optional
from urllib.parse import urlsplit
from config.settings import settings
from models.schemas import ResearchQueryLog

class WebSearchSkill:
    """
    Reusable skill for domain-restricted web research using Tavily API.
    Enforces allowlist per service line, global deny list, and logs every query and URL.
    Offline fixtures are available only through an explicit demo mode. They are
    never treated as retrieved live evidence.
    """

    def __init__(self, sources_path: Optional[Path] = None, demo_mode: bool = False):
        self.sources_path = Path(sources_path or settings.SOURCES_FILE)
        self.demo_mode = demo_mode
        self._sources: Dict[str, Any] = {}
        self._deny_list: List[str] = []
        self._retrieved_urls_this_run: set[str] = set()
        self._fixture_urls_this_run: set[str] = set()
        self._query_logs: List[ResearchQueryLog] = []
        self._lock = Lock()
        self._load_sources()

    def _load_sources(self) -> None:
        if not self.sources_path.exists():
            raise FileNotFoundError(f"Sources file not found at {self.sources_path}")
        with open(self.sources_path, "r", encoding="utf-8") as f:
            self._sources = json.load(f)
        self._deny_list = [d.strip().lower().rstrip(".") for d in self._sources.get("_deny", [])]

    def get_allowed_domains(self, service: Optional[str]) -> List[str]:
        services = {
            key: value for key, value in self._sources.items()
            if not key.startswith("_") and isinstance(value, dict)
        }
        if service is not None and service not in services:
            raise ValueError(f"Unsupported research service: {service!r}")
        selected = services.values() if service is None else [services[service]]
        allowed = set()
        for service_dict in selected:
            for domains in service_dict.values():
                if isinstance(domains, list):
                    allowed.update(d.strip().lower().rstrip(".") for d in domains)
        return sorted(domain for domain in allowed if not any(
            domain == denied or domain.endswith("." + denied) for denied in self._deny_list
        ))

    def is_domain_allowed(self, url: str, allowed_domains: List[str]) -> bool:
        try:
            if not isinstance(url, str) or "\\" in url or any(c.isspace() or ord(c) < 32 for c in url):
                return False
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"}:
                return False
            if parsed.username is not None or parsed.password is not None or not parsed.hostname:
                return False
            # Accessing port validates malformed/non-numeric ports.
            _ = parsed.port
            if parsed.hostname.endswith(".."):
                return False
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
            if len(hostname) > 253 or any(not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label
            ) for label in hostname.split(".")):
                return False
            for deny in self._deny_list:
                if hostname == deny or hostname.endswith("." + deny):
                    return False
            for allow in allowed_domains:
                allow = allow.lower().rstrip(".")
                if hostname == allow or hostname.endswith("." + allow):
                    return True
            return False
        except Exception:
            return False

    def search(
        self,
        query: str,
        slot_id: str,
        post_type: str,
        service: Optional[str] = None,
        max_results: int = 5,
    ) -> List[Dict[str, str]]:
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise ValueError("max_results must be a positive integer")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Research query must not be blank")
        allowed_domains = []

        def log(results, requested_domains, status, error=None):
            urls = [result["url"] for result in results]
            mode = "demo" if self.demo_mode else "live"
            notes = (
                f"Demo fixtures only: {len(results)} items; no live retrieval occurred."
                if self.demo_mode else
                f"Live search returned {len(results)} usable domain-filtered items."
            )
            if error:
                notes += f" Error: {error}"
            entry = ResearchQueryLog(
                timestamp=datetime.now(timezone.utc).isoformat(),
                slot_id=slot_id, post_type=post_type, service=service, query=query,
                allowed_domains=allowed_domains, requested_domains=requested_domains,
                mode=mode, status=status, error=error, results_count=len(results),
                # Fixture URLs deliberately never appear in a live retrieval log.
                retrieved_urls=[] if self.demo_mode else urls,
                fixture_urls=urls if self.demo_mode else [], notes=notes,
            )
            with self._lock:
                self._query_logs.append(entry)
                target = self._fixture_urls_this_run if self.demo_mode else self._retrieved_urls_this_run
                target.update(urls)

        try:
            allowed_domains = self.get_allowed_domains(service)
        except (TypeError, ValueError) as exc:
            log([], [], "error", self._safe_error(exc))
            raise

        if self.demo_mode:
            results = self._get_fixture_results(query, service, allowed_domains)[:max_results]
            log(results, [], "fixture" if results else "empty")
            return results
        if not allowed_domains:
            log([], [], "error", "No allowed domains are configured for this service.")
            return []
        if not settings.TAVILY_API_KEY:
            log([], [], "error", "TAVILY_API_KEY is not configured; live research was not attempted.")
            return []
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=settings.TAVILY_API_KEY)
        except Exception as exc:
            log([], [], "error", self._safe_error(exc))
            return []

        results: List[Dict[str, str]] = []
        seen_urls: set[str] = set()
        # Each batch is actually submitted and logged, including domains beyond
        # the provider's per-request limit. The final public result stays bounded.
        for start in range(0, len(allowed_domains), 10):
            requested_domains = allowed_domains[start:start + 10]
            batch_results = []
            try:
                response = client.search(
                    query=query, search_depth="advanced", include_domains=requested_domains,
                    max_results=min(max_results, 20),
                )
                if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                    raise ValueError("Search provider returned an invalid results payload")
                batch_seen = set()
                for item in response["results"]:
                    if not isinstance(item, dict):
                        continue
                    url, content = item.get("url", ""), item.get("content", "")
                    if (not self.is_domain_allowed(url, requested_domains)
                            or not isinstance(content, str) or not content.strip()
                            or url in batch_seen):
                        continue
                    batch_seen.add(url)
                    title = item.get("title")
                    batch_results.append({
                        "title": title.strip() if isinstance(title, str) and title.strip() else url,
                        "url": url, "content": content.strip(), "provenance": "live",
                    })
                log(batch_results, requested_domains, "success" if batch_results else "empty")
                for result in batch_results:
                    if result["url"] not in seen_urls:
                        seen_urls.add(result["url"])
                        results.append(result)
            except Exception as exc:
                log([], requested_domains, "error", self._safe_error(exc))
        return results[:max_results]

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Keep provider failures diagnosable without exposing an API key."""
        detail = str(exc)
        if settings.TAVILY_API_KEY:
            detail = detail.replace(settings.TAVILY_API_KEY, "[redacted]")
        return f"{type(exc).__name__}: {detail}"

    def _get_fixture_results(
        self, query: str, service: Optional[str], allowed_domains: List[str]
    ) -> List[Dict[str, str]]:
        """Synthetic examples for explicit offline demos; not verified evidence."""
        fixtures = {
            "agentic-security": [
                {
                    "title": "Prompt Injection Attacks and LLM Tool Use Risks",
                    "url": "https://simonwillison.net/2023/Apr/14/prompt-injection-tools/",
                    "content": "Why prompt injection cannot be stopped simply by hiding the system prompt or private keys. The real attack vector is manipulating model instructions when the model has access to external tools and signing capabilities. Policy limits at the tool level are required.",
                },
                {
                    "title": "OWASP Top 10 for Large Language Model Applications",
                    "url": "https://owasp.org/www-project-top-10-for-large-language-model-applications/",
                    "content": "LLM01: Prompt Injection and LLM02: Sensitive Information Disclosure. AI agents with direct financial permissions require explicit approval boundaries and execution policy constraints rather than blind natural language trust.",
                },
                {
                    "title": "AI Agent Red Teaming: Bypassing Guardrails via Indirect Injection",
                    "url": "https://lakera.ai/blog/agent-red-teaming-indirect-injection",
                    "content": "Case study analyzing how untrusted Web2 data ingested into an autonomous agent's context caused unauthorized transaction invocation on Solana and EVM bridge contracts.",
                },
            ],
            "agentic-payments": [
                {
                    "title": "x402 Protocol Specification and Architecture",
                    "url": "https://x402.org/specs/protocol-v1",
                    "content": "The x402 open standard for agentic payments enables automated micro-settlements over HTTP 402 Payment Required. Contrast with MPP (Machine Payment Protocol): x402 focuses on stateless HTTP negotiation, while MPP relies on persistent facilitator channels.",
                },
                {
                    "title": "Agentic Micropayment Infrastructure and Facilitator Risk",
                    "url": "https://coinbase.com/blog/agentic-payments-facilitator-trust",
                    "content": "Analyzing transaction volume across AI agent payment facilitators. Protocols with unvetted facilitators expose agents to settlement frontrunning and unauthorized balance drainage.",
                },
            ],
            "pentesting": [
                {
                    "title": "Supply Chain and Dependency Hijacking in Web3 Infrastructure",
                    "url": "https://portswigger.net/daily-swig/malicious-npm-packages-drain-crypto-wallets",
                    "content": "Over $400M lost in Web3 protocols was traced not to smart contract flaws, but to poisoned npm packages and compromised CI/CD workflows injecting malicious frontend payloads.",
                },
                {
                    "title": "Developer Laptop Compromise Leading to Protocol Key Exfiltration",
                    "url": "https://bleepingcomputer.com/news/security/defi-protocol-hacked-via-dev-laptop/",
                    "content": "Analysis of multi-million dollar protocol drains where clean smart contract audits provided false security; attacker compromised developer cloud credentials and manipulated frontend RPC endpoints.",
                },
            ],
            "web3-security": [
                {
                    "title": "Solana Account Confusion and CPI Privilege Escalation Invariants",
                    "url": "https://samczsun.com/the-anatomy-of-a-cross-program-invocation-exploit/",
                    "content": "Formal reasoning about account ownership and signer privilege escalation in Solana programs. Why missing signer checks during CPI calls allow arbitrary account draining.",
                },
                {
                    "title": "Rekt Leaderboard: Smart Contract Logic vs Infrastructure Failures",
                    "url": "https://rekt.news/leaderboard/",
                    "content": "Post-mortem analysis showing that 58% of top decentralized finance losses involved cross-layer attack vectors including oracle manipulation, bridge relayer compromises, and compromised private keys.",
                },
            ],
        }

        service_fixtures = fixtures.get(service, []) if service is not None else [
            item for group in fixtures.values() for item in group
        ]
        filtered = [dict(item, provenance="fixture") for item in service_fixtures
                    if self.is_domain_allowed(item["url"], allowed_domains)]
        return filtered

    def get_retrieved_urls(self) -> set[str]:
        with self._lock:
            return set(self._retrieved_urls_this_run)

    def get_fixture_urls(self) -> set[str]:
        with self._lock:
            return set(self._fixture_urls_this_run)

    def get_query_logs(self) -> List[ResearchQueryLog]:
        with self._lock:
            return [entry.model_copy(deep=True) for entry in self._query_logs]

web_search_skill = WebSearchSkill()
