# Review experiments (2026-10-02)

Offline only: no Luna/Codex calls, no golden labels used for training or selection.
Run inside the Cowork Linux VM (4 cores, 3.9 GB RAM, Python 3.10, scikit-learn 1.7.2 — NOT the pinned 1.9.1 env),
so numbers are a replica of `distilled-tfidf-linear-v1`, not the saved artifact.

Data
- train: Luna labels from `experiments/codex_luna/checkpoint-10000-normal-usage-v2/predictions.csv` joined to `data/analysis_10000.csv` rows 500..9999,
  excluding first-500 and golden texts by normalized text, conflicting normalized groups removed (7,863 rows).
- test: first 500 rows (`checkpoint_500`) vs saved Luna labels (pseudo-labels, not human).
- gold: `evals/golden_50.csv` (human), reported only for reference.

Scripts
- `e1.py` learning curve (1k/2k/4k/7.9k) and joint (intent, severity) decoding.
- `e2.py <seed>` active learning: 2k random base + 2k random vs 2k lowest mean-margin.
- `e3.py` error rate by length bucket; complaint-subset accuracy.
- `e4.py` sentence-segment topic selection for long reviews (no gain).
- `e5.py` scores all 484,189 distinct texts (timing + candidate pools); writes `full_scores.npz` (not committed).

Run: `python3 e1.py` etc. from this folder with numpy + scikit-learn installed.
- `e7.py` saved Gemma (checkpoint-500.db) vs TF-IDF replica vs Luna on the held-out 500; cascade and agreement-gating simulation.
