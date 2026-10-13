# Earlier 100-review pilot attempts

All three ran `spotify-pipeline run` on `data/cost_100.csv` into a new, empty output folder. They are kept so the failures are visible; the submitted pilot is [`../pilot_100/`](../pilot_100/). Each folder has the run's summary, log, traces and outputs (the SQLite state is not committed).

| Folder | Workers | Outcome | Luna requests | What happened next |
|---|---:|---|---|---|
| `1_three_workers_memo_stopped/` | 3 | stopped in `recommend` after 92.4 s | 4 succeeded + 2 failed memo attempts | The memo validator required two alternative issues, but this small input had only one eligible alternative. It now requires `min(2, eligible)`. |
| `2_three_workers_completed/` | 3 | completed in 44.9 s; warm rerun 0.0135 s with 0 requests | 5 | Valid, but the cost specification asks the pilot to start with one worker, so it was redone with `config/pilot_100.json`. |
| `3_one_worker_memo_stopped/` | 1 | stopped in `recommend` after 65.3 s | 4 succeeded + 2 failed memo attempts | The grouping role judged the only coherent and actionable issue to be in `downloads`, which is not one of the five candidate topics, so no priority was eligible. The rule now relaxes in that case and the memo must say so. |

In both stops the validator rejected the memo role's output and no memo was written (fail-closed); the rejected memo outputs are in each folder's `luna_traces/`, and the stop reason is in its `run_log.jsonl` and `run_summary.json`. The code changes are listed in [CODE_PROVENANCE.md](../../artifacts/full_run_v4/CODE_PROVENANCE.md), and `scripts/robustness_sweep.py` now checks small inputs for this class of failure without model calls.
