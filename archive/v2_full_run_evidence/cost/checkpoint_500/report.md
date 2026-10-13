# 500-review cost and runtime report

## Measured checkpoint

- Provider: local-mlx
- Rows: 500
- Input SHA-256: a94e31663ee7b7eaa77e23b7a8425b530cc0c866ed5e714953172a0afef6a12f
- Completed / failed or quarantined: 500 / 0
- Unique nonempty texts: 479
- Direct exact-text cache hits: 21
- Batch size / worker limit: 10 / 1
- Successful calls: 64
- Failed attempts: 2
- Input tokens: 90065
- Cached input tokens: 0
- Output tokens: 66690
- API spend: $0.00000000
- Cold API spend: $0.00000000
- Warm incremental API spend: $0.00000000
- Cold wall time: 2458.466121 seconds
- Warm wall time: not run for this checkpoint
- Warm new enrichment calls: not run for this checkpoint
- Warm saved-result reuses: not run for this checkpoint
- Cold throughput: 0.203379 rows/second
- Cold cost per 1,000 input rows: $0.00000000
- Cold cost per completed record: $0.0000000000
- Hardware and electricity cost: unknown (API charge is tracked separately)

## Full-run projection

- Rows / nonempty / empty: 660622 / 660609 / 13
- Distinct nonempty texts with exact reuse: 484189
- Base reuse API cost: $0.000000
- Base no-reuse API cost: $0.000000
- Conservative reuse API cost: $0.000000
- Base reuse elapsed time: 2503644.879 seconds
- Base no-reuse elapsed time: 3304227.995 seconds
- Conservative reuse elapsed time: 3129556.099 seconds
- Base reuse elapsed time: 28.977 days
- Base no-reuse elapsed time: 38.243 days
- Conservative reuse elapsed time: 36.222 days
- Base reuse throughput: 0.263864 rows/second
- Verification rate: 0.1
- Budget: $25.00
- Base exceeds budget: False
- Conservative exceeds budget: False
- Total cost including local compute: unknown

The projection was refreshed from the measured 500-review checkpoint. Refresh again after 10,000 reviews.
