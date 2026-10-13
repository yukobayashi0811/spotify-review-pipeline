# Spotify next-quarter product recommendation

**Priority:** billing / I_BILLING_04: Focus next-quarter effort on making core listening controls, including song choice, skipping, and seeking, consistently available under the relevant product rules and clearly communicated. The issue is coherent and actionable in the supplied metadata, with the highest actionable baseline issue priority score. Its typical evidence examples describe limits on song choice and changing tracks; treat these as representative examples, not the most severe cases.

**Selected topic / issue:** `billing` / `I_BILLING_04`

The required baseline is complaint_count × mean_severity = severity_sum, using complaint and cancellation records only. The actionable ranking puts I_BILLING_04 first; the topic rollup instead places usability first at 187967, billing fourth at 134753, and playback third at 136213, so these broader topic estimates provide context rather than issue claims. Coverage denominators are 660622 source rows, 660609 classified rows, 13 quarantined empty-text rows, 0 unaccounted rows, and 287914 complaint or cancellation rows; labels came from gpt-6-luna for 23200 rows and local_classifier for 637409. For trend context, full-month complaint/cancellation shares rose from 0.320373 in May 2023 (8990/28061) to 0.670316 in November 2023 (23142/34524), but November is explicitly partial, so this comparison does not qualify as full-month evidence; June 2023 (10146/27117) and October 2023 (64805/89102) are full-month denominators and show a higher share in October. These are corpus-level context only, not issue-specific trends. Sensitivity correction changes the topic ordering: other is estimated first, billing second, and usability third; its top-topic bootstrap frequency is other 994/1000 and billing 6/1000. At issue level, I_OTHER_02 remains the adjusted top issue (bootstrap top probability 1.0) but is non-actionable generic dislike. I_BILLING_04's adjusted estimate remains second among the supplied top adjusted issues, with a 95% interval for its priority score of 31764.635843–53532.546481; these are sensitivity estimates, not baseline claims. The recommendation therefore pairs a specific, high-ranked actionable issue with the available uncertainty and evidence boundaries.

## Baseline and sensitivity interpretation

The baseline top issue is I_OTHER_02, General app dislike (priority score 117639 [C013]), but it is coherent and explicitly non-actionable because it describes broad dislike without a specific product problem. Do not treat it as an effort recommendation. Generic actionable=no issues remain in the baseline and issue-level ranking as descriptive burden; exclude them from actionable issue selection. If sensitivity correction elevates a generic non-actionable issue, report that as a sensitivity estimate and keep it out of the recommendation unless the supplied metadata identifies a coherent actionable problem.

The correction leaves generic I_OTHER_02 at the top adjusted issue position (bootstrap top probability 1.0) and shifts topic estimates from baseline usability-first to adjusted other-first (994/1000 bootstrap top frequency; billing 6/1000). These estimates do not make a generic non-actionable label a coherent product task. I_BILLING_04 remains the leading actionable baseline issue, while its adjusted priority estimate and interval are model outputs, not claims.

## Traceable baseline issue claims

- The baseline ranking records this many complaint and cancellation rows for I_BILLING_04. [C001; I_BILLING_04; complaint_count=22475; reviews: 08ebe7dd-6a4f-4afe-b85e-a1d79d976c5b, f66e1b45-15ae-4348-a0ad-694ebfa80bd8, d2ec681f-9c41-44b1-beef-e959f8c27be8]
- The baseline severity sum for I_BILLING_04 is recorded as follows. [C002; I_BILLING_04; severity_sum=64148; reviews: 08ebe7dd-6a4f-4afe-b85e-a1d79d976c5b, f66e1b45-15ae-4348-a0ad-694ebfa80bd8, d2ec681f-9c41-44b1-beef-e959f8c27be8]
- The baseline mean severity for I_BILLING_04 is recorded as follows. [C003; I_BILLING_04; mean_severity=2.854194; reviews: 08ebe7dd-6a4f-4afe-b85e-a1d79d976c5b, f66e1b45-15ae-4348-a0ad-694ebfa80bd8, d2ec681f-9c41-44b1-beef-e959f8c27be8]
- Using complaint count multiplied by mean severity, the baseline priority score for I_BILLING_04 is recorded as follows. [C004; I_BILLING_04; priority_score=64148; reviews: 08ebe7dd-6a4f-4afe-b85e-a1d79d976c5b, f66e1b45-15ae-4348-a0ad-694ebfa80bd8, d2ec681f-9c41-44b1-beef-e959f8c27be8]
- The baseline ranking records this many complaint and cancellation rows for I_USABILITY_01. [C005; I_USABILITY_01; complaint_count=26570; reviews: 1b78673c-5923-4c03-95eb-25d470704b4d, c3b882ce-7665-49e1-b778-6b6b86d1104b, fee308b6-50f6-4d27-bc29-98fb1c59f02a]
- The baseline severity sum for I_USABILITY_01 is recorded as follows. [C006; I_USABILITY_01; severity_sum=60211; reviews: 1b78673c-5923-4c03-95eb-25d470704b4d, c3b882ce-7665-49e1-b778-6b6b86d1104b, fee308b6-50f6-4d27-bc29-98fb1c59f02a]
- The baseline mean severity for I_USABILITY_01 is recorded as follows. [C007; I_USABILITY_01; mean_severity=2.266127; reviews: 1b78673c-5923-4c03-95eb-25d470704b4d, c3b882ce-7665-49e1-b778-6b6b86d1104b, fee308b6-50f6-4d27-bc29-98fb1c59f02a]
- Using complaint count multiplied by mean severity, the baseline priority score for I_USABILITY_01 is recorded as follows. [C008; I_USABILITY_01; priority_score=60211; reviews: 1b78673c-5923-4c03-95eb-25d470704b4d, c3b882ce-7665-49e1-b778-6b6b86d1104b, fee308b6-50f6-4d27-bc29-98fb1c59f02a]
- The baseline ranking records this many complaint and cancellation rows for I_PLAYBACK_01. [C009; I_PLAYBACK_01; complaint_count=14449; reviews: 47b4bee4-51b3-4e2d-85e2-625d5a70e879, 34ef9137-7d10-41cb-a31d-7ff6648cec58, 7dc4c848-847e-4958-9a60-622dc8f92ba1]
- The baseline severity sum for I_PLAYBACK_01 is recorded as follows. [C010; I_PLAYBACK_01; severity_sum=44827; reviews: 47b4bee4-51b3-4e2d-85e2-625d5a70e879, 34ef9137-7d10-41cb-a31d-7ff6648cec58, 7dc4c848-847e-4958-9a60-622dc8f92ba1]
- The baseline mean severity for I_PLAYBACK_01 is recorded as follows. [C011; I_PLAYBACK_01; mean_severity=3.102429; reviews: 47b4bee4-51b3-4e2d-85e2-625d5a70e879, 34ef9137-7d10-41cb-a31d-7ff6648cec58, 7dc4c848-847e-4958-9a60-622dc8f92ba1]
- Using complaint count multiplied by mean severity, the baseline priority score for I_PLAYBACK_01 is recorded as follows. [C012; I_PLAYBACK_01; priority_score=44827; reviews: 47b4bee4-51b3-4e2d-85e2-625d5a70e879, 34ef9137-7d10-41cb-a31d-7ff6648cec58, 7dc4c848-847e-4958-9a60-622dc8f92ba1]
- Baseline priority score of I_OTHER_02, stated in the prose above; linked by code after the run. [C013; I_OTHER_02; priority_score=117639; reviews: 4cb969c0-544b-494e-9e87-8d704c0c36c4, cc24a214-e7a8-4cba-82bf-4db431ff8008, 8398472d-f697-4d3a-98be-5abafc94c5d4]

Representative review IDs are typical members of each issue (the most common severity, closest to the issue centroid), not its most severe reviews. Each number above is copied by code from the saved baseline ranking.

## Alternatives not selected

- I_USABILITY_01: Frequent ads has more complaints (26,570) and the second actionable rank, and usability leads the baseline topic rollup; defer it because the issue is less specific about the intervention than the Premium-restricted listening controls, and sensitivity-adjusted issue estimates also place it below I_BILLING_04.
- I_PLAYBACK_01: Playback stops or fails is coherent and actionable with higher mean severity (3.102429); defer it because its baseline priority score (44,827) is lower than I_BILLING_04's, and playback's topic rollup ranks third.

## Limitations

- The corpus is historical, self-selected app reviews and does not represent the complete customer population.
- The complete customer population, revenue, plan tier, confirmed churn, and causal retention data are unavailable.
- Cancellation language expresses intent, not observed churn.
- The independent verifier agreement figures are intent 0.874359, exact severity 0.807692, severity MAE 0.202564, and topic 0.812821; the verifier is an independent model pass, not human ground truth.
- Sensitivity estimates are model-based corrections using a local stratum of 290 verified texts, with sparse cells depending on smoothing; bootstrap intervals reflect verifier-sample uncertainty under that correction model, not total model uncertainty. Corrections cannot allocate predicted non-complaints into issues, and adjusted rankings are analytical estimates, not baseline issue claims.
- Grouping uses embedding_group_v3 across 44 issues; remaining grouping incoherence includes low_coherence issues, and the adopted grouping followed an earlier comparison whose details are not supplied here.
- 13 rows were quarantined as unresolved classifications because review text was empty.
- Coverage denominators: 660622 source rows, 660609 classified rows, 13 quarantined rows, and 0 unaccounted rows; 287914 classified rows were complaint or cancellation rows. Label sources accounted for 23200 rows from gpt-6-luna and 637409 from local_classifier.
- Evidence IDs cite typical examples selected by most common issue severity and centroid closeness, not the most severe cases; the supplied examples do not establish prevalence or generalize beyond the described reports.
