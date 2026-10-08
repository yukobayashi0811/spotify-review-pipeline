from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from spotify_pipeline.classify import ModelConfig, actual_cost
from spotify_pipeline.distilled import build_training_rows, normalize_text
from spotify_pipeline.hybrid import (
    RoutingPolicy,
    _collect_local_diagnostics,
    _routing_priority,
)
from spotify_pipeline.providers import ProviderResult


SOURCE_FIELDS = (
    "review_id",
    "review_text",
    "review_rating",
    "review_likes",
    "app_version",
    "review_timestamp",
)


class DistilledPipelineTests(unittest.TestCase):
    def test_normalized_holdout_leakage_and_conflicts_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.csv"
            labels = root / "labels.csv"
            excluded = root / "golden_input.csv"
            self._write_source(
                source,
                [
                    ("r1", "  GREAT\u3000APP "),
                    ("r2", "Safe playback issue"),
                    ("r3", "same normalized"),
                    ("r4", "SAME   NORMALIZED"),
                ],
            )
            self._write_source(excluded, [("g1", "great app")])
            with labels.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "review_id",
                        "topic",
                        "intent",
                        "sentiment",
                        "severity",
                        "needs_review",
                    ),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(
                    [
                        dict(review_id="r1", topic="other", intent="praise", sentiment="1", severity="1", needs_review="false"),
                        dict(review_id="r2", topic="playback", intent="complaint", sentiment="-1", severity="3", needs_review="false"),
                        dict(review_id="r3", topic="other", intent="praise", sentiment="1", severity="1", needs_review="false"),
                        dict(review_id="r4", topic="usability", intent="complaint", sentiment="-1", severity="2", needs_review="true"),
                    ]
                )
            rows, report = build_training_rows(
                source_path=source,
                labels_path=labels,
                excluded_source_paths=(excluded,),
            )
            self.assertEqual([row["review_id"] for row in rows], ["r2"])
            self.assertEqual(report["excluded_by_normalized_text_after_exact_exclusion"], 1)
            self.assertEqual(report["conflicting_normalized_label_groups_removed"], 1)
            self.assertEqual(normalize_text(" A\u3000 B "), "a b")

    def test_local_billing_mode_has_zero_api_cost(self) -> None:
        result = ProviderResult(
            request_id="local",
            payload={"records": []},
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            duration_seconds=0.01,
            provider="local-distilled-sklearn",
            billing_mode="local",
            usage_status="not_applicable",
        )
        self.assertEqual(actual_cost(result, ModelConfig()), 0.0)

    def test_routing_fraction_is_bounded(self) -> None:
        RoutingPolicy(max_escalation_fraction=0.20).validate()
        with self.assertRaises(ValueError):
            RoutingPolicy(max_escalation_fraction=1.01).validate()

    def test_uncertainty_routing_prioritizes_small_mean_margin(self) -> None:
        policy = RoutingPolicy(routing_mode="uncertainty", uncertainty_metric="mean_margin")
        certain = {
            "route_reasons": ["high_severity"],
            "local_severity": 5,
            "minimum_confidence": 0.8,
            "mean_margin": 0.7,
            "text_sha256": "b",
        }
        uncertain = {
            "route_reasons": ["uncertainty_priority"],
            "local_severity": 1,
            "minimum_confidence": 0.4,
            "mean_margin": 0.1,
            "text_sha256": "a",
        }
        self.assertLess(
            _routing_priority(uncertain, policy),
            _routing_priority(certain, policy),
        )

    def test_routing_policy_rejects_unknown_mode(self) -> None:
        with self.assertRaises(ValueError):
            RoutingPolicy(routing_mode="golden_tuned").validate()

    def test_saved_compact_diagnostics_map_back_to_source_ids(self) -> None:
        calls = [
            {
                "review_ids": ["source-a", "source-b"],
                "provider_metadata": {
                    "review_diagnostics": {
                        "R002": {"mean_margin": 0.8},
                        "R001": {"mean_margin": 0.2},
                    }
                },
            }
        ]
        mapped = _collect_local_diagnostics(object(), calls)
        self.assertEqual(mapped["source-a"]["mean_margin"], 0.2)
        self.assertEqual(mapped["source-b"]["mean_margin"], 0.8)

    @staticmethod
    def _write_source(path: Path, values: list[tuple[str, str]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(SOURCE_FIELDS)
            for review_id, text in values:
                writer.writerow((review_id, text, "5", "0", "1", "2023-01-01 00:00:00"))


if __name__ == "__main__":
    unittest.main()
