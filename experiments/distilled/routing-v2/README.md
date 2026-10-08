# Uncertainty routing development evidence

This experiment selects which locally classified reviews receive the bounded Codex Luna fallback. It does not open or use Golden labels.

`checkpoint_500.csv` is split deterministically into 250 calibration and 250 audit rows with seed `routing-calibration-v1`. The policy is selected only on the calibration rows. The objective is mean macro-F1 across topic, intent, and severity after replacing no more than 20% of rows with their saved Luna pseudo-labels.

The selected rule is `mean_margin`: compute the top-two probability margin separately for topic, intent, and severity, average the three margins, and route the smallest values first. The 20% hard cap is applied before Luna calls.

On the audit segment, the legacy risk router produced mean accuracy 89.733% and mean macro-F1 0.831464 against saved Luna pseudo-labels. The selected rule produced 91.733% and 0.884033. Topic/intent/severity agreement was 91.2% / 90.8% / 93.2%. These are teacher-fidelity results, not human-label accuracy.

Machine-readable evidence is in `report.json`; per-review split, diagnostics, predictions, and route selection are in `routing_cases.csv`.

A separate fresh 100-review execution is stored in `../hybrid-100-uncertainty-v2/`. It completed 100/100 with exactly 20 Luna-routed reviews and passed 16/16 validation checks. Its comparison with an earlier saved Luna run was mixed, which is expected under nondeterministic generation and is retained separately from this fixed teacher-replacement routing study.
