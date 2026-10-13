from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .classify import ModelConfig, _call_event, _failed_result_event
from .io_utils import canonical, file_sha, write_csv, write_json
from .providers import Provider
from .schemas import GROUP_SCHEMA
from .storage import Store


GROUP_SCHEMA_VERSION = "group-v3"
GROUP_PROMPT_VERSION = "2026-10-03-v3"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
RANDOM_SEED = 20261003
NAMING_BUCKET = "G_EMBED_V3"
NAMING_TEXT_LIMIT = 600
# Topics with fewer distinct complaint texts than this form a single cluster.
SMALL_TOPIC_TEXTS = 30
# Incoherent clusters smaller than this are flagged, not split.
MIN_SPLIT_TEXTS = 20
INITIAL_CLUSTER_CAP = 48
TOPICS = (
    "access", "usability", "playback", "downloads",
    "catalog", "billing", "support", "other",
)


def run_grouping(
    *,
    state_path: str | Path,
    provider: Provider,
    prompt_path: str | Path,
    config: ModelConfig,
    run_id: str,
    artifacts_dir: str | Path,
    evidence_per_issue: int = 10,
) -> dict[str, Any]:
    """Build embedding clusters in code and use Luna only for bounded naming."""
    if evidence_per_issue != 10:
        raise ValueError("GROUP_SCHEMA v3 requires exactly 10 representatives per cluster")
    try:
        import joblib
        import numpy as np
        from sklearn.cluster import MiniBatchKMeans
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("Install the embedding and distilled optional dependencies") from error

    artifacts = Path(artifacts_dir)
    artifacts.mkdir(parents=True, exist_ok=True)
    prompt = Path(prompt_path).read_text(encoding="utf-8")
    group_config = (
        f"{config.model}:{config.effort}:group-{GROUP_PROMPT_VERSION}:"
        f"{GROUP_SCHEMA_VERSION}"
    )
    if config.execution_fingerprint:
        group_config += f":{config.execution_fingerprint}"

    with Store(state_path) as store:
        input_fingerprint = _group_input_fingerprint(store)
        if (
            store.get_metadata("group_v3_input_hash") == input_fingerprint
            and store.get_metadata("group_v3_issues")
        ):
            return _export_cached(store, artifacts, input_fingerprint)

        rows = _load_distinct_inputs(store)
        embeddings, embedding_manifest = _load_or_create_embeddings(rows, artifacts)
        by_topic: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            by_topic[row["topic"]].append(index)

        candidates: dict[str, list[dict[str, Any]]] = {}
        diagnostics: dict[str, Any] = {}
        for topic in TOPICS:
            topic_indexes = np.asarray(by_topic.get(topic, []), dtype=np.int64)
            if len(topic_indexes) == 0:
                continue
            if len(topic_indexes) < SMALL_TOPIC_TEXTS:
                # Small inputs (for example the 100-review pilot): too few distinct
                # texts for k=3..10 with ten representatives each, so the whole
                # topic is one code-owned cluster. Full-corpus topics never take
                # this branch.
                topic_embeddings = np.asarray(embeddings[topic_indexes], dtype=np.float32)
                weights = np.asarray(
                    [rows[int(index)]["group_size"] for index in topic_indexes],
                    dtype=np.float64,
                )
                single = {
                    "k": 1,
                    "labels": np.zeros(len(topic_indexes), dtype=np.int32),
                    "centroids": np.vstack([_weighted_centroid(topic_embeddings, weights)]),
                    "effective_k": 1,
                    "silhouette": 0.0,
                    "minimum_cluster_share": 1.0,
                    "valid_minimum_share": True,
                }
                candidates[topic] = [single]
                diagnostics[topic] = {
                    "distinct_texts": int(len(topic_indexes)),
                    "weighted_rows": int(weights.sum()),
                    "small_topic_single_cluster": True,
                    "rule": f"fewer than {SMALL_TOPIC_TEXTS} distinct texts -> one cluster",
                    "candidates": [{"k": 1, "effective_k": 1, "silhouette": 0.0,
                                    "minimum_cluster_share": 1.0,
                                    "valid_minimum_share": True}],
                }
                continue
            topic_embeddings = np.asarray(embeddings[topic_indexes], dtype=np.float32)
            weights = np.asarray(
                [rows[int(index)]["group_size"] for index in topic_indexes], dtype=np.float64
            )
            sample_positions = _deterministic_sample(
                len(topic_indexes), min(20_000, len(topic_indexes))
            )
            topic_candidates: list[dict[str, Any]] = []
            for k in range(3, min(10, len(topic_indexes) // 10) + 1):
                model = MiniBatchKMeans(
                    n_clusters=k,
                    random_state=RANDOM_SEED,
                    batch_size=min(8192, max(1024, len(topic_indexes))),
                    n_init=1,
                    max_iter=10,
                    max_no_improvement=5,
                    reassignment_ratio=0.01,
                )
                labels = model.fit_predict(topic_embeddings, sample_weight=weights)
                cluster_weight = np.bincount(labels, weights=weights, minlength=k)
                minimum_share = float(cluster_weight.min() / cluster_weight.sum())
                silhouette = _cosine_silhouette_exact(
                    topic_embeddings[sample_positions], labels[sample_positions]
                )
                merged_labels, centroids = _merge_centroid_similar(
                    topic_embeddings, labels, weights, threshold=0.90
                )
                topic_candidates.append(
                    {
                        "k": k,
                        "labels": merged_labels,
                        "centroids": centroids,
                        "effective_k": int(len(centroids)),
                        "silhouette": silhouette,
                        "minimum_cluster_share": minimum_share,
                        "valid_minimum_share": minimum_share >= 0.02,
                    }
                )
            candidates[topic] = topic_candidates
            diagnostics[topic] = {
                "distinct_texts": int(len(topic_indexes)),
                "weighted_rows": int(weights.sum()),
                "silhouette_sample_size": int(len(sample_positions)),
                "candidates": [
                    {
                        key: value[key]
                        for key in (
                            "k", "effective_k", "silhouette",
                            "minimum_cluster_share", "valid_minimum_share",
                        )
                    }
                    for value in topic_candidates
                ],
            }

        selected = _select_candidates(candidates, rows, by_topic)
        initial_issues: list[dict[str, Any]] = []
        for topic in TOPICS:
            if topic not in selected:
                continue
            candidate = selected[topic]
            topic_indexes = np.asarray(by_topic[topic], dtype=np.int64)
            topic_weights = np.asarray(
                [rows[int(index)]["group_size"] for index in topic_indexes], dtype=np.float64
            )
            labels = candidate["labels"]
            centroids = candidate["centroids"]
            for label in range(len(centroids)):
                positions = np.flatnonzero(labels == label)
                global_indexes = topic_indexes[positions]
                initial_issues.append(
                    _make_issue(
                        temporary_id=f"TMP_{topic.upper()}_{label + 1:02d}",
                        topic=topic,
                        member_indexes=global_indexes,
                        centroid=np.asarray(centroids[label], dtype=np.float32),
                        embeddings=embeddings,
                        rows=rows,
                        weights=topic_weights[positions],
                    )
                )
            diagnostics[topic]["selected_k"] = int(candidate["k"])
            diagnostics[topic]["selected_effective_k_after_0_90_merge"] = int(
                candidate["effective_k"]
            )

        if len(initial_issues) > INITIAL_CLUSTER_CAP:
            raise RuntimeError("Cap-aware candidate selection exceeded the initial cluster cap")
        initial_names, initial_packs = _name_issues(
            store=store,
            provider=provider,
            prompt=prompt,
            config=config,
            run_id=run_id,
            group_config=group_config,
            issues=initial_issues,
            phase="initial",
        )
        for issue in initial_issues:
            issue.update(initial_names[issue["temporary_id"]])

        post_merged = _merge_named_issues(initial_issues, threshold=0.85)
        incoherent = [issue for issue in post_merged if issue["coherent"] == "no"]
        if len(initial_issues) * 10 + len(incoherent) * 10 > NAMING_TEXT_LIMIT:
            raise RuntimeError(
                "The bounded one-time split would exceed the 600-distinct-text grouping allowance"
            )

        final_issues: list[dict[str, Any]] = []
        split_inputs: list[dict[str, Any]] = []
        split_parent: dict[str, str] = {}
        for issue in post_merged:
            if issue["coherent"] == "yes":
                final_issues.append(issue)
                continue
            if len(issue["member_indexes"]) < MIN_SPLIT_TEXTS:
                issue["flags"] = sorted(
                    set(issue.get("flags", [])) | {"low_coherence", "too_small_to_split"}
                )
                final_issues.append(issue)
                continue
            member_indexes = np.asarray(issue["member_indexes"], dtype=np.int64)
            sub_embeddings = np.asarray(embeddings[member_indexes], dtype=np.float32)
            sub_weights = np.asarray(
                [rows[int(index)]["group_size"] for index in member_indexes], dtype=np.float64
            )
            split_model = MiniBatchKMeans(
                n_clusters=2,
                random_state=RANDOM_SEED,
                batch_size=min(4096, max(256, len(member_indexes))),
                n_init=3,
                max_iter=20,
                max_no_improvement=8,
            )
            sub_labels = split_model.fit_predict(sub_embeddings, sample_weight=sub_weights)
            for sub_label, suffix in ((0, "A"), (1, "B")):
                positions = np.flatnonzero(sub_labels == sub_label)
                child_indexes = member_indexes[positions]
                centroid = _weighted_centroid(
                    np.asarray(embeddings[child_indexes], dtype=np.float32),
                    sub_weights[positions],
                )
                child = _make_issue(
                    temporary_id=f"{issue['temporary_id']}_{suffix}",
                    topic=issue["topic"],
                    member_indexes=child_indexes,
                    centroid=centroid,
                    embeddings=embeddings,
                    rows=rows,
                    weights=sub_weights[positions],
                )
                child["split_depth"] = 1
                split_inputs.append(child)
                split_parent[child["temporary_id"]] = issue["temporary_id"]

        split_packs: list[dict[str, Any]] = []
        if split_inputs:
            split_names, split_packs = _name_issues(
                store=store,
                provider=provider,
                prompt=prompt,
                config=config,
                run_id=run_id,
                group_config=group_config,
                issues=split_inputs,
                phase="coherence_split_once",
            )
            for issue in split_inputs:
                issue.update(split_names[issue["temporary_id"]])
                issue["flags"] = ["split_once"]
                if issue["coherent"] == "no":
                    issue["flags"].append("low_coherence")
                issue["split_parent"] = split_parent[issue["temporary_id"]]
                final_issues.append(issue)

        final_issues = _merge_duplicate_names(final_issues)
        final_issues = _assign_stable_issue_ids(final_issues, rows)
        assignments = [
            (rows[int(index)]["text_sha256"], issue["topic"], issue["issue_id"])
            for issue in final_issues
            for index in issue["member_indexes"]
        ]
        if len(assignments) != len(rows):
            raise RuntimeError(
                f"Distinct-text membership mismatch: expected {len(rows)}, got {len(assignments)}"
            )
        if len({value[0] for value in assignments}) != len(assignments):
            raise RuntimeError("A distinct review text was assigned to more than one issue")

        model_bundle = {
            "version": GROUP_SCHEMA_VERSION,
            "seed": RANDOM_SEED,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_revision": EMBEDDING_REVISION,
            "selected": {
                topic: {
                    "k": int(candidate["k"]),
                    "effective_k": int(candidate["effective_k"]),
                    "silhouette": float(candidate["silhouette"]),
                    "minimum_cluster_share": float(candidate["minimum_cluster_share"]),
                }
                for topic, candidate in selected.items()
            },
            "final_centroids": {
                issue["issue_id"]: issue["centroid"].tolist() for issue in final_issues
            },
        }
        model_path = artifacts / "grouping_model.joblib"
        joblib.dump(model_bundle, model_path)
        model_sha = file_sha(model_path)

        with store.transaction() as connection:
            connection.execute("DELETE FROM membership")
            connection.execute("DELETE FROM issue_names")
            connection.execute("DROP TABLE IF EXISTS temp.group_assignments")
            connection.execute(
                "CREATE TEMP TABLE group_assignments("
                "text_sha256 TEXT PRIMARY KEY,topic TEXT NOT NULL,issue_id TEXT NOT NULL)"
            )
            connection.executemany("INSERT INTO group_assignments VALUES(?,?,?)", assignments)
            connection.execute(
                """
                INSERT INTO membership(issue_id,review_id)
                SELECT a.issue_id,s.review_id
                FROM group_assignments a JOIN source_rows s USING(text_sha256)
                JOIN records r USING(review_id)
                WHERE r.status='completed' AND r.intent IN ('complaint','cancellation')
                  AND r.topic=a.topic
                """
            )
            connection.executemany(
                """
                INSERT INTO issue_names(
                  issue_id,topic,issue_name,description,label_config,coherent,actionable,flags_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        issue["issue_id"], issue["topic"], issue["issue_name"],
                        issue["description"], group_config, issue["coherent"],
                        issue["actionable"], canonical(issue.get("flags", [])),
                    )
                    for issue in final_issues
                ],
            )
        _validate_membership(store)
        store.set_metadata("group_v3_input_hash", input_fingerprint)
        store.set_metadata(
            "group_v3_issues",
            [
                {
                    "issue_id": issue["issue_id"],
                    "topic": issue["topic"],
                    "issue_name": issue["issue_name"],
                    "description": issue["description"],
                    "coherent": issue["coherent"],
                    "actionable": issue["actionable"],
                    "flags": issue.get("flags", []),
                    "distinct_texts": int(len(issue["member_indexes"])),
                    "weighted_rows": int(
                        sum(rows[int(index)]["group_size"] for index in issue["member_indexes"])
                    ),
                }
                for issue in final_issues
            ],
        )
        store.set_metadata("group_v3_model_sha256", model_sha)
        write_json(artifacts / "grouping_diagnostics.json", diagnostics)
        write_json(
            artifacts / "grouping_model_manifest.json",
            {
                "schema_version": GROUP_SCHEMA_VERSION,
                "seed": RANDOM_SEED,
                "artifact_sha256": model_sha,
                "input_sha256": input_fingerprint,
                "embedding": embedding_manifest,
                "pre_naming_centroid_merge_threshold": 0.90,
                "post_naming_merge_threshold": 0.85,
                "minimum_cluster_share": 0.02,
                "candidate_k": [3, 4, 5, 6, 7, 8, 9, 10],
                "small_topic_single_cluster_below_distinct_texts": SMALL_TOPIC_TEXTS,
                "minimum_texts_for_coherence_split": MIN_SPLIT_TEXTS,
                "initial_cluster_cap_for_bounded_split": INITIAL_CLUSTER_CAP,
            },
        )
        write_json(
            artifacts / "group_evidence_and_responses.json",
            {"initial": initial_packs, "split_once": split_packs},
        )
        return _export_cached(store, artifacts, input_fingerprint, model_called=True)


def _load_distinct_inputs(store: Store) -> list[dict[str, Any]]:
    rows = []
    for row in store.connection.execute(
        """
        SELECT t.text_sha256,t.review_text,t.group_size,t.word_count,
               t.representative_review_id,r.topic,r.evidence_quote
        FROM texts t JOIN records r ON r.review_id=t.representative_review_id
        WHERE r.status='completed' AND r.intent IN ('complaint','cancellation')
        ORDER BY t.text_sha256
        """
    ):
        value = dict(row)
        evidence = str(value.get("evidence_quote") or "").strip()
        if int(value["word_count"]) <= 25 or not evidence:
            value["embedding_text"] = str(value["review_text"])
            value["embedding_input_source"] = "full_text_25_words_or_less"
        else:
            value["embedding_text"] = evidence
            value["embedding_input_source"] = "evidence_quote_over_25_words"
        rows.append(value)
    if not rows:
        raise ValueError("No complaint/cancellation distinct texts are available")
    return rows


def _load_or_create_embeddings(
    rows: list[dict[str, Any]], artifacts: Path
) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    array_path = artifacts / "group_embeddings.npy"
    mapping_path = artifacts / "group_embedding_map.csv"
    manifest_path = artifacts / "group_embedding_manifest.json"
    input_sha = hashlib.sha256(
        canonical(
            [[row["text_sha256"], row["embedding_text"], row["topic"]] for row in rows]
        ).encode("utf-8")
    ).hexdigest()
    expected = {
        "model": EMBEDDING_MODEL,
        "revision": EMBEDDING_REVISION,
        "input_sha256": input_sha,
        "rows": len(rows),
        "normalized": True,
    }
    if array_path.is_file() and mapping_path.is_file() and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if all(manifest.get(key) == value for key, value in expected.items()):
            with mapping_path.open(encoding="utf-8", newline="") as stream:
                mapping = list(csv.DictReader(stream))
            if [value["text_sha256"] for value in mapping] == [
                value["text_sha256"] for value in rows
            ]:
                array = np.load(array_path, mmap_mode="r")
                if array.shape[0] == len(rows):
                    return array, manifest

    try:
        import torch
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer
    except ImportError as error:  # pragma: no cover
        raise RuntimeError("Install with: python -m pip install -e '.[embedding]'") from error

    snapshot = Path(
        snapshot_download(
            repo_id=EMBEDDING_MODEL,
            revision=EMBEDDING_REVISION,
            allow_patterns=[
                "1_Pooling/*", "config.json", "config_sentence_transformers.json",
                "modules.json", "sentence_bert_config.json", "special_tokens_map.json",
                "tokenizer.json", "tokenizer_config.json", "vocab.txt", "model.safetensors",
            ],
        )
    )
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(str(snapshot), device=device)
    encoded = model.encode(
        [str(row["embedding_text"]) for row in rows],
        batch_size=256 if device == "mps" else 64,
        show_progress_bar=True,
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).astype(np.float32, copy=False)
    np.save(array_path, encoded, allow_pickle=False)
    write_csv(
        mapping_path,
        [
            "embedding_index", "text_sha256", "representative_review_id", "topic",
            "embedding_input_source", "embedding_input_sha256",
        ],
        [
            {
                "embedding_index": index,
                "text_sha256": row["text_sha256"],
                "representative_review_id": row["representative_review_id"],
                "topic": row["topic"],
                "embedding_input_source": row["embedding_input_source"],
                "embedding_input_sha256": hashlib.sha256(
                    str(row["embedding_text"]).encode("utf-8")
                ).hexdigest(),
            }
            for index, row in enumerate(rows)
        ],
    )
    model_files = [path for path in snapshot.rglob("*") if path.is_file()]
    model_tree_sha = hashlib.sha256(
        canonical(
            [[str(path.relative_to(snapshot)), file_sha(path)] for path in sorted(model_files)]
        ).encode("utf-8")
    ).hexdigest()
    manifest = {
        **expected,
        "dimensions": int(encoded.shape[1]),
        "dtype": str(encoded.dtype),
        "device": device,
        "model_snapshot_path": str(snapshot),
        "model_snapshot_tree_sha256": model_tree_sha,
        "embedding_npy_sha256": file_sha(array_path),
        "mapping_csv_sha256": file_sha(mapping_path),
        "selection_rule": (
            "full review text when word_count <= 25; otherwise saved evidence_quote"
        ),
    }
    write_json(manifest_path, manifest)
    return np.load(array_path, mmap_mode="r"), manifest


def _deterministic_sample(size: int, sample_size: int) -> Any:
    import numpy as np

    if sample_size >= size:
        return np.arange(size, dtype=np.int64)
    rng = np.random.default_rng(RANDOM_SEED)
    return np.sort(rng.choice(size, size=sample_size, replace=False))


def _cosine_silhouette_exact(embeddings: Any, labels: Any) -> float:
    """Exact cosine silhouette without materializing an n-by-n matrix."""
    import numpy as np

    x = np.asarray(embeddings, dtype=np.float32)
    labels = np.asarray(labels, dtype=np.int32)
    unique = np.unique(labels)
    if len(unique) < 2:
        return -1.0
    means = np.vstack([x[labels == label].mean(axis=0) for label in unique])
    counts = np.asarray([(labels == label).sum() for label in unique], dtype=np.int64)
    distances = 1.0 - x @ means.T
    own_positions = np.searchsorted(unique, labels)
    own_counts = counts[own_positions]
    own_dot_sum = np.einsum(
        "ij,ij->i", x, means[own_positions] * own_counts[:, None]
    )
    a = np.where(
        own_counts > 1,
        (own_counts - own_dot_sum) / np.maximum(1, own_counts - 1),
        0.0,
    )
    distances[np.arange(len(x)), own_positions] = np.inf
    b = distances.min(axis=1)
    denominator = np.maximum(a, b)
    values = np.where(denominator > 0, (b - a) / denominator, 0.0)
    values[own_counts == 1] = 0.0
    return float(np.mean(values))


def _weighted_centroid(embeddings: Any, weights: Any) -> Any:
    import numpy as np

    centroid = np.average(embeddings, axis=0, weights=weights)
    norm = float(np.linalg.norm(centroid))
    return centroid / norm if norm else centroid


def _merge_centroid_similar(
    embeddings: Any, labels: Any, weights: Any, *, threshold: float
) -> tuple[Any, Any]:
    import numpy as np

    labels = np.asarray(labels, dtype=np.int32)
    count = int(labels.max()) + 1
    centroids = [
        _weighted_centroid(embeddings[labels == label], weights[labels == label])
        for label in range(count)
    ]
    parent = list(range(count))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for left in range(count):
        for right in range(left + 1, count):
            if float(np.dot(centroids[left], centroids[right])) >= threshold:
                union(left, right)
    roots = sorted({find(value) for value in range(count)})
    root_to_label = {root: index for index, root in enumerate(roots)}
    merged = np.asarray([root_to_label[find(int(value))] for value in labels], dtype=np.int32)
    merged_centroids = np.vstack(
        [
            _weighted_centroid(embeddings[merged == label], weights[merged == label])
            for label in range(len(roots))
        ]
    )
    return merged, merged_centroids


def _select_candidates(
    candidates: dict[str, list[dict[str, Any]]],
    rows: list[dict[str, Any]],
    by_topic: dict[str, list[int]],
) -> dict[str, dict[str, Any]]:
    """Maximize weighted silhouette subject to the bounded naming allowance."""
    topic_weights = {
        topic: sum(rows[index]["group_size"] for index in indexes)
        for topic, indexes in by_topic.items()
    }
    options: dict[str, list[dict[str, Any]]] = {}
    for topic, values in candidates.items():
        valid = [value for value in values if value["valid_minimum_share"]]
        if not valid:
            best_share = max(value["minimum_cluster_share"] for value in values)
            valid = [value for value in values if value["minimum_cluster_share"] == best_share]
        options[topic] = valid

    states: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {0: (0.0, {})}
    for topic in TOPICS:
        if topic not in options:
            continue
        next_states: dict[int, tuple[float, dict[str, dict[str, Any]]]] = {}
        for used, (score, choice) in states.items():
            for candidate in options[topic]:
                new_used = used + int(candidate["effective_k"])
                if new_used > INITIAL_CLUSTER_CAP:
                    continue
                new_score = score + topic_weights[topic] * float(candidate["silhouette"])
                incumbent = next_states.get(new_used)
                new_choice = {**choice, topic: candidate}
                if incumbent is None or new_score > incumbent[0]:
                    next_states[new_used] = (new_score, new_choice)
        states = next_states
    if not states:
        raise RuntimeError("No k=3..10 selection fits the bounded naming allowance")
    _, (_, selected) = max(states.items(), key=lambda item: (item[1][0], -item[0]))
    return selected


def _make_issue(
    *, temporary_id: str, topic: str, member_indexes: Any, centroid: Any,
    embeddings: Any, rows: list[dict[str, Any]], weights: Any,
) -> dict[str, Any]:
    import numpy as np

    members = np.asarray(member_indexes, dtype=np.int64)
    similarities = np.asarray(embeddings[members], dtype=np.float32) @ centroid
    closest_positions = [int(value) for value in np.argsort(-similarities, kind="stable")[:8]]
    selected_positions = set(closest_positions)
    remaining = np.asarray(
        [position for position in range(len(members)) if position not in selected_positions],
        dtype=np.int64,
    )
    needed = min(10, len(members)) - len(closest_positions)
    random_positions: list[int] = []
    if needed > 0:
        rng_seed = int(hashlib.sha256(temporary_id.encode()).hexdigest()[:16], 16)
        rng = np.random.default_rng(rng_seed)
        random_positions = [
            int(value)
            for value in rng.choice(remaining, size=min(needed, len(remaining)), replace=False)
        ]
    positions = [*closest_positions, *random_positions]
    if len(positions) < min(10, len(members)):
        for position in range(len(members)):
            if position not in positions:
                positions.append(position)
            if len(positions) == min(10, len(members)):
                break
    if len(positions) != min(10, len(members)):
        raise RuntimeError(f"Cluster {temporary_id} representative selection failed")
    examples = []
    for position, selection_method in [
        *[(value, "centroid_close") for value in positions[:8]],
        *[(value, "seeded_random") for value in positions[8:]],
    ]:
        row = rows[int(members[position])]
        examples.append(
            {
                "review_id": str(row["representative_review_id"]),
                "review_text": str(row["embedding_text"]),
                "text_sha256": str(row["text_sha256"]),
                "selection_method": selection_method,
            }
        )
    return {
        "temporary_id": temporary_id,
        "topic": topic,
        "member_indexes": members,
        "centroid": centroid,
        "examples": examples,
        "distinct_texts": int(len(members)),
        "weighted_rows": int(sum(rows[int(index)]["group_size"] for index in members)),
        "flags": [],
        "split_depth": 0,
    }


def _name_issues(
    *, store: Store, provider: Provider, prompt: str, config: ModelConfig,
    run_id: str, group_config: str, issues: list[dict[str, Any]], phase: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    packs: list[dict[str, Any]] = []
    names: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(issues), 5):
        chunk = issues[offset : offset + 5]
        model_input = [
            {
                "issue_id": issue["temporary_id"],
                "topic": issue["topic"],
                "examples": [
                    {"review_id": value["review_id"], "review_text": value["review_text"]}
                    for value in issue["examples"]
                ],
            }
            for issue in chunk
        ]
        hashes = list(dict.fromkeys(
            value["text_sha256"] for issue in chunk for value in issue["examples"]
        ))
        newly_reserved = _reserve_group_evidence(store, hashes)
        review_ids = [value["review_id"] for issue in chunk for value in issue["examples"]]
        result = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                result = provider.call(
                    role="group",
                    model=config.model,
                    effort=config.effort,
                    developer_prompt=prompt,
                    user_payload={"issues": model_input},
                    schema=GROUP_SCHEMA,
                    max_output_tokens=min(config.max_output_tokens, 4000),
                    timeout_seconds=config.timeout_seconds,
                )
                values = _validate_group_payload(result.payload, model_input)
                for value in values:
                    names[value["issue_id"]] = value
                event = _call_event(
                    result=result, run_id=run_id, role="group", review_ids=review_ids,
                    model=config.model, effort=config.effort, phase=phase,
                    outcome="succeeded", label_config=group_config,
                )
                event["prompt_version"] = GROUP_PROMPT_VERSION
                event["schema_version"] = GROUP_SCHEMA_VERSION
                store.log_call(event)
                if newly_reserved:
                    store.settle_luna_batch(
                        items=[(NAMING_BUCKET, value) for value in newly_reserved],
                        request_id=result.request_id,
                        succeeded=True,
                        input_tokens=result.input_tokens,
                        cached_input_tokens=result.cached_input_tokens,
                        output_tokens=result.output_tokens,
                        duration_seconds=result.duration_seconds,
                    )
                packs.append(
                    {
                        "issues": model_input,
                        "response": result.payload,
                        "attempt": attempt + 1,
                        "new_distinct_texts_reserved": len(newly_reserved),
                    }
                )
                break
            except Exception as error:
                last_error = error
                if result is not None:
                    event = _failed_result_event(
                        result=result, run_id=run_id, role="group", review_ids=review_ids,
                        model=config.model, effort=config.effort, phase=phase,
                        label_config=group_config, error=f"{type(error).__name__}: {error}",
                    )
                    event["prompt_version"] = GROUP_PROMPT_VERSION
                    event["schema_version"] = GROUP_SCHEMA_VERSION
                    store.log_call(event)
                if attempt == 1:
                    if newly_reserved:
                        store.settle_luna_batch(
                            items=[(NAMING_BUCKET, value) for value in newly_reserved],
                            request_id=result.request_id if result else f"group-v3-failed-{offset}",
                            succeeded=False,
                            duration_seconds=result.duration_seconds if result else 0,
                            error=f"{type(error).__name__}: {error}",
                        )
                    raise
        if last_error is not None and result is None:
            raise last_error
    return names, packs


def _reserve_group_evidence(store: Store, hashes: list[str]) -> list[str]:
    rows = store.connection.execute(
        "SELECT text_sha256,status FROM luna_ledger WHERE bucket=? AND text_sha256 IN (%s)"
        % ",".join("?" for _ in hashes),
        [NAMING_BUCKET, *hashes],
    ).fetchall()
    existing = {str(row["text_sha256"]): str(row["status"]) for row in rows}
    bad = {
        digest: status
        for digest, status in existing.items()
        if status not in {"reserved", "succeeded"}
    }
    if bad:
        raise RuntimeError(f"Grouping evidence has non-reusable ledger states: {bad}")
    missing = [digest for digest in hashes if digest not in existing]
    if not missing:
        return []
    base = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM luna_ledger WHERE bucket<>? "
            "AND status IN ('reserved','succeeded')",
            (NAMING_BUCKET,),
        ).fetchone()[0]
    )
    admitted = store.reserve_luna(
        bucket=NAMING_BUCKET,
        text_hashes=missing,
        bucket_limit=NAMING_TEXT_LIMIT,
        global_limit=base + NAMING_TEXT_LIMIT + 2,
    )
    if set(admitted) != set(missing):
        raise RuntimeError("Grouping evidence reservation would exceed the declared 600-text cap")
    return admitted


def _validate_group_payload(
    payload: dict[str, Any], inputs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or set(payload) != {"issues"}:
        raise ValueError("Invalid GROUP_SCHEMA v3 envelope")
    if not isinstance(payload["issues"], list):
        raise ValueError("GROUP_SCHEMA v3 issues must be a list")
    allowed = {issue["issue_id"]: issue for issue in inputs}
    required = {
        "issue_id", "issue_name", "description", "coherent", "actionable",
        "heterogeneous_review_ids",
    }
    received: set[str] = set()
    values: list[dict[str, Any]] = []
    for item in payload["issues"]:
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("Invalid GROUP_SCHEMA v3 issue")
        issue_id = str(item["issue_id"])
        if issue_id not in allowed or issue_id in received:
            raise ValueError("Unknown or duplicate issue_id")
        evidence_ids = {value["review_id"] for value in allowed[issue_id]["examples"]}
        if item["coherent"] not in {"yes", "no"}:
            raise ValueError("coherent must be yes or no")
        if item["actionable"] not in {"yes", "no"}:
            raise ValueError("actionable must be yes or no")
        if not isinstance(item["heterogeneous_review_ids"], list) or any(
            value not in evidence_ids for value in item["heterogeneous_review_ids"]
        ):
            raise ValueError("Heterogeneous IDs must come from the bounded evidence pack")
        if not str(item["issue_name"]).strip() or not str(item["description"]).strip():
            raise ValueError("Issue name and description must be nonempty")
        received.add(issue_id)
        values.append(item)
    if received != set(allowed):
        raise ValueError("Group response omitted issue IDs")
    return values


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _merge_named_issues(issues: list[dict[str, Any]], *, threshold: float) -> list[dict[str, Any]]:
    import numpy as np

    parent = list(range(len(issues)))

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for left, first in enumerate(issues):
        for right in range(left + 1, len(issues)):
            second = issues[right]
            if first["topic"] != second["topic"]:
                continue
            same_name = _normalize_name(first["issue_name"]) == _normalize_name(second["issue_name"])
            similar = float(np.dot(first["centroid"], second["centroid"])) >= threshold
            if same_name or similar:
                union(left, right)
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, issue in enumerate(issues):
        groups[find(index)].append(issue)
    return [_combine_issue_component(values) for _, values in sorted(groups.items())]


def _merge_duplicate_names(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        groups[(issue["topic"], _normalize_name(issue["issue_name"]))].append(issue)
    return [_combine_issue_component(values) for _, values in sorted(groups.items())]


def _combine_issue_component(values: list[dict[str, Any]]) -> dict[str, Any]:
    import numpy as np

    primary = max(values, key=lambda value: (value["weighted_rows"], value["temporary_id"]))
    members = np.concatenate([np.asarray(value["member_indexes"]) for value in values])
    member_weights = np.asarray([value["weighted_rows"] for value in values], dtype=np.float64)
    centroid = _weighted_centroid(
        np.vstack([value["centroid"] for value in values]), member_weights
    )
    flags = sorted({flag for value in values for flag in value.get("flags", [])})
    if len(values) > 1:
        flags.append("code_merged")
    return {
        **primary,
        "temporary_id": "+".join(sorted(value["temporary_id"] for value in values)),
        "member_indexes": members,
        "centroid": centroid,
        "distinct_texts": int(len(members)),
        "weighted_rows": int(sum(value["weighted_rows"] for value in values)),
        "coherent": "yes" if all(value["coherent"] == "yes" for value in values) else "no",
        "actionable": "yes" if all(value["actionable"] == "yes" for value in values) else "no",
        "heterogeneous_review_ids": sorted(
            {rid for value in values for rid in value.get("heterogeneous_review_ids", [])}
        ),
        "flags": sorted(set(flags)),
    }


def _assign_stable_issue_ids(
    issues: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        by_topic[issue["topic"]].append(issue)
    output: list[dict[str, Any]] = []
    for topic in TOPICS:
        values = by_topic.get(topic, [])
        values.sort(
            key=lambda issue: hashlib.sha256(
                canonical(
                    sorted(rows[int(index)]["text_sha256"] for index in issue["member_indexes"])
                ).encode()
            ).hexdigest()
        )
        for sequence, issue in enumerate(values, 1):
            issue["issue_id"] = f"I_{topic.upper()}_{sequence:02d}"
            output.append(issue)
    return output


def _validate_membership(store: Store) -> None:
    expected = int(
        store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE status='completed' "
            "AND intent IN ('complaint','cancellation')"
        ).fetchone()[0]
    )
    total = int(store.connection.execute("SELECT COUNT(*) FROM membership").fetchone()[0])
    distinct = int(
        store.connection.execute("SELECT COUNT(DISTINCT review_id) FROM membership").fetchone()[0]
    )
    if total != expected or distinct != expected:
        raise RuntimeError(
            f"Membership must be exactly one issue per complaint/cancellation: "
            f"expected={expected}, rows={total}, distinct={distinct}"
        )


def _group_input_fingerprint(store: Store) -> str:
    digest = hashlib.sha256()
    for row in store.connection.execute(
        """
        SELECT r.review_id,r.topic,r.intent,r.severity,r.evidence_quote,s.text_sha256
        FROM records r JOIN source_rows s USING(review_id)
        WHERE r.status='completed' AND r.intent IN ('complaint','cancellation')
        ORDER BY r.review_id
        """
    ):
        digest.update(canonical(list(row)).encode("utf-8"))
    digest.update(EMBEDDING_MODEL.encode())
    digest.update(EMBEDDING_REVISION.encode())
    digest.update(GROUP_SCHEMA_VERSION.encode())
    return digest.hexdigest()


def _export_cached(
    store: Store, artifacts: Path, input_hash: str, model_called: bool = False
) -> dict[str, Any]:
    membership = [
        dict(row)
        for row in store.connection.execute(
            "SELECT issue_id,review_id FROM membership ORDER BY issue_id,review_id"
        )
    ]
    issue_rows = []
    for row in store.connection.execute(
        "SELECT issue_id,topic,issue_name,description,coherent,actionable,"
        "flags_json,label_config FROM issue_names ORDER BY issue_id"
    ):
        value = dict(row)
        value["flags"] = ";".join(json.loads(value.pop("flags_json")))
        issue_rows.append(value)
    write_csv(artifacts / "membership.csv", ["issue_id", "review_id"], membership)
    write_csv(
        artifacts / "issues.csv",
        [
            "issue_id", "topic", "issue_name", "description", "coherent",
            "actionable", "flags", "label_config",
        ],
        issue_rows,
    )
    ledger = store.connection.execute(
        "SELECT status,COUNT(*) AS n FROM luna_ledger WHERE bucket=? GROUP BY status",
        (NAMING_BUCKET,),
    ).fetchall()
    report = {
        "schema_version": GROUP_SCHEMA_VERSION,
        "complaint_or_cancellation_records": len(membership),
        "issues": len(issue_rows),
        "memberships": len(membership),
        "model_called": model_called,
        "input_hash": input_hash,
        "luna_distinct_texts": {str(row["status"]): int(row["n"]) for row in ledger},
        "rule": (
            "Pinned MiniLM embeddings -> L2 normalization -> seeded weighted "
            "MiniBatchKMeans(k=3..10) -> code-owned centroid merges; Luna names "
            "and reviews bounded representatives but cannot alter membership"
        ),
    }
    write_json(artifacts / "group_summary.json", report)
    return report
