# 100-review cost and runtime report

## Measured pilot

- Provider: local-distilled plus local-mlx; Luna shown only as API-rate equivalent
- Rows: 100
- Input SHA-256: c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e
- Completed / failed or quarantined: 100 / 0
- Unique nonempty texts: 100
- Direct exact-text cache hits: 0
- Batch size / worker limit: 50 / 1
- Successful calls: 6
- Failed attempts: 1
- Input tokens: 14594
- Cached input tokens: 0
- Output tokens: 3516
- Pilot API spend: $0.00000000
- One-time Luna API-equivalent estimate: $0.46379610
- Cold API spend: $0.00000000
- Warm incremental API spend: $0.00000000
- Cold wall time: 176.246427 seconds
- Warm wall time: 0.011639 seconds
- Warm new enrichment calls: 0
- Warm saved-result reuses: 100
- Cold throughput: 0.567387 rows/second
- Cold cost per 1,000 input rows: $0.00000000
- Cold cost per completed record: $0.0000000000
- Hardware and electricity cost: unknown (API charge is tracked separately)

## One-time measured Luna work, API-rate equivalent

- Estimated equivalent: $0.46379610
- Actual billing mode: ChatGPT subscription; this is not an API invoice.
- Pricing basis: editable GPT-6 Luna standard token rates in `rates.csv`.
- Evidence: {"declared_teacher_and_verifier_texts": {"existing_teacher": 8448, "new_teacher": 3560, "new_total": 3960, "new_verifier": 400}, "selected_grouping_565": {"attempted_distinct_review_ids": 565, "cached_input_tokens": 39936, "calls": 14, "input_tokens": 185531, "output_tokens": 6782, "successful_distinct_review_ids": 565}, "selected_memo_two_attempts": {"attempted_distinct_review_ids": 50, "cached_input_tokens": 0, "calls": 2, "input_tokens": 50772, "output_tokens": 4693, "successful_distinct_review_ids": 50}, "teacher_existing_8448": {"attempted_distinct_review_ids": 8448, "cached_input_tokens": 1759232, "calls": 187, "input_tokens": 2359632, "output_tokens": 411099, "successful_distinct_review_ids": 8448}, "teacher_existing_failed_attempts": {"attempted_distinct_review_ids": 200, "cached_input_tokens": 39936, "calls": 4, "input_tokens": 50906, "output_tokens": 9699, "successful_distinct_review_ids": 0}, "teacher_new_3560": {"attempted_distinct_review_ids": 3560, "cached_input_tokens": 728832, "calls": 74, "input_tokens": 990637, "output_tokens": 189038, "successful_distinct_review_ids": 3560}, "token_source": "saved provider call rows and Luna ledger request IDs", "verification_new_400": {"attempted_distinct_review_ids": 400, "cached_input_tokens": 9984, "calls": 8, "input_tokens": 101681, "output_tokens": 22475, "successful_distinct_review_ids": 400}}

## Full-run projection

- Rows / nonempty / empty: 660622 / 660609 / 13
- Distinct nonempty texts with exact reuse: 484189
- Base reuse API cost: $0.463796
- Base no-reuse API cost: $0.463796
- Conservative reuse API cost: $0.579745
- Base reuse elapsed time: 5049.17 seconds
- Base no-reuse elapsed time: 6368.044 seconds
- Conservative reuse elapsed time: 6311.463 seconds
- Base reuse elapsed time: 0.058 days
- Base no-reuse elapsed time: 0.074 days
- Conservative reuse elapsed time: 0.073 days
- Base reuse throughput: 130.837733 rows/second
- Verification rule: declared fixed sample
- Projected verification texts: 400
- Pilot verification rate: 0.1
- Budget: $25.00
- Base exceeds budget: False
- Conservative exceeds budget: False
- Total cost including local compute: unknown

The projection is an initial extrapolation. Refresh after 500 and 10,000 reviews.
