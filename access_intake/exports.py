"""Publish a coherent database snapshot as one immutable export generation."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .presentation import for_output
from .store import Store


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class PinnedExport:
    generation: Path

    def read(self) -> dict[str, Any]:
        # Never resolve the publication symlink again between these two reads.
        queue = json.loads((self.generation / "queue.json").read_text(encoding="utf-8"))
        text = (self.generation / "extractions.jsonl").read_text(encoding="utf-8")
        return {
            "generation": str(self.generation), "queue": queue,
            "extractions": [json.loads(line) for line in text.splitlines()],
        }


def pin_export(destination: str | Path) -> PinnedExport:
    destination = Path(destination)
    if not destination.is_symlink():
        raise ValueError("Export destination must be a published generation symlink")
    generation = destination.resolve(strict=True)
    if not generation.is_dir():
        raise ValueError("Published generation must be a directory")
    return PinnedExport(generation)


def export_snapshot(
    store: Store, destination: str | Path, *,
    before_publish: Callable[[Path], None] | None = None,
    after_publish: Callable[[Path], None] | None = None,
) -> Path:
    snapshot = for_output(store.snapshot(), store.path)
    if any(row["processing_state"] not in ("succeeded", "failed") for row in snapshot["extractions"]):
        raise ValueError("Unfinished work must complete before final export")

    destination = Path(destination).absolute()
    # Resolve the parent only: resolving destination would follow the old link.
    destination = destination.parent.resolve() / destination.name
    if destination.exists() and not destination.is_symlink():
        raise FileExistsError("Export destination exists and is not a publication symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    generations = destination.parent / f".{destination.name}-generations"
    generations.mkdir(exist_ok=True)
    identity = uuid4().hex
    staging = generations / f".staging-{identity}"
    staging.mkdir()
    _write(staging / "extractions.jsonl", "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in snapshot["extractions"]
    ))
    _write(staging / "queue.json", json.dumps(snapshot["queue"], ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    _sync_directory(staging)
    generation = generations / identity
    staging.rename(generation)
    _sync_directory(generations)
    # The new generation is complete and durable before it can become current.
    if before_publish is not None:
        before_publish(generation)
    temporary_link = destination.parent / f".{destination.name}-publish-{identity}"
    temporary_link.symlink_to(os.path.relpath(generation, destination.parent), target_is_directory=True)
    _sync_directory(destination.parent)
    os.replace(temporary_link, destination)
    _sync_directory(destination.parent)
    if after_publish is not None:
        after_publish(generation)
    return generation
