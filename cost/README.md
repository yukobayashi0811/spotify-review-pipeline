# Cost and runtime evidence

This folder holds the measured 100-review pilot of the final pipeline, the offline calculator that turns it into a full-run projection, and the measured cost of the final full-corpus run.

## 1. Measured pilot (100 reviews)

The pilot is the final pipeline command on `data/cost_100.csv` with one worker, as the cost specification asks: [config/pilot_100.json](../config/pilot_100.json) is the full-run configuration with `luna.workers` set to 1 (the full run used 3 parallel classify requests; every other stage sends one request at a time in both). It started with a new, empty output folder (no state database, no result cache, nothing read from any other run):

```sh
./.venv/bin/spotify-pipeline run --input data/cost_100.csv --config config/pilot_100.json --output cost/pilot_100   # cold
./.venv/bin/spotify-pipeline run --input data/cost_100.csv --config config/pilot_100.json --output cost/pilot_100   # warm: same command again
```

| Measure | Cold | Warm |
|---|---:|---:|
| Wall time | 52.786 s | 0.0135 s |
| Completed | 100 / 100 | reused |
| GPT-6 Luna requests (classify / verify / group / memo) | 5 (1 / 1 / 2 / 1), 0 failed | 0 |
| Local classifier batches | 2 | 0 |
| API-equivalent cost | $0.00810270 | $0 |
| Every stage skipped as unchanged | no | yes |

Per-stage tokens, batch sizes, workers and times are in [report.md](report.md). Saved evidence: `pilot_100/` (the run folder, including `cold_run_summary.json`, `warm_run_summary.json`, `run_manifest.json` with the source-file hashes of the code that ran, `luna_traces/` and its own `grading/`), exported by `scripts/export_pilot_evidence.py` into `pilot_records.jsonl`, `pilot_calls.jsonl` and `pilot_run.json`.

**Earlier pilot attempts, kept as evidence** ([pilot_attempts/](pilot_attempts/README.md)): one with 3 workers stopped in the memo stage (the validator required two alternatives and the input had one), one with 3 workers completed (44.9 s, $0.00662678), and one with one worker stopped in the memo stage (no issue was eligible for the strict priority rule). Each stop was fail-closed — no memo was written — and led to the code changes listed in [CODE_PROVENANCE.md](../artifacts/full_run_v4/CODE_PROVENANCE.md).

## 2. Offline calculator

```sh
python3 cost/calculator.py                          # writes report.md, report.json, usage.csv
python3 cost/calculator.py --rate-multiplier 2      # what-if: every API rate doubled
python3 cost/calculator.py --budget-usd 0.5 --max-workers 1
python3 cost/calculator.py --self-check             # arithmetic invariants -> replay_check.json
```

Standard library only; it never contacts a provider and needs no key. Inputs: the pilot files above, editable rates in [rates.csv](rates.csv) (GPT-6 Luna: $0.10 / M input, $0.01 / M cached input, $0.50 / M output; local models $0) and editable projection settings in [assumptions.json](assumptions.json) (budget, output-token cap, workers, routed fraction, scenario percentiles). For the projection it takes per-request tokens and durations at the production batch size of 50 from `reference_full_run_calls.jsonl`, the saved requests of the earlier full-corpus run (the pilot's batches are smaller). The self-check confirms that doubling every rate doubles measured and projected cost while leaving measured time and local compute unchanged, and that the pilot IDs and input checksum match the dataset manifest.

Projection made before the final run (`report.md`):

| Scenario | API-equiv. USD | Est. wall s |
|---|---:|---:|
| base, with exact-text reuse | 0.842979 | 8,485.5 |
| conservative, with exact-text reuse | 0.987149 | 11,257.8 |
| base, no reuse | 1.131731 | 11,403.6 |
| conservative, no reuse | 1.316507 | 15,056.8 |

## 3. Scaling path and decision (100 → 500 → 10,000 → full run)

The estimate was refreshed at each development checkpoint before the full run; each step changed the design.

| Checkpoint | Setup measured | Result | Decision |
|---|---|---|---|
| 500 reviews (`checkpoint_500.csv`) | local Gemma model through MLX, 1 worker, 10 reviews per request | 2,458 s for 500 rows (64 successful requests, 2 failed attempts); projected about 29 days for the full corpus ([report](../archive/v2_full_run_evidence/cost/checkpoint_500/report.md), [PERFORMANCE.md](../PERFORMANCE.md)) | too slow; local generation abandoned |
| 500 reviews | GPT-6 Luna `low`, 3 workers, 50 per request | 154 s, 12 requests ([benchmark](../experiments/codex_luna/checkpoint-500-normal-usage-v2/benchmark_report.json)) | scale to 10,000 |
| 10,000 reviews (`analysis_10000.csv`) | same Luna setup | 2,915 s, 191 requests (4 failed, recovered by splitting); all-Luna full run projected at 64.3 h, 80.4 h conservative ([RESULTS.md](../experiments/codex_luna/checkpoint-10000-normal-usage-v2/RESULTS.md), [projection](../experiments/codex_luna/checkpoint-10000-normal-usage-v2/full-corpus-projection.json)) | too long for a shared subscription; train a local classifier on the saved Luna labels and keep Luna for a capped share of risky texts |
| 100 / 500 / 10,000 reviews | local classifier + capped Luna routing | development runs in [experiments/distilled/](../experiments/distilled/README.md) | adopted, with the cap set at 5% of distinct texts |
| full corpus | final pipeline (`config/pipeline.json`) | 6,528 s, $0.929008 (section 4) | — |

Budget and limits chosen for the full run: $5 API-equivalent spend limit, 3 classify requests in flight, at most 5% of distinct texts routed to Luna, per-role request caps, 50 reviews per request.

## 4. Measured full run

`python3 scripts/build_full_run_v4_actuals.py` totals every request of the final run from `grading/calls.jsonl` (both the deliberately interrupted invocation and the resume) at the same rates: [full_run_v4_actuals.md](full_run_v4_actuals.md).

| Measure | Value |
|---|---:|
| GPT-6 Luna requests (classify / verify / group / memo) | 525 (497 / 13 / 14 / 1), of which 42 failed and were retried by splitting |
| Tokens: input / cached input / output | 7,054,638 / 4,807,424 / 1,312,425 |
| API-equivalent cost | **$0.929008** (the run's own spend ledger: $0.929008) |
| Local classifier batches | 9,226 |
| Wall time | 6,528 s (688 s before the deliberate stop + 5,840 s after resuming) |

The measured cost is inside the projected range (base $0.843 – conservative $0.987 with reuse) and far below the $5 run limit. The wall time is shorter than projected mainly because classify requests averaged 28 s in this run (14,136 s summed over 497 requests) against the 47.5 s median of the reference requests the projection used.

## Notes

- GPT-6 Luna ran through the Codex CLI signed in with a ChatGPT subscription. Every USD figure is token usage × the editable API rates, not an invoice.
- Local hardware and electricity were not metered and are reported as unknown.
- The earlier run's cost evidence (frozen 100-review proof, replay union, double-rate receipt, full-run actuals) is kept in `archive/v2_full_run_evidence/cost/`.
