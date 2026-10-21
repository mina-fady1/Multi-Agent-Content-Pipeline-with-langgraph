"""Resolve generated artifacts without following paths outside the output root."""
from pathlib import Path


def output_path(root: Path, *parts: str) -> Path:
    base = Path(root).resolve()
    target = base.joinpath(*parts).resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"Artifact destination must remain inside {base}")
    return target
