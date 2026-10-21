# Content Review Rubric

The Reviewer Agent scores every piece against this rubric. A piece must satisfy all dimensions to be accepted. If rejected, the Reviewer must provide specific, actionable feedback for the writer to revise the piece.

## Dimensions & Scoring Criteria

### 1. Grounding & Factual Integrity (Pass / Fail)
- **Hard requirement:** Every factual claim, vulnerability reference, statistic, or protocol behavior must trace directly to the provided Knowledge Base (`KB.json`), an explicitly retrieved URL from the run, or the user's focus input.
- **No hallucinated details:** If a metric, bug mechanism, or link was not in the source material, the piece fails immediately.
- **Link Integrity:** Any cited URL must match a URL retrieved during the current execution run.

### 2. Brand Voice & Tone (Pass / Fail)
- **Stance:** Technical, calm, authoritative, and direct. Written for builders, smart contract engineers, and security researchers.
- **Anti-Hype:** No sensationalism, buzzwords, or hollow claims.
- **Anti-AI Cadence:** Must read like an experienced human security researcher wrote it. Varied sentence lengths, authentic phrasing, no robotic transitional phrases ("Moreover", "Furthermore", "In conclusion", "It is worth noting").

### 3. Hedging Density Threshold
- **Threshold:** Maximum 2.0 hedging words (*may, might, could, generally, typically, arguably*) per 100 words.
- If the piece exceeds this threshold, flag it for excessive qualification and lack of directness.

### 4. Format & Length Conformance
- **Technical Thread:** 5 to 10 distinct tweets. First tweet must have a compelling technical hook without clickbait. Subsequent tweets break down root cause, exploit mechanics, real-world impact, and remediation. Each tweet <= 280 characters.
- **Marketing Post:** Single standalone post matching one of the approved formats (Short tip, Differentiation post, Service overview, News reaction, Learning resource, Thread). At most one call to action. Post <= 280 characters.

### 5. Positioning Alignment
- Does the piece reinforce SanadSquad's core thesis: "SanadSquad covers the whole attack surface, not just the contract"?
- Does it clearly show why contract-only audits leave critical protocol infrastructure, agent keys, or payment flows exposed?

## Review Verdict Format
The reviewer must return a structured evaluation:
- `verdict`: `"ACCEPT"` or `"REJECT"`
- `score`: Numeric score (1-10)
- `feedback`: Clear critique and instructions for revision (empty if accepted)
- `violations`: List of specific rule or rubric breaches
