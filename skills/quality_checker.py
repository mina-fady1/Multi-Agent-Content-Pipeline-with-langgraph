import re
from pathlib import Path
from typing import List, Set, Optional
from urllib.parse import urlsplit, urlunsplit
from config.settings import settings
from models.schemas import CodeQualityResult

class QualityCheckerSkill:
    """
    Code-level quality gate. Executes before the LLM Reviewer.
    Enforces hard constraints in code:
    - Banned characters (em dash, en dash as separator, curly quotes, emoji, hashtags)
    - Banned vocabulary from banned-words.txt
    - Banned constructions (negative parallelism, copula avoidance, filler openers)
    - Rule of three repetition
    - Hedging density threshold
    - Sentence length variance calculation & logging
    - URL retrieval verification against run's retrieved URLs
    - Length and post count constraints
    """

    HEDGING_WORDS = {
        "may", "might", "could", "generally", "typically",
        "arguably", "possibly", "probably", "seemingly", "largely"
    }

    HEDGING_THRESHOLD = 2.0  # Max per 100 words

    def __init__(self, banned_words_path: Optional[Path] = None):
        self.banned_words_path = banned_words_path or settings.BANNED_WORDS_FILE
        self._banned_words: List[str] = []
        self._load_banned_words()

    def _load_banned_words(self) -> None:
        if not self.banned_words_path.exists():
            return
        with open(self.banned_words_path, "r", encoding="utf-8") as f:
            lines = [line.strip().lower() for line in f if line.strip() and not line.startswith("#")]
        self._banned_words = lines

    def check_piece(
        self,
        text: str,
        post_type: str,
        retrieved_urls: Set[str],
        tweets: Optional[List[str]] = None,
        source_id: Optional[str] = None,
        source_url: Optional[str] = None,
        required_links: Optional[List[str]] = None,
    ) -> CodeQualityResult:
        violations: List[str] = []
        banned_chars_found: List[str] = []
        banned_words_found: List[str] = []
        banned_constructions_found: List[str] = []

        combined_text = "\n".join(tweets) if (tweets and len(tweets) > 0) else text
        if not self._has_content(combined_text):
            violations.append("Content must contain meaningful, nonempty text.")

        # 1. Banned Characters
        if "—" in combined_text or "\u2014" in combined_text:
            banned_chars_found.append("em dash (—)")
        if " – " in combined_text or " \u2013 " in combined_text:
            banned_chars_found.append("en dash used as separator ( – )")
        for cq in ["“", "”", "‘", "’"]:
            if cq in combined_text:
                banned_chars_found.append(f"curly quote ({cq})")
        # Emoji detection
        emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"  # emoticons
            "\U0001F300-\U0001F5FF"  # symbols & pictographs
            "\U0001F680-\U0001F6FF"  # transport & map
            "\U0001F1E0-\U0001F1FF"  # flags
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "\U0001F900-\U0001F9FF"  # supplemental symbols
            "\U0001FA70-\U0001FAFF"
            "]+",
            flags=re.UNICODE,
        )
        if emoji_pattern.search(combined_text):
            banned_chars_found.append("emoji")

        # Hashtags
        without_urls = re.sub(r"https?://[^\s)\]>]+", "", combined_text, flags=re.IGNORECASE)
        if re.search(r"#\w+", without_urls):
            banned_chars_found.append("hashtag (#)")

        if banned_chars_found:
            violations.append(f"Banned characters or symbols detected: {', '.join(set(banned_chars_found))}")

        # 2. Banned Vocabulary
        for bw in self._banned_words:
            # Word boundary regex
            pattern = rf"\b{re.escape(bw)}\b"
            if re.search(pattern, combined_text, flags=re.IGNORECASE):
                banned_words_found.append(bw)

        if banned_words_found:
            violations.append(f"Banned vocabulary detected: {', '.join(set(banned_words_found))}")

        # 3. Banned Constructions
        # Negative parallelism
        if re.search(r"\bnot\s+just\b.+?\bbut\b", combined_text, flags=re.IGNORECASE | re.DOTALL):
            banned_constructions_found.append("negative parallelism ('not just X, but Y')")
        if re.search(r"\bnot\s+only\b.+?\bbut\s+also\b", combined_text, flags=re.IGNORECASE | re.DOTALL):
            banned_constructions_found.append("negative parallelism ('not only ... but also')")

        # Copula avoidance
        if re.search(r"\bserves\s+as\b", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("copula avoidance ('serves as')")
        if re.search(r"\bstands\s+as\b", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("copula avoidance ('stands as')")
        if re.search(r"\bboasts\b", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("copula avoidance ('boasts')")

        # Filler openers
        if re.search(r"(?:^|\n)\s*in\s+today's\s+fast-paced\s+world", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("filler opener ('In today's fast-paced world')")
        if re.search(r"\bit\s+is\s+important\s+to\s+note\b", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("filler opener ('It is important to note')")
        if re.search(r"\bit\s+is\s+worth\s+noting\b", combined_text, flags=re.IGNORECASE):
            banned_constructions_found.append("filler opener ('It is worth noting')")

        if banned_constructions_found:
            violations.append(f"Banned constructions detected: {', '.join(set(banned_constructions_found))}")

        # 4. Rule of Three
        # Pattern like: word, word, and word OR word, word and word
        rule_of_three_pattern = re.compile(
            r"\b[A-Za-z0-9_-]+,\s+[A-Za-z0-9_-]+,?\s+(?:and|or)\s+[A-Za-z0-9_-]+\b",
            re.IGNORECASE
        )
        matches = rule_of_three_pattern.findall(combined_text)
        rule_of_three_detected = len(matches) >= 2
        if rule_of_three_detected:
            violations.append(f"Rule of three repetition detected ({len(matches)} parallel structures).")

        # 5. Hedging Density
        words = re.findall(r"\b\w+\b", combined_text.lower())
        total_word_count = len(words)
        hedging_count = sum(1 for w in words if w in self.HEDGING_WORDS)
        hedging_density = (hedging_count / total_word_count * 100.0) if total_word_count > 0 else 0.0

        if hedging_density > self.HEDGING_THRESHOLD:
            violations.append(
                f"Hedging density ({hedging_density:.2f} per 100 words) exceeds rubric threshold ({self.HEDGING_THRESHOLD})."
            )

        # 6. Sentence Length Variance
        sentences = [s.strip() for s in re.split(r"[.!?]+", combined_text) if s.strip()]
        sentence_lengths = [len(re.findall(r"\b\w+\b", s)) for s in sentences if s]
        if len(sentence_lengths) > 1:
            mean = sum(sentence_lengths) / len(sentence_lengths)
            variance = sum((x - mean) ** 2 for x in sentence_lengths) / len(sentence_lengths)
        else:
            variance = 0.0

        # 7. Link Retrieval Verification
        # Extract all URLs in the text
        url_pattern = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
        found_urls = url_pattern.findall(combined_text)
        normalized_retrieved = {
            normalized for url in retrieved_urls
            if (normalized := self.normalize_url(url)) is not None
        }
        normalized_found = set()
        for u in found_urls:
            # Clean trailing punctuation
            clean_url = u.rstrip(".,;:'\"")
            normalized = self.normalize_url(clean_url)
            if normalized is not None:
                normalized_found.add(normalized)
            if normalized is None or normalized not in normalized_retrieved:
                violations.append(f"Unretrieved link in post: '{clean_url}' was not fetched during this run.")

        required = list(required_links or [])
        if post_type == "tech-thread":
            if source_id and not re.search(
                rf"(?<![\w-]){re.escape(source_id)}(?![\w-])", combined_text
            ):
                violations.append(f"Technical thread must cite its KB finding: {source_id}.")
            if source_url:
                required.append(source_url)
        for url in required:
            normalized = self.normalize_url(url)
            if normalized is None or normalized not in normalized_found:
                violations.append(f"Required source citation missing: {url}.")

        # 8. Post-Specific Length Constraints
        if post_type == "tech-thread":
            thread_list = tweets if (tweets and len(tweets) > 0) else [combined_text]
            if len(thread_list) < 5 or len(thread_list) > 10:
                violations.append(
                    f"Technical thread must have between 5 and 10 tweets (got {len(thread_list)})."
                )
            for i, tweet in enumerate(thread_list):
                if not self._has_content(tweet):
                    violations.append(f"Tweet #{i+1} must contain meaningful, nonempty text.")
                if len(tweet) > 280:
                    violations.append(
                        f"Tweet #{i+1} exceeds 280 characters limit ({len(tweet)} chars)."
                    )
        elif post_type == "marketing-post":
            if tweets is not None and len(tweets) != 1:
                violations.append("Marketing content must contain exactly one standalone post.")
            if len(combined_text) > 280:
                violations.append(
                    f"Marketing post exceeds 280 characters limit ({len(combined_text)} chars)."
                )

        else:
            violations.append(f"Unsupported post type: {post_type}.")

        passed = len(violations) == 0
        return CodeQualityResult(
            passed=passed,
            violations=violations,
            hedging_density=round(hedging_density, 2),
            sentence_length_variance=round(variance, 2),
            rule_of_three_detected=rule_of_three_detected,
            banned_words_found=banned_words_found,
            banned_chars_found=banned_chars_found,
            banned_constructions_found=banned_constructions_found,
        )

    @staticmethod
    def _has_content(text: str) -> bool:
        body = re.sub(r"https?://[^\s)\]>]+", "", text, flags=re.IGNORECASE)
        body = re.sub(r"\[KB(?: Finding)?:[^\]]*\]", "", body, flags=re.IGNORECASE)
        body = re.sub(r"(?m)^\s*(?:\d+[/.:)]|\[\d+\])\s*", "", body)
        return any(character.isalpha() for character in body)

    @staticmethod
    def normalize_url(url: str) -> Optional[str]:
        """Only equate host/scheme case, default ports, root slash, and document fragments.

        Paths and query strings remain exact: a retrieved page never authorizes
        a child page, hostname prefix, or a different query string.
        """
        if not isinstance(url, str) or not url or re.search(r"\s|\\", url):
            return None
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
            if scheme not in {"http", "https"} or not hostname or parsed.username is not None or parsed.password is not None:
                return None
            hostname = hostname.encode("idna").decode("ascii").lower()
            port = parsed.port
            host = f"[{hostname}]" if ":" in hostname else hostname
            if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
                host = f"{host}:{port}"
            return urlunsplit((scheme, host, parsed.path or "/", parsed.query, ""))
        except (ValueError, UnicodeError):
            return None

quality_checker_skill = QualityCheckerSkill()
