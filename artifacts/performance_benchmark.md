# Local MLX performance benchmark

This is measured development evidence, not a full-corpus execution.
All optimized variants use the same first 20 reviews after one unreported warm-up call.

| Variant | Calls | Reviews | Seconds | Output tokens | Seconds/review |
|---|---:|---:|---:|---:|---:|
| batch=10 | 2 | 20 | 58.383 | 2283 | 2.919 |
| batch=20 | 1 | 20 | 73.297 | 2316 | 3.665 |

- Legacy successful batch-10 mean: 101.467452 seconds/call.
- Legacy output tokens/review: 263.233333.
- Batch 10 vs 20 topic/intent/severity triple agreement: 0.850.
- A batch-size choice still requires the human-labeled golden-set evaluation; agreement is stability, not accuracy.
- Full-corpus projections below are linear scenarios from this 20-review sample, not completed runs.

| Variant | Projected days for 484,189 distinct texts |
|---|---:|
| batch=10 | 16.359 |
| batch=20 | 20.538 |
