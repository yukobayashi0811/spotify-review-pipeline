"""End-to-end test of ``spotify-pipeline run`` with a synthetic stand-in for Luna.

The stand-in (``FakeLuna``) is a deterministic fixture, never assignment
evidence. It exercises every stage, the guard's caps and the warm-run cache.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import numpy as np

from spotify_pipeline import group as group_module
from spotify_pipeline.guard import (
    CallCapReachedError, GuardedProvider, ModelNotAllowedError, SpendCapReachedError,
)
from spotify_pipeline.io_utils import write_csv, write_json
from spotify_pipeline.pipeline_run import run_pipeline
from spotify_pipeline.providers import ProviderResult, ProviderUnavailableError

ROOT = Path(__file__).resolve().parents[1]


class FakeLuna:
    name = "fake-luna-fixture"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, *, role, model, effort, developer_prompt, user_payload, schema,
             max_output_tokens, timeout_seconds) -> ProviderResult:
        self.calls.append(role)
        if role in {"enrich", "verify"}:
            records = []
            for item in user_payload["reviews"]:
                text = item["review_text"]
                lower = text.lower()
                negative = any(word in lower for word in ("not", "can't", "cannot", "bad", "worst", "ads", "fix"))
                records.append({
                    "review_id": item["review_id"],
                    "topic": "usability" if "ads" in lower else ("playback" if "play" in lower else "other"),
                    "intent": "complaint" if negative else "praise",
                    "sentiment": -0.5 if negative else 0.5,
                    "severity": 3 if negative else 1,
                    "entities": [],
                    "evidence_quote": text[:60] if text.strip() else text,
                    "needs_review": False,
                })
            payload = {"records": records}
        elif role == "group":
            payload = {"issues": [
                {
                    "issue_id": issue["issue_id"],
                    "issue_name": f"{issue['topic']} issue {issue['issue_id'][-2:]}",
                    "description": f"Reviews about {issue['topic']}.",
                    "coherent": "yes",
                    "actionable": "yes" if issue["topic"] != "other" else "no",
                    "heterogeneous_review_ids": [],
                }
                for issue in user_payload["issues"]
            ]}
        else:
            issues = user_payload["issues"]
            actionable = user_payload["actionable_ranking"]
            allowed = [row for row in actionable if row["topic"] in
                       {"access", "usability", "playback", "billing", "support"}]
            top = allowed[0]
            others = [row["issue_id"] for row in actionable if row["issue_id"] != top["issue_id"]]
            coverage = user_payload["coverage"]
            payload = {
                "priority_topic": top["topic"],
                "priority_issue_id": top["issue_id"],
                "priority": f"Prioritize {top['issue_id']}.",
                "rationale": f"{coverage['classified_rows']} of {coverage['source_rows']} rows classified.",
                "alternative_issue_ids": others[:2],
                "alternatives": [f"{value} ranks lower." for value in others[:2]],
                "baseline_top_handling": "Generic issues are not selected.",
                "sensitivity_interpretation": "Not estimated for this input.",
                "limitations": [
                    "Self-selected reviews.", "No revenue or churn data.",
                    "Cancellation is intent.",
                    f"{coverage['quarantined_rows']} rows quarantined.",
                ],
                "claims": [],
            }
            assert issues  # the pack carries issue metadata
        return ProviderResult(
            request_id=f"fake-{uuid.uuid4()}", payload=payload, input_tokens=1000,
            cached_input_tokens=200, output_tokens=100, duration_seconds=0.01,
            provider=self.name, billing_mode="synthetic", usage_status="measured",
            provider_metadata={},
        )


def fake_embeddings(rows, artifacts: Path):
    vectors = []
    for row in rows:
        vector = np.zeros(32, dtype=np.float32)
        for word in str(row["embedding_text"]).lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 32] += 1
        vector += 1e-3
        vectors.append(vector / np.linalg.norm(vector))
    array = np.vstack(vectors).astype(np.float32)
    np.save(artifacts / "group_embeddings.npy", array, allow_pickle=False)
    write_csv(artifacts / "group_embedding_map.csv", ["embedding_index", "text_sha256"],
              [{"embedding_index": i, "text_sha256": row["text_sha256"]} for i, row in enumerate(rows)])
    return array, {"model": "fake-hash-embedding", "rows": len(rows)}


class PipelineRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.config = ROOT / "config" / "pipeline.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _input(self) -> Path:
        source = ROOT / "data" / "cost_100.csv"
        target = self.dir / "input.csv"
        target.write_bytes(source.read_bytes())
        return target

    def test_cold_then_warm_run_on_100_reviews(self) -> None:
        fake = FakeLuna()
        with mock.patch.object(group_module, "_load_or_create_embeddings", fake_embeddings):
            cold = run_pipeline(self._input(), self.config, output_dir=self.dir / "out",
                                luna_provider_factory=lambda _root: fake)
            calls_after_cold = len(fake.calls)
            warm = run_pipeline(self._input(), self.config, output_dir=self.dir / "out",
                                luna_provider_factory=lambda _root: fake)
        self.assertEqual(cold["status"], "completed")
        self.assertEqual(cold["counts"]["source_rows"], 100)
        self.assertEqual(cold["counts"]["completed"] + cold["counts"]["quarantined"], 100)
        routed = cold["stages"]["route"]["routed_texts"]
        self.assertLessEqual(routed, cold["declared_controls"]["route_limit_texts"])
        roles = cold["new_model_calls_this_run"]["luna_by_role"]
        self.assertEqual(roles["memo"], 1)
        self.assertGreaterEqual(roles["verify"], 1)
        self.assertGreaterEqual(roles["group"], 1)
        self.assertEqual(roles["enrich"], 1 if routed else 0)
        # Warm run: every stage skipped, no new model call of any kind.
        self.assertTrue(warm["all_stages_skipped_unchanged"])
        self.assertEqual(len(fake.calls), calls_after_cold)
        self.assertEqual(warm["new_model_calls_this_run"]["luna_attempts"], 0)
        self.assertEqual(warm["new_model_calls_this_run"]["local_classifier_batches"], 0)
        out = self.dir / "out"
        for name in ("run.json", "ingestion.json", "records.jsonl", "membership.csv",
                     "ranking.csv", "claims.csv", "calls.jsonl", "checkpoint_before.json",
                     "checkpoint_after.json"):
            self.assertTrue((out / "grading" / name).is_file(), name)
        memo = json.loads((out / "memo_payload.json").read_text())
        self.assertGreaterEqual(len(memo["claims"]), 12)
        pack = json.loads((out / "memo_evidence_pack.json").read_text())
        self.assertIn("coverage", pack)
        self.assertIn("monthly_trend", pack)
        selections = {item["selection"] for items in pack["evidence"].values() for item in items}
        self.assertEqual(selections, {"modal_severity_closest_to_centroid"})
        manifest = json.loads((out / "run_manifest.json").read_text())
        self.assertEqual(manifest["luna"]["model"], "gpt-6-luna")


class GuardTests(unittest.TestCase):
    def _result(self, **metadata):
        return ProviderResult(
            request_id="r", payload={}, input_tokens=10, cached_input_tokens=0,
            output_tokens=10, duration_seconds=0.0, provider="p", billing_mode="x",
            usage_status="measured", provider_metadata=metadata,
        )

    def test_allowlist_rejects_other_models(self) -> None:
        inner = mock.Mock(name="provider")
        inner.name = "p"
        guard = GuardedProvider(inner, allowlist={"gpt-6-luna": ("low",)})
        with self.assertRaises(ModelNotAllowedError):
            guard.call(role="enrich", model="gpt-6-sol", effort="low")
        with self.assertRaises(ModelNotAllowedError):
            guard.call(role="enrich", model="gpt-6-luna", effort="high")
        inner.call.assert_not_called()

    def test_reported_served_model_must_match(self) -> None:
        inner = mock.Mock()
        inner.name = "p"
        inner.call.return_value = self._result(served_model="gpt-6-sol")
        guard = GuardedProvider(inner, allowlist={"gpt-6-luna": ("low",)})
        with self.assertRaises(ModelNotAllowedError):
            guard.call(role="memo", model="gpt-6-luna", effort="low")

    def test_transient_errors_back_off_then_succeed(self) -> None:
        inner = mock.Mock()
        inner.name = "p"
        inner.call.side_effect = [
            ProviderUnavailableError("rate limited", request_id="a", duration_seconds=0.1),
            self._result(),
        ]
        sleeps: list[float] = []
        logged: list[dict] = []
        guard = GuardedProvider(inner, sleep=sleeps.append, on_failed_attempt=logged.append)
        guard.call(role="verify", model="gpt-6-luna", effort="low")
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(1.0 <= sleeps[0] <= 2.0)
        self.assertEqual(len(logged), 1)

    def test_transient_errors_are_bounded(self) -> None:
        inner = mock.Mock()
        inner.name = "p"
        inner.call.side_effect = ProviderUnavailableError("down", request_id="a", duration_seconds=0)
        guard = GuardedProvider(inner, max_transient_retries=2, sleep=lambda _s: None)
        with self.assertRaises(ProviderUnavailableError):
            guard.call(role="verify", model="gpt-6-luna", effort="low")
        self.assertEqual(inner.call.call_count, 3)

    def test_call_cap_and_spend_cap(self) -> None:
        inner = mock.Mock()
        inner.name = "p"
        inner.call.return_value = self._result()
        guard = GuardedProvider(inner, max_calls_by_role={"memo": 2})
        guard.call(role="memo", model="gpt-6-luna", effort="low")
        guard.call(role="memo", model="gpt-6-luna", effort="low")
        with self.assertRaises(CallCapReachedError):
            guard.call(role="memo", model="gpt-6-luna", effort="low")
        tight = GuardedProvider(inner, spend_limit_usd=0.005)
        with self.assertRaises(SpendCapReachedError):
            tight.call(role="enrich", model="gpt-6-luna", effort="low")


if __name__ == "__main__":
    unittest.main()


class InterruptResumeTests(unittest.TestCase):
    """Controlled interruption during Luna relabeling, then resume (fixture Luna)."""

    def test_interrupt_then_resume_meets_contract_rules(self) -> None:
        from spotify_pipeline.classify import ControlledInterrupt

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out"
            source = ROOT / "data" / "analysis_10000.csv"
            fake = FakeLuna()
            with mock.patch.object(group_module, "_load_or_create_embeddings", fake_embeddings):
                with self.assertRaises(ControlledInterrupt):
                    run_pipeline(source, ROOT / "config" / "pipeline.json", output_dir=out,
                                 luna_provider_factory=lambda _root: fake,
                                 interrupt_after_luna_batches=2)
                resumed = run_pipeline(source, ROOT / "config" / "pipeline.json", output_dir=out,
                                       luna_provider_factory=lambda _root: fake)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["enrichment_phase"], "resume")
            grading = out / "grading"
            before = set(json.loads((grading / "checkpoint_before.json").read_text())["completed_ids"])
            after = set(json.loads((grading / "checkpoint_after.json").read_text())["completed_ids"])
            self.assertTrue(before and before < after)
            records = {
                row["review_id"]: row
                for row in map(json.loads, (grading / "records.jsonl").read_text().splitlines())
            }
            cached = {rid for rid, row in records.items() if row.get("cache_source_id")}
            initial, resume, logged = set(), set(), set()
            for event in map(json.loads, (grading / "calls.jsonl").read_text().splitlines()):
                if event["role"] != "enrich":
                    continue
                self.assertIn(event["phase"], {"initial", "resume"})
                self.assertTrue(1 <= len(event["review_ids"]) <= 50)
                if event["phase"] == "resume":
                    self.assertFalse(before & set(event["review_ids"]), "resume re-sent saved IDs")
                if event["outcome"] != "succeeded":
                    continue
                for rid in event["review_ids"]:
                    self.assertEqual(event["label_config"], records[rid]["label_config"])
                    logged.add(rid)
                    (initial if event["phase"] == "initial" else resume).add(rid)
            completed = {rid for rid, row in records.items() if row["status"] == "completed"}
            self.assertFalse(completed - cached - logged, "completed records without a logged call")
            self.assertFalse(before - cached - initial)
            self.assertTrue((after - before - cached) & resume)
            luna_rows = sum(row.get("label_config", "").startswith("gpt-6-luna") for row in records.values())
            self.assertGreater(luna_rows, 100)


class PriorityEligibilityTests(unittest.TestCase):
    def test_strict_rule_then_fallbacks(self) -> None:
        from spotify_pipeline.recommend import priority_eligibility

        ranking = [{"issue_id": i} for i in ("I_PLAYBACK_01", "I_OTHER_01", "I_DOWNLOADS_01", "I_BILLING_01")]
        issues = {
            "I_PLAYBACK_01": {"topic": "playback", "coherent": "no", "actionable": "yes"},
            "I_OTHER_01": {"topic": "other", "coherent": "yes", "actionable": "no"},
            "I_DOWNLOADS_01": {"topic": "downloads", "coherent": "yes", "actionable": "yes"},
            "I_BILLING_01": {"topic": "billing", "coherent": "yes", "actionable": "yes"},
        }
        self.assertEqual(priority_eligibility(issues, ranking), ("coherent_and_actionable", ["I_BILLING_01"]))
        issues["I_BILLING_01"]["coherent"] = "no"
        # Downloads is coherent and actionable but not an allowed priority topic.
        self.assertEqual(
            priority_eligibility(issues, ranking),
            ("actionable_only_fallback", ["I_PLAYBACK_01", "I_BILLING_01"]),
        )
        for value in issues.values():
            value["actionable"] = "no"
        self.assertEqual(priority_eligibility(issues, ranking)[0], "any_issue_fallback")
