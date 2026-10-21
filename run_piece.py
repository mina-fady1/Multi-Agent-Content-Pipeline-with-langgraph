import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console
from rich.panel import Panel

from config.settings import settings
from models.schemas import Slot
from agents.orchestrator import Orchestrator

console = Console()

def main():
    parser = argparse.ArgumentParser(
        description="Run a single piece through the SanadSquad Content Pipeline."
    )
    parser.add_argument(
        "--type",
        type=str,
        required=True,
        choices=["tech-thread", "marketing-post"],
        help="Post type: 'tech-thread' or 'marketing-post'",
    )
    parser.add_argument(
        "--topic",
        type=str,
        required=True,
        help="Focus topic or vulnerability (e.g., 'reentrancy' or 'SS-03699')",
    )
    parser.add_argument(
        "--service",
        type=str,
        default="web3-security",
        choices=["web3-security", "pentesting", "agentic-security", "agentic-payments"],
        help="Service line (default: web3-security)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Target output directory (default: output/piece_<timestamp>)",
    )
    parser.add_argument(
        "--provider",
        type=str,
        choices=["gemini", "ollama"],
        default=None,
        help="LLM provider: 'gemini' or 'ollama'",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name",
    )

    parser.add_argument("--demo-research", action="store_true",
                        help="Use labelled research fixtures; export previews only, never publication posts")
    args = parser.parse_args()

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output) if args.output else Path(f"output/piece_{timestamp_str}")

    slot = Slot(
        id="single_piece",
        type=args.type,
        service=args.service,
        focus=args.topic,
    )

    console.print(Panel(
        f"[bold green]Running Single Piece[/bold green]\n"
        f"Type: {args.type}\n"
        f"Topic: {args.topic}\n"
        f"Provider: {args.provider or settings.DEFAULT_PROVIDER}",
        expand=False,
    ))

    orchestrator = Orchestrator(provider=args.provider, model_name=args.model, demo_mode=args.demo_research)
    try:
        result_slot, summary = orchestrator.run_piece(slot, output_dir)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Pipeline failed: {exc}\n")

    if result_slot.status == "accepted":
        console.print(Panel(
            f"[bold green]Final Accepted Content:[/bold green]\n\n{result_slot.draft_content}\n\n"
            f"[dim]Image Asset: {result_slot.image_path}[/dim]",
            title="Demo Preview" if args.demo_research else "Delivered Piece",
            border_style="green",
        ))
    else:
        console.print(Panel(
            f"[bold red]Piece Dropped:[/bold red]\n{result_slot.rejection_reason}",
            title="Rejection Report",
            border_style="red",
        ))

    return 1 if summary.pieces_dropped else 0

if __name__ == "__main__":
    sys.exit(main())
