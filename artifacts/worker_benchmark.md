# Local worker benchmark

This is measured development evidence on the same 20 reviews. Model warm-up is excluded.

| Configuration | Wall seconds |
|---|---:|
| one warm worker, two sequential batch-10 calls | 58.383 |
| two warm workers, one batch-10 call each | 55.403 |

Measured speedup: 1.054x. Decision: **keep one worker**.
