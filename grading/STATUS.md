# Grading export status

This folder is the final full-corpus grading export described in `docs/source_spec/GRADING_CONTRACT.md`, written by the `export` stage of the final run (`spotify-pipeline run`, `pipeline-v4`, output `artifacts/full_run_v4/`).

- Source: `spotify_reviews_18months.csv` (SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6`), 660,622 rows.
- Records (`records.jsonl.gz`): 660,609 completed and 13 quarantined (`empty_review_text`); 176,420 completed rows reuse an identical text's result (`cache_source_id`).
- `membership.csv`: 287,914 complaint/cancellation rows, one issue each (44 issues). `ranking.csv`, `claims.csv`: the final ranking and the memo's 12 claims.
- `calls.jsonl`: every request of both invocations (the deliberately interrupted one and the resume); `checkpoint_before.json` / `checkpoint_after.json`: completed IDs at the stop and at the end.
- Self-check (zero model calls): `check_submission.py check` as documented in the repository README; receipt `artifacts/self_check_final.json` (pass).

`records.jsonl` is committed compressed (`records.jsonl.gz`, byte-identical after `gunzip -k`) because GitHub rejects files over 100 MB.
