# Earlier full-corpus run (superseded)

This folder is the first complete full-corpus run, kept for provenance. **It is not the submitted result.** The submitted run is [`artifacts/full_run_v4/`](../full_run_v4/), exported to [`grading/`](../../grading/) and described in the [README](../../README.md).

What this earlier run is still used for:

- The local classifier used by the final run was trained from GPT-6 Luna teacher labels collected here and in earlier development runs (see [experiments/distilled/README.md](../../experiments/distilled/README.md)).
- `grouping_comparison.json` is the TF-IDF vs. embedding grouping comparison that chose the grouping method the final run uses.
- Its saved Luna requests (`cost/reference_full_run_calls.jsonl`) gave the per-request usage behind the pre-run cost projection.

Its memo, ranking and issue IDs differ from the final run (different labels and grouping) and should not be read as the final recommendation. Its other evidence is in [`archive/v2_full_run_evidence/`](../../archive/v2_full_run_evidence/).
