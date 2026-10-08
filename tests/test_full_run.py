from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from spotify_pipeline.distilled import _select_evidence_quote, joint_decode
from spotify_pipeline.full_run import _write_checkpoint
from spotify_pipeline.legacy_luna import LEGACY_LABEL_CONFIG, legacy_luna_config
from spotify_pipeline.storage import Store


class _Vectorizer:
    def transform(self, values: list[str]) -> list[str]:
        return values


class _TopicModel:
    classes_ = np.array(["other", "playback"])

    def decision_function(self, values: list[str]) -> np.ndarray:
        return np.array(
            [[0.0, 3.0 if "stops" in value else 0.1] for value in values], dtype=float
        )


class FullRunAcceptanceTests(unittest.TestCase):
    def test_legacy_label_config_is_reproduced_exactly(self) -> None:
        self.assertEqual(legacy_luna_config().label_config, LEGACY_LABEL_CONFIG)
        self.assertEqual(legacy_luna_config().batch_size, 50)

    def test_duplicate_finalization_points_directly_to_representative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with Store(state) as store:
                digest = "a" * 64
                store.upsert_source_rows(
                    [
                        ("r1", "same", "1", "0", "1", "2023", "s1", digest),
                        ("r2", "same", "1", "0", "1", "2023", "s2", digest),
                        ("r3", "same", "1", "0", "1", "2023", "s3", digest),
                    ]
                )
                store.rebuild_texts()
                result = {
                    "topic": "other",
                    "intent": "complaint",
                    "sentiment": -0.8,
                    "severity": 2,
                    "entities": [],
                    "evidence_quote": "same",
                    "needs_review": False,
                }
                self.assertEqual(
                    store.finalize_text_batch(rows=[(digest, result, "local:test")]), 3
                )
                sources = dict(
                    store.connection.execute(
                        "SELECT review_id,cache_source_id FROM records ORDER BY review_id"
                    ).fetchall()
                )
                self.assertIsNone(sources["r1"])
                self.assertEqual(sources["r2"], "r1")
                self.assertEqual(sources["r3"], "r1")

    def test_luna_budget_ledger_reserves_without_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Store(Path(directory) / "state.db") as store:
                rows = []
                for index in range(5):
                    digest = f"{index:064x}"
                    rows.append((f"r{index}", f"t{index}", "1", "0", "1", "2023", f"s{index}", digest))
                store.upsert_source_rows(rows)
                store.rebuild_texts()
                hashes = [row[-1] for row in rows]
                self.assertEqual(
                    len(store.reserve_luna(bucket="AL1", text_hashes=hashes, bucket_limit=4, global_limit=3)),
                    3,
                )
                self.assertEqual(
                    store.reserve_luna(bucket="AL1", text_hashes=hashes[3:], bucket_limit=4, global_limit=3),
                    [],
                )

    def test_checkpoint_before_after_reflect_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with Store(root / "state.db") as store:
                store.upsert_source_rows(
                    [("r1", "text", "1", "0", "1", "2023", "s1", "a" * 64)]
                )
                store.rebuild_texts()
                _write_checkpoint(store, root / "before.json", "before")
                store.reserve_luna(
                    bucket="H", text_hashes=["a" * 64], bucket_limit=1, global_limit=1
                )
                _write_checkpoint(store, root / "after.json", "after")
            before = json.loads((root / "before.json").read_text())
            after = json.loads((root / "after.json").read_text())
            self.assertEqual(before["luna_ledger_rows"], 0)
            self.assertEqual(after["luna_ledger_rows"], 1)

    def test_joint_decode_enforces_hard_severity_one(self) -> None:
        decoded = joint_decode(
            topic_classes=["playback"], topic_probabilities=[1.0],
            intent_classes=["praise", "complaint"], intent_probabilities=[0.9, 0.1],
            severity_classes=[1, 4], severity_probabilities=[0.1, 0.9],
        )
        self.assertEqual(decoded, ("playback", "praise", 1))

    def test_long_quote_is_exact_source_substring(self) -> None:
        text = (
            "The interface has several useful controls and the catalog is broad. "
            "Playback stops every few seconds and makes listening impossible. "
            "I tried reinstalling twice but the same failure continues every day."
        )
        quote = _select_evidence_quote(text, "playback", _Vectorizer(), _TopicModel())
        self.assertIn(quote, text)
        self.assertIn("stops", quote)


if __name__ == "__main__":
    unittest.main()

