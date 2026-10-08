from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .classify import ModelConfig
from .io_utils import file_sha


LEGACY_MODEL = "gpt-6-luna"
LEGACY_EFFORT = "low"
LEGACY_PROMPT_VERSION = "2026-09-30-v2"
LEGACY_SCHEMA_VERSION = "enrichment-v1"
LEGACY_BATCH_SIZE = 50
LEGACY_CLI_VERSION = "codex-cli 0.159.3"
LEGACY_EXECUTION_FINGERPRINT = (
    "codex-cli-0.159.3:subscription:ephemeral:empty-read-only:"
    "tools-disabled:provider-output-cap-unavailable"
)
LEGACY_LABEL_CONFIG = (
    "gpt-6-luna:low:classify-2026-09-30-v2:enrichment-v1:"
    + LEGACY_EXECUTION_FINGERPRINT
)
LEGACY_PROMPT_SHA256 = "e417f005c44df4a1693b4470a8646f658a5d115db3ec9cedc20325bdb413def4"


@dataclass(frozen=True)
class LegacyCompatibilityReport:
    compatible: bool
    expected_label_config: str
    observed_label_configs: tuple[str, ...]
    prompt_sha256: str
    prompt_matches: bool
    successful_enrich_calls: int
    maximum_successful_batch: int
    batch_matches: bool
    cli_version: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def legacy_luna_config() -> ModelConfig:
    """Return the exact immutable configuration used by the saved 10k run."""

    config = ModelConfig(
        model=LEGACY_MODEL,
        effort=LEGACY_EFFORT,
        batch_size=LEGACY_BATCH_SIZE,
        max_workers=3,
        max_attempts=2,
        max_output_tokens=4_000,
        timeout_seconds=300.0,
        spend_limit_usd=25.0,
        fallback_model=None,
        fallback_fraction=0.0,
        input_rate_per_million=0.0,
        cached_input_rate_per_million=0.0,
        output_rate_per_million=0.0,
        execution_fingerprint=LEGACY_EXECUTION_FINGERPRINT,
        prompt_version=LEGACY_PROMPT_VERSION,
        schema_version=LEGACY_SCHEMA_VERSION,
    )
    if config.label_config != LEGACY_LABEL_CONFIG:
        raise AssertionError("Legacy Luna label_config construction drifted")
    return config


def check_legacy_compatibility(
    *, legacy_state_path: str | Path, prompt_path: str | Path, cli_version: str
) -> LegacyCompatibilityReport:
    """Fail closed unless saved evidence and the executable inputs match exactly."""

    state = Path(legacy_state_path)
    prompt = Path(prompt_path)
    reasons: list[str] = []
    observed: tuple[str, ...] = ()
    successful_calls = 0
    maximum_batch = 0
    if not state.is_file():
        reasons.append(f"legacy state DB not found: {state}")
    else:
        with sqlite3.connect(state) as connection:
            rows = connection.execute(
                "SELECT label_config,review_ids_json FROM calls "
                "WHERE role='enrich' AND outcome='succeeded'"
            ).fetchall()
        successful_calls = len(rows)
        observed = tuple(sorted({str(row[0]) for row in rows}))
        sizes = [len(json.loads(row[1])) for row in rows]
        maximum_batch = max(sizes, default=0)
        if observed != (LEGACY_LABEL_CONFIG,):
            reasons.append("saved successful enrich calls do not have one exact legacy label_config")
        if not rows:
            reasons.append("legacy DB contains no successful enrichment calls")
        if any(size < 1 or size > LEGACY_BATCH_SIZE for size in sizes):
            reasons.append("saved enrichment call cardinality is outside 1..50")
        if sizes and not any(size == LEGACY_BATCH_SIZE for size in sizes):
            reasons.append("saved run contains no full 50-text batch")
    prompt_sha = file_sha(prompt) if prompt.is_file() else "missing"
    if prompt_sha != LEGACY_PROMPT_SHA256:
        reasons.append("classification prompt body SHA-256 does not match the frozen legacy body")
    if cli_version != LEGACY_CLI_VERSION:
        reasons.append(
            f"Codex CLI version mismatch: expected {LEGACY_CLI_VERSION}, found {cli_version}"
        )
    config = legacy_luna_config()
    if config.label_config != LEGACY_LABEL_CONFIG:
        reasons.append("current code cannot reproduce the exact legacy label_config")
    return LegacyCompatibilityReport(
        compatible=not reasons,
        expected_label_config=LEGACY_LABEL_CONFIG,
        observed_label_configs=observed,
        prompt_sha256=prompt_sha,
        prompt_matches=prompt_sha == LEGACY_PROMPT_SHA256,
        successful_enrich_calls=successful_calls,
        maximum_successful_batch=maximum_batch,
        batch_matches=bool(successful_calls) and maximum_batch == LEGACY_BATCH_SIZE,
        cli_version=cli_version,
        reasons=tuple(reasons),
    )


def require_legacy_compatibility(**kwargs: object) -> LegacyCompatibilityReport:
    report = check_legacy_compatibility(**kwargs)  # type: ignore[arg-type]
    if not report.compatible:
        raise RuntimeError(
            "Legacy Luna compatibility gate failed; no Luna call is permitted: "
            + "; ".join(report.reasons)
        )
    return report

