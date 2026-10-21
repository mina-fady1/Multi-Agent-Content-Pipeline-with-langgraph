import argparse
import sys
from pathlib import Path

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from agents.orchestrator import Orchestrator
from config.settings import settings

def parse_set_args(set_args):
    """Parses key=value arguments from --set flag."""
    overrides = {}
    if not set_args:
        return overrides
    for item in set_args:
        if "=" in item:
            k, v = item.split("=", 1)
            overrides[k.strip()] = v.strip().strip('"').strip("'")
        else:
            print(f"[WARN] Ignoring malformed --set argument: {item}. Expected format: slot_id=\"value\"")
    return overrides

def main():
    parser = argparse.ArgumentParser(
        description="Run SanadSquad Multi-Agent Weekly Content Pipeline."
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=str(settings.DEFAULT_PLAN_FILE),
        help="Path to weekly plan JSON file (default: plan.json)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output/week-42",
        help="Target output directory for artifacts (default: output/week-42)",
    )
    parser.add_argument(
        "--set",
        dest="set_overrides",
        action="append",
        help="Override slot focus, e.g. --set thread_2=\"oracle manipulation\"",
    )
    parser.add_argument(
        "--provider",
        type=str,
        choices=["gemini", "ollama"],
        default=None,
        help="LLM provider: 'gemini' or 'ollama' (default auto-detects from environment)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name (e.g., 'gemini-2.5-flash' or 'qwen3.5:4b-q4_K_M')",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help="Run slots sequentially instead of parallel",
    )

    parser.add_argument("--demo-research", action="store_true",
                        help="Use labelled research fixtures; export previews only, never publication posts")
    args = parser.parse_args()

    plan_path = Path(args.plan)
    output_path = Path(args.output)
    set_overrides = parse_set_args(args.set_overrides)

    if not plan_path.exists():
        print(f"[ERROR] Plan file '{plan_path}' does not exist.")
        sys.exit(1)

    orchestrator = Orchestrator(provider=args.provider, model_name=args.model, demo_mode=args.demo_research)
    try:
        summary = orchestrator.run_week(
            plan_path=plan_path, output_dir=output_path, set_overrides=set_overrides,
            parallel=not args.no_parallel,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Pipeline failed: {exc}\n")
    return 1 if summary.pieces_dropped else 0

if __name__ == "__main__":
    sys.exit(main())
