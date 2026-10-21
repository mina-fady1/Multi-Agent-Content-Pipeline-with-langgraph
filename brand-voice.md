# SanadSquad Brand Voice Guide

## Tone & Stance
- **Direct, technical, and grounded.** We write for engineers, protocol founders, and security researchers.
- **Skeptical of hype.** Avoid sensationalism, breathless claims, and hollow marketing language.
- **Calm authority.** We state facts, explain technical mechanics, and let the evidence speak.
- **No invented facts.** Every claim must trace back to the knowledge base, a source URL, or user input.

## Hard Rules & Banned Elements
The following rules are enforced in code before any piece reaches review:

1. **Banned Characters & Symbols:**
   - **No em dashes** (`—` or `\u2014`) and no en dashes used as separators (` – `). Use standard hyphens (`-`) or parentheses.
   - **No curly quotes** (`“`, `”`, `‘`, `’`). Use straight quotes (`"`, `'`).
   - **No emoji** under any circumstances.
   - **No hashtags** (`#web3`, `#security`, `#crypto`, etc.).

2. **Banned AI Vocabulary:**
   - Words from `banned-words.txt`, including: *delve, leverage, tapestry, underscore, foster, pivotal, realm, landscape, crucial, seamless, robust, navigate, testament, ever-evolving, game-changer, revolutionize, unleash, groundbreaking, beacon, harness*.

3. **Banned Syntactic Constructions:**
   - **Negative parallelism:** "not just X, but Y", "not only ... but also".
   - **Copula avoidance:** "serves as", "stands as", "boasts".
   - **Filler openers:** "In today's fast-paced world", "It is important to note", "In the rapidly evolving landscape".
   - **Repeated rule of three:** Avoid sentences built on three parallel items when repeated in the same piece.

4. **Hedging Density:**
   - Modal and qualifying words (*may, might, could, generally, typically*) must not exceed 2.0 per 100 words.

5. **Links:**
   - Only include URLs that were retrieved during the current execution run from verified sources. Never fabricate or guess URLs.

## Approved Company Information
- **Core Positioning:** "SanadSquad covers the whole attack surface, not just the contract."
- **Four Service Lines:**
  1. **Web3 Security:** Smart contract audits across EVM and Solana / SVM, protocol design review, formal invariant reasoning.
  2. **Web2 Pentesting:** Infrastructure around the protocol (APIs, cloud, CI/CD, dependencies, internal access, frontend).
  3. **Agentic Security & Red Teaming:** AI agent systems, prompt injection, tool abuse, unauthorized transactions, agent wallet spending policy.
  4. **Agentic Payment Protocols:** x402 and MPP integration review, facilitator trust, payment policy design.
- **Audits & Findings:** Refer to specific findings from the SanadSquad Knowledge Base (`KB.json`).
