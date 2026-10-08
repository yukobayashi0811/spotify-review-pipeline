# Data placement

Included development files:

- `cost_100.csv`
- `checkpoint_500.csv`
- `analysis_10000.csv`
- `manifest.json`

The 97.4 MB `spotify_reviews_18months.csv` is intentionally ignored by Git. Place the authoritative supplied file at `data/spotify_reviews_18months.csv`. This working copy was extracted from the course-supplied Drive ZIP and verified against the required SHA-256:

```text
1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6
```

The blank 50-case human-label template is in `evals/golden_50_to_label.csv` so it remains visibly separate from development inputs.
