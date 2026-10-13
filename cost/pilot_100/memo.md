# Spotify next-quarter product recommendation

**Priority:** Focus next-quarter product effort on clarifying and evaluating the free-tier boundary for core playback controls and song selection, including skipping limits. This is the highest-ranked coherent, actionable issue in the actionable ranking. Its topic rollup is 9 complaints and priority score 24, while playback leads the topic rollup at 31 but maps to an incoherent, generic actionable=no issue. Representative billing reviews are typical examples selected by severity and centroid proximity, not extreme cases.

**Selected topic / issue:** `billing` / `I_BILLING_01`

The baseline is defined as priority_score = complaint_count × mean_severity = severity_sum, using complaint and cancellation records only. It ranks I_PLAYBACK_01 first at 31, but that issue groups varied playback behavior and is marked incoherent and actionable=no. The actionable ranking instead places coherent, actionable I_BILLING_01 first, with a focused issue description around Premium restrictions on core controls and song selection. The topic rollup independently puts playback at 31 and billing at 24; these are topic-level analytical estimates, not issue claims. Coverage denominators are 100 source rows, 100 classified rows, 0 quarantined, 0 unaccounted, and 52 complaint/cancellation rows; labels came from gpt-6-luna for 5 rows and local_classifier for 95. Monthly trend shares vary substantially across comparable full months (for example, July 2022: 3/5; October 2023: 16/19), so they provide context rather than a stable signal. The typical billing examples discuss payment restrictions or lost controls after an update, but do not establish prevalence beyond the pack or effects on churn.

## Baseline and sensitivity interpretation

The baseline top issue is I_PLAYBACK_01 (priority score 31), but it is marked incoherent and generic actionable=no, so it is not selected as a product effort target. Generic actionable=no issues remain visible in baseline rankings and topic rollups, but are not treated as actionable targets. Sensitivity correction would elevate one only if a fitted correction and its evidence supported that change; here no verifier-based correction was fitted, and the supplied sensitivity ranking leaves I_PLAYBACK_01 at the top, so none is elevated.

The sensitivity summary reports the unadjusted baseline ordering for issues and topics and explicitly says no verifier-based correction was fitted. Therefore it does not elevate a generic actionable=no issue: I_PLAYBACK_01 remains the top baseline item, while the recommendation is based on actionability and coherence, not a sensitivity-adjusted score. No intervals are supplied in this pack.

## Traceable baseline issue claims

- The baseline ranking records 9 complaints for I_BILLING_01. [C001; I_BILLING_01; complaint_count=9; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline severity sum for I_BILLING_01 is 24. [C002; I_BILLING_01; severity_sum=24; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline mean severity for I_BILLING_01 is 2.666667. [C003; I_BILLING_01; mean_severity=2.666667; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline priority score for I_BILLING_01 is 24. [C004; I_BILLING_01; priority_score=24; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline ranking records 2 complaints for I_ACCESS_01. [C005; I_ACCESS_01; complaint_count=2; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- The baseline severity sum for I_ACCESS_01 is 7. [C006; I_ACCESS_01; severity_sum=7; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- The baseline mean severity for I_ACCESS_01 is 3.500000. [C007; I_ACCESS_01; mean_severity=3.500000; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- The baseline priority score for I_ACCESS_01 is 7. [C008; I_ACCESS_01; priority_score=7; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- I_DOWNLOADS_01 has baseline complaint_count 1. [C009; I_DOWNLOADS_01; complaint_count=1; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline severity_sum 3. [C010; I_DOWNLOADS_01; severity_sum=3; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline mean_severity 3.000000. [C011; I_DOWNLOADS_01; mean_severity=3.000000; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline priority_score 3. [C012; I_DOWNLOADS_01; priority_score=3; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]

Representative review IDs are typical members of each issue (the most common severity, closest to the issue centroid), not its most severe reviews. Each number above is copied by code from the saved baseline ranking.

## Alternatives not selected

- I_ACCESS_01 (access): Coherent and actionable, but it has only 2 complaints and a baseline priority score of 7, well below the selected issue.
- I_DOWNLOADS_01 (downloads) was not selected: its baseline priority score is 3 versus 24 for I_BILLING_01. [code-filled]

## Limitations

- The evidence is a historical, self-selected app-review sample and does not represent the complete customer population.
- The complete customer population, revenue, plan tier, confirmed churn, and causal retention data are absent.
- Cancellation language indicates expressed intent, not observed churn.
- Independent-verifier agreement figures supplied are intent 0.8, severity exact 0.7, severity MAE 0.3, and topic 0.8; these are agreement summaries, not proof of correct labels.
- No verifier-based sensitivity correction was fitted. The sensitivity summary supplies no uncertainty intervals, so it does not establish adjusted issue priorities or precision.
- Grouping remains partly incoherent: several issues are marked incoherent, the adopted method is embedding_group_v3, and the pack notes that low_coherence issues remain after one bounded split.
- Coverage denominator: 100 source rows, of which 100 were classified; 52 were complaint or cancellation rows. There were 0 quarantined rows (no reasons supplied) and 0 unaccounted rows. Label sources were gpt-6-luna for 5 rows and local_classifier for 95 rows.
- The representative evidence quotes are typical examples selected from each issue at its most common severity and nearest the issue centroid, not the most severe cases; they illustrate only the cited review text and should not be generalized beyond it.
- The first and last trend months are partial; monthly shares are context only and do not establish durable changes in issue incidence.
