# 100-review cost and runtime calculator

Generated offline by `python3 cost/calculator.py` from `pilot_calls.jsonl`, `pilot_run.json`, `cost/rates.csv` (rate multiplier 1.0) and `assumptions.json`. No provider was contacted.

Luna requests ran through the Codex CLI with ChatGPT subscription authentication. USD figures are usage x the editable API-equivalent rates, not an invoice.

## Measured pilot (cold run)

| Measure | Value |
|---|---|
| Input | `cost_100.csv`, sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (matches manifest: True) |
| Pilot IDs | 100 (all present in pilot_records.jsonl: True) |
| Completed / failed or quarantined | 100 / 0 |
| Distinct nonempty texts / exact-text cache-hit rows | 100 / 0 |
| Result cache at start | empty (new output directory and state database) |
| Routed to Luna | 5 of 100 distinct texts (limit 5; 5 eligible) |
| Verification sample | 10 of 10 sampled rows verified |
| End-to-end wall time | 52.786 s |
| Requests / failed attempts / missing usage | 7 / 0 / 0 |
| API-equivalent cost | $0.00810270 |
| Per 1,000 input rows / per completed record | $0.081027 / $0.00008103 |
| Throughput | 1.894 rows/s |
| Local compute cost | unknown: hardware, electricity and memory were not metered (local classifier, embeddings, SQLite) |

### By stage

| Stage | Provider / model / effort | Prompt / schema | Batch max (requests by size) | Workers | Attempts (ok/failed, retries, fallbacks) | Tokens in / cached / out | API-equiv. USD | Stage wall s | Summed call s |
|---|---|---|---|---|---|---|---|---|---|
| classify_luna | codex-exec-subscription / `gpt-6-luna` / low | 2026-10-02-v3 / enrichment-v2 | 5 ({'5': 1}) | 1 | 1 (1/0, 0, 0) | 11375 / 0 / 254 | 0.00126450 | 10.522 | 10.485 |
| classify_local | local-distilled-sklearn / `distilled-tfidf-linear-v2` / none | 2026-10-02-v3 / enrichment-v2 | 50 ({'45': 1, '50': 1}) | 1 | 2 (2/0, 0, 0) | 0 / 0 / 0 | 0.00000000 | 0.205 | 0.021 |
| verify | codex-exec-subscription / `gpt-6-luna` / low | 2026-10-12-v4 / enrichment-v2 | 10 ({'10': 1}) | 1 | 1 (1/0, 0, 0) | 11409 / 0 / 486 | 0.00138390 | 5.658 | 5.643 |
| group | codex-exec-subscription / `gpt-6-luna` / low | 2026-10-03-v3 / group-v3 | 29 ({'19': 1, '29': 1}) | 1 | 2 (2/0, 0, 0) | 24097 / 0 / 889 | 0.00285420 | 17.051 | 15.272 |
| recommend | codex-exec-subscription / `gpt-6-luna` / low | 2026-10-12-v3 / memo-v3 | 17 ({'17': 1}) | 1 | 1 (1/0, 0, 0) | 15261 / 0 / 2148 | 0.00260010 | 18.710 | 18.688 |

## Measured pilot (warm run, same command)

- Wall time 0.013509 s; every stage skipped unchanged: True.
- New requests 0 (new enrichment requests 0); incremental API-equivalent cost $0.00000000.

## Full-run projection (estimate)

All 660,622 rows accounted for: 660,609 nonempty outputs and 13 empty-text quarantines. Exact-text reuse reduces first-pass work to 484,189 distinct texts (176,420 reused rows); the no-reuse rows show the comparison.

Per-request tokens and durations come from the saved full-run GPT-6 Luna requests at the production batch size of 50 (`reference_full_run_calls.jsonl`), because the pilot's batches are smaller. Local fixed overhead (ingest, export, embeddings/clustering) is added once per run; memo and grouping requests are fixed counts, not per review.

Controls: budget $5.0, output-token cap 6000 per request, max workers 3, max fallback (routed) fraction 0.05.

| Scenario | Distinct texts | Routed fraction | Luna requests (enrich/verify/group/memo) | Failed-attempt rate | API-equiv. USD | Local s | Luna summed s | Est. wall s | Budget |
|---|---:|---:|---|---:|---:|---:|---:|---:|---|
| base/with_exact_text_reuse | 484,189 | 0.05 (measured) | 485/8/14/1 | 0.020906 | 0.842979 | 403.7 | 24245.6 | 8485.5 | within |
| base/no_reuse | 660,609 | 0.05 (measured) | 661/8/14/1 | 0.020906 | 1.131731 | 474.2 | 32788.1 | 11403.6 | within |
| conservative/with_exact_text_reuse | 484,189 | 0.05 (cap) | 485/8/28/2 | 0.05 | 0.987149 | 807.4 | 31351.4 | 11257.8 | within |
| conservative/no_reuse | 660,609 | 0.05 (cap) | 661/8/28/2 | 0.05 | 1.316507 | 948.5 | 42324.9 | 15056.8 | within |

Wall-time estimate = local compute + summed Luna request seconds / workers. Summed request seconds are not wall time when requests overlap. Local compute cost (hardware, electricity) is unknown and not included in USD.
