# Grading export status

This folder is the final full-corpus grading export described in `docs/source_spec/GRADING_CONTRACT.md`.

- Source: `spotify_reviews_18months.csv` (SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6`), 660,622 rows.
- Records: 660,609 completed and 13 quarantined (`empty_review_text`).
- Membership, ranking, claims, calls, and the two resume checkpoints correspond to the completed full run in `artifacts/full_run_v2/`.
- Zero-API self-check: run `check_submission.py check` as documented in the repository README.

An earlier version of this file described a pre-run scaffold; it is preserved in `archive/pre_audit_2026-10-06/STATUS.md`.
