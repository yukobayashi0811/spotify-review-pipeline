from __future__ import annotations

import time
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any

from .classify import ControlledInterrupt, ModelConfig, run_enrichment
from .group import run_grouping
from .io_utils import write_json
from .prepare import ingest
from .providers import Provider
from .rank import rerank
from .recommend import run_recommendation
from .storage import Store
from .verify import run_verification


STAGES = ("prepare", "classify", "verify", "group", "rank", "recommend")


def run_pipeline(
    *,
    input_csv: str | Path,
    state_path: str | Path,
    artifacts_dir: str | Path,
    provider: Provider,
    config: ModelConfig,
    phase: str = "initial",
    stop_after: str = "recommend",
    interrupt_after_batches: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if stop_after not in STAGES:
        raise ValueError(f"stop_after must be one of {STAGES}")
    run_id = run_id or f"run-{uuid.uuid4()}"
    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    prompt_root = files("spotify_pipeline").joinpath("prompt_text")
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "phase": phase,
        "stages": {},
        "stage_wall_seconds": {},
    }
    stage_started = time.perf_counter()
    summary["stages"]["prepare"] = ingest(
        input_csv, state_path, artifacts / "ingestion_report.json"
    )
    summary["stage_wall_seconds"]["prepare"] = round(time.perf_counter() - stage_started, 6)
    if stop_after == "prepare":
        return _finish(summary, started, artifacts)
    try:
        stage_started = time.perf_counter()
        summary["stages"]["classify"] = run_enrichment(
            state_path=state_path,
            provider=provider,
            prompt_path=str(prompt_root.joinpath("classify.md")),
            config=config,
            run_id=run_id,
            phase=phase,
            interrupt_after_batches=interrupt_after_batches,
        )
        summary["stage_wall_seconds"]["classify"] = round(
            time.perf_counter() - stage_started, 6
        )
    except ControlledInterrupt:
        with Store(state_path) as store:
            checkpoint = {"completed_ids": store.completed_ids()}
            store.set_metadata("checkpoint_before", checkpoint)
        write_json(artifacts / "checkpoint_before.json", checkpoint)
        summary["status"] = "intentionally_interrupted"
        _finish(summary, started, artifacts)
        raise
    if phase == "resume":
        with Store(state_path) as store:
            checkpoint = {"completed_ids": store.completed_ids()}
            store.set_metadata("checkpoint_after", checkpoint)
        write_json(artifacts / "checkpoint_after.json", checkpoint)
    if stop_after == "classify":
        return _finish(summary, started, artifacts)
    stage_started = time.perf_counter()
    summary["stages"]["verify"] = run_verification(
        state_path=state_path,
        provider=provider,
        prompt_path=str(prompt_root.joinpath("verify.md")),
        config=config,
        run_id=run_id,
        output_path=artifacts / "verification_report.json",
    )
    summary["stage_wall_seconds"]["verify"] = round(time.perf_counter() - stage_started, 6)
    if stop_after == "verify":
        return _finish(summary, started, artifacts)
    stage_started = time.perf_counter()
    summary["stages"]["group"] = run_grouping(
        state_path=state_path,
        provider=provider,
        prompt_path=str(prompt_root.joinpath("group.md")),
        config=config,
        run_id=run_id,
        artifacts_dir=artifacts,
    )
    summary["stage_wall_seconds"]["group"] = round(time.perf_counter() - stage_started, 6)
    if stop_after == "group":
        return _finish(summary, started, artifacts)
    stage_started = time.perf_counter()
    summary["stages"]["rank"] = {
        "ranking": rerank(
            state_path=state_path,
            output_csv=artifacts / "ranking.csv",
            summary_json=artifacts / "ranking.json",
        )
    }
    summary["stage_wall_seconds"]["rank"] = round(time.perf_counter() - stage_started, 6)
    if stop_after == "rank":
        return _finish(summary, started, artifacts)
    stage_started = time.perf_counter()
    summary["stages"]["recommend"] = run_recommendation(
        state_path=state_path,
        provider=provider,
        prompt_path=str(prompt_root.joinpath("memo.md")),
        config=config,
        run_id=run_id,
        artifacts_dir=artifacts,
    )
    summary["stage_wall_seconds"]["recommend"] = round(
        time.perf_counter() - stage_started, 6
    )
    return _finish(summary, started, artifacts)


def _finish(summary: dict[str, Any], started: float, artifacts: Path) -> dict[str, Any]:
    summary.setdefault("status", "completed")
    summary["wall_seconds"] = round(time.perf_counter() - started, 6)
    write_json(artifacts / "run_summary.json", summary)
    return summary
