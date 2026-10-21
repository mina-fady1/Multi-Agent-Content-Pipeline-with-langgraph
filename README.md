# Multi-Agent Content Pipeline

Content-generation system for **SanadSquad** built with **LangGraph**, supporting cloud models via **Google Gemini API**, web research via **Tavily API**, and local execution via **Ollama** (`qwen3.5:4b-q4_K_M`).

---

## Overview

SanadSquad is a full-surface security firm covering:
1. **Web3 Security** (EVM & Solana / SVM contract audits, formal invariant reasoning)
2. **Web2 Pentesting** (APIs, cloud, CI/CD, dependencies, frontends)
3. **Agentic Security & Red Teaming** (Prompt injection, tool abuse, agent wallets & spending policies)
4. **Agentic Payment Protocols** (x402 & MPP integration, facilitator trust)

This system produces one week of verified, grounded content for X (Twitter) by orchestrating specialized agents:
- **Orchestrator**: Reads the weekly plan, executes slots in parallel with fault isolation, handles retries, tracks tokens & costs, and exports full audit artifacts.
- **Research Agent**: Fills empty slots from the Knowledge Base (`KB.json`) or domain-restricted web research (`sources.json`). Enforces strict source separation in code.
- **Tech Writer Agent**: Writes 5 to 10 tweet technical threads grounded in vulnerability findings or technical post-mortems.
- **Marketing Writer Agent**: Writes concise, high-impact standalone marketing posts (<= 280 chars) grounded in web research.
- **Code Quality Gate**: Hard deterministic code checks (banned characters, AI vocabulary, syntactic patterns, hedging density, length limits, unretrieved links) executing before model review.
- **Reviewer Agent**: Evaluates drafted content against a written rubric (`rubric.md`), gating publication with an automated 1-retry feedback loop.
- **Visual Agent**: Generates visual specifications and renders branded 1200x675 X cards (Pillow) exclusively for accepted pieces.

---

## Setup & Installation

### 1. Prerequisites
- Python 3.10+ (Tested on Python 3.12)
- [Ollama](https://ollama.com/) (for local model execution)

### 2. Virtual Environment Setup
```powershell
# Clone or navigate to the repository
cd "Multi-Agent Content Pipeline with langgraph"

# Create Python virtual environment
python -m venv .venv

# Activate virtual environment
# On Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# On Linux / macOS:
# source .venv/bin/activate

# Install required dependencies
python -m pip install -r requirements-lock.txt
```

### 3. API Keys & Configuration
Copy `.env.example` to `.env`:
```powershell
cp .env.example .env
```
Edit `.env` to configure your keys:
```env
# Google Gemini API Key (optional if using local Ollama)
GEMINI_API_KEY=your_gemini_api_key_here

# Tavily Search API Key (required for live web research)
TAVILY_API_KEY=your_tavily_api_key_here

# Default LLM Provider: 'gemini' or 'ollama'
LLM_PROVIDER=gemini

# Model Configuration
GEMINI_MODEL=gemini-2.5-flash
OLLAMA_MODEL=qwen3.5:4b-q4_K_M
OLLAMA_BASE_URL=http://localhost:11434
```

> **Research modes:** Live searches fail honestly when credentials are missing, a provider fails, or no permitted evidence is returned. Synthetic fixtures are available only with `--demo-research`. Demo runs produce labelled `demo_content.json` previews and images; `final_content.json`, `posts.json`, and `posts.csv` contain no publishable demo pieces. The demo flag does not mock the language model; Gemini or Ollama must still be available.

### 4. Ollama Local Model
Ensure Ollama is running and your local model is pulled:
```powershell
ollama pull qwen3.5:4b-q4_K_M
```

---

## CLI Usage

### 1. Run Full Weekly Pipeline
```powershell
# Run default weekly plan (output to output/week-42)
python run_week.py --plan plan.json --output output/week-42

# Run with topic override on thread_2
python run_week.py --plan plan.json --set thread_2="oracle manipulation"

# Force execution using local Ollama (qwen3.5:4b-q4_K_M)
python run_week.py --plan plan.json --provider ollama

# Force execution using Gemini API
python run_week.py --plan plan.json --provider gemini --model gemini-2.5-flash
```

### 2. Run Single Piece
```powershell
# Run a single technical thread about reentrancy
python run_piece.py --type tech-thread --topic "reentrancy"

# Run a marketing post about prompt injection
python run_piece.py --type marketing-post --topic "prompt injection" --service agentic-security

# Run single piece with Ollama
python run_piece.py --type tech-thread --topic "SS-03699" --provider ollama
```

---

## Output Artifacts & Audit Trail

Each run writes to its designated output directory (e.g., `output/week-42/`):
- `resolved_plan.json`: The fully resolved weekly schedule with slot IDs, assigned topics, and source references.
- `research_log.json`: Every search batch, the actual submitted domains, live/demo provenance, retrieved or fixture URLs, and empty/error outcomes.
- `drafts.json`: Every attempt, including the raw response, processed draft, retry feedback, code-quality checks, and review verdict.
- `review_report.json`: Final status plus complete attempt history, code-gate metrics, review feedback, and execution errors.
- `final_content.json`: Final accepted pieces with image specifications.
- `posts.csv`: Standard export ready for social media scheduling tools with columns `Post Text` and `Image`.
- `run_summary.json`: Shipped/dropped/demo counts, provider/model, per-model token usage, pricing assumptions, and duration. Unknown model pricing is `null`, not a guessed dollar value.
- `artifact_manifest.json`: Run status and managed per-post/image paths. Only a `complete` run is ready for consumption.
- `demo_content.json`: Accepted previews from explicit demo runs; these are excluded from publication exports.
- `images/`: High-resolution 1200x675 branded graphic cards for every accepted piece.

---

## Architecture & Code Quality Gates

See [ARCHITECTURE.md](ARCHITECTURE.md) for full architectural documentation, Mermaid diagrams, trade-off explanations, and scaling guidelines.


## Validation and reruns

Run the offline regression suite with the project's Python environment:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

Tests mock model/search calls and write to temporary directories. The lock file pins the reviewed Python 3.12 Windows environment; `requirements.txt` pins the direct runtime dependencies.

Both CLIs honor `--provider` and `--model`. Explicit Gemini selection requires a Gemini key; it never silently switches the requested model to Ollama. Use `--demo-research --output output/demo` for fixture-based previews.

Plans reject invalid service names, duplicate IDs (including case-only duplicates), unsafe filenames, and unknown override IDs. Explicit unmatched topics fail rather than silently becoming another vulnerability. Each run gets separate research, topic-allocation, and token state.

Rerunning the same output directory replaces current exports and removes obsolete generated posts/images while preserving unrelated files. JSON/CSV writes replace files atomically. A failed or incomplete export is identified in `artifact_manifest.json`; consumers must check its status instead of scanning an old `posts` directory. `.pipeline.lock` prevents simultaneous writers. If a process is killed, check that the recorded process is no longer running before removing its abandoned lock file and rerunning.

The CLIs exit nonzero when any piece is dropped or the run fails. Oversized drafts receive the existing one-revision opportunity; text is never silently cut to fit. Invalid reviewer output is rejected. Semantic claims and editorial quality still depend on the evidence and the model review; the offline suite verifies code behavior, not every possible generated statement.
