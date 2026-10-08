# Cost/runtime evidence

`cost/` contains the measured 100-review fresh frozen execution, its zero-call warm replay, saved provider token evidence, editable rates, and an offline calculator.

## Measured commands

The cold run began with no `cost/frozen_100/` directory, no state database, and no result cache. It did not read the full-run/10,000 state as a cache.

```sh
./.venv/bin/spotify-pipeline full-run --mode frozen \
  --input data/cost_100.csv \
  --config config/cost_100_frozen.json
```

The same command was run again without changing inputs. Results:

| Measure | Cold | Warm |
|---|---:|---:|
| Wall time | 176.246427s | 0.011639s |
| Completed | 100/100 | reused |
| New enrich calls | 2 | 0 |
| New verify/group/memo attempts | 1/2/2 | 0/0/0 |
| All unchanged stages skipped | no | yes |

The cold proof includes prepare, sealed distilled classification, blind local-Gemma verification of 10, code-owned pilot grouping plus local naming/review, deterministic ranking, memo role, code-owned claims, and grading export. The pilot's one-topic groups are execution evidence only, not the adopted full-corpus embedding grouping.

## Offline replay

```sh
./.venv/bin/python scripts/export_frozen_cost_evidence.py
./.venv/bin/spotify-pipeline cost-replay \
  --calls cost/all_measured_calls.jsonl \
  --rates cost/rates.csv \
  --usage cost/usage.csv \
  --report cost/report.md \
  --run-metadata cost/pilot_run.json
```

No provider is contacted. `pilot_records.jsonl` and `pilot_calls.jsonl` contain the fresh 100-review evidence. `teacher_fixed_calls.jsonl` contains actual saved call rows selected by legacy state or Luna-ledger request IDs. `all_measured_calls.jsonl` is their replay union.

The fixed token evidence includes:

- existing teacher: 8,448 successful texts plus four retained failed attempts covering 200 rows;
- new teacher: 3,560 texts (`H`, `R`, `AL1`, `AL2`);
- new independent verification: 400 texts (`V`), bringing the requested new total to 3,960;
- adopted embedding grouping: 565 bounded evidence texts;
- adopted memo: two attempts, one failed validation and one succeeded.

At the editable GPT-6 Luna standard rates ($0.10/M input, $0.01/M cached input, $0.50/M output), the saved calls equal an estimated $0.46379610. Actual calls used ChatGPT subscription authentication, so this is not an API invoice. The 1.25 conservative estimate is $0.579745. Local hardware/electricity is unknown.

## Double-rate proof

```sh
./.venv/bin/python scripts/verify_cost_replay.py
```

The script replays saved data twice. With all nonzero monetary rates doubled, the equivalent increases from $0.46379610 to $0.92759220 (exactly 2.0×), while the measured cold runtime remains 176.246427 seconds. Receipt: `double_rate_check/verification.json`.

## Files

- `frozen_100/cold_run_summary.json`, `warm_run_summary.json`, state, stage log, role outputs, grading export
- `pilot_records.jsonl`, `pilot_calls.jsonl`, `pilot_run.json`
- `teacher_fixed_calls.jsonl`, `all_measured_calls.jsonl`
- `rates.csv`, `usage.csv`, `report.json`, `report.md`
- `double_rate_check/rates_2x.csv`, `usage.csv`, `report.*`, `verification.json`

The post-run Codex subscription percentage is stored separately in `artifacts/full_run_v2/usage_after_all.json`, because it is account-wide and cannot be derived from tokens or attributed solely to this assignment.
