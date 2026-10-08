from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from spotify_pipeline.full_run import (  # noqa: E402
    _exclusion_hashes,
    _score_unlabeled_texts,
    _select_active_learning,
)
from spotify_pipeline.io_utils import file_sha, write_json  # noqa: E402
from spotify_pipeline.storage import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--golden-input", type=Path, required=True)
    parser.add_argument("--holdout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1500)
    args = parser.parse_args()
    started = time.perf_counter()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    artifact_sha = file_sha(args.artifact)
    if manifest.get("artifact_sha256") != artifact_sha:
        raise RuntimeError("AL1 artifact does not match its manifest")
    with Store(args.state) as store:
        with store.transaction() as connection:
            connection.execute("DELETE FROM router_scores")
        scored = _score_unlabeled_texts(
            store=store,
            artifact_path=args.artifact,
            artifact_sha=artifact_sha,
            policy_fingerprint="calibrated-error-group-risk-v2-al1",
        )
        _, exact, normalized = _exclusion_hashes([args.golden_input, args.holdout])
        normalized_matches = {
            row[0]
            for row in store.connection.execute(
                "SELECT text_sha256 FROM texts WHERE normalized_text_sha256 IN (%s)"
                % ",".join("?" * len(normalized)),
                tuple(normalized),
            )
        }
        prior = {
            row[0]
            for row in store.connection.execute(
                "SELECT text_sha256 FROM routing UNION SELECT text_sha256 FROM teacher_labels"
            )
        }
        selected = _select_active_learning(
            store=store,
            excluded=exact | normalized_matches | prior,
            limit=args.limit,
        )
        if len(selected) != args.limit:
            raise RuntimeError(f"AL2 selected {len(selected)} texts, expected {args.limit}")
        first_order = int(
            store.connection.execute(
                "SELECT COALESCE(MAX(selected_order),0) FROM routing"
            ).fetchone()[0]
        )
        with store.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO routing(text_sha256,route_bucket,selected_order,reasons_json)
                VALUES(?,?,?,?)
                """,
                (
                    (
                        row["text_sha256"],
                        "AL2",
                        first_order + index,
                        json.dumps(
                            {
                                "p_err": row["p_err"],
                                "mean_margin": row["mean_margin"],
                                "policy": "AL2 uncertainty + topic cap + TF-IDF/SVD k-center",
                                "model_sha256": artifact_sha,
                            },
                            sort_keys=True,
                        ),
                    )
                    for index, row in enumerate(selected, start=1)
                ),
            )
        topic_counts = {
            row[0]: int(row[1])
            for row in store.connection.execute(
                """
                SELECT rs.topic,COUNT(*) FROM routing r JOIN router_scores rs USING(text_sha256)
                WHERE r.route_bucket='AL2' GROUP BY rs.topic ORDER BY COUNT(*) DESC
                """
            )
        }
    report = {
        "status": "selected",
        "router_scored_texts": scored,
        "selected_distinct_texts": len(selected),
        "topic_counts": topic_counts,
        "maximum_topic_fraction": max(topic_counts.values()) / len(selected),
        "artifact_sha256": artifact_sha,
        "golden_labels_opened": False,
        "seconds": round(time.perf_counter() - started, 6),
    }
    write_json(args.output, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

