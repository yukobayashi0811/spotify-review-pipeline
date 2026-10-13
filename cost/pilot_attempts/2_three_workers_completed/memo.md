# Spotify next-quarter product recommendation

**Priority:** billing / I_BILLING_01: Focus next-quarter product effort on making the basic playback controls available and predictable for users on the basic tier, covering song selection, skipping, and seeking within tracks. The issue is coherent and actionable in the supplied issue metadata and ranks first in the actionable issue list. The three cited evidence records are typical severity-3 examples selected by severity frequency and centroid proximity, not worst-case examples.

**Selected topic / issue:** `billing` / `I_BILLING_01`

The required baseline score is complaint_count × mean_severity = severity_sum, using complaint and cancellation records only. The topic rollup puts playback first at 31, followed by other at 28, usability at 25, and billing at 24; these are analytical topic estimates, not issue claims. The highest-ranked actionable, coherent issue is I_BILLING_01, which gives a specific product focus. Coverage denominators are 100 source rows, 100 classified rows, 0 quarantined rows, 0 unaccounted rows, and 52 complaint-or-cancellation rows; 5 classified rows came from gpt-6-luna and 95 from local_classifier. Among comparable full months, complaint-or-cancellation shares range from 0/2 in December 2022 to 16/19 in October 2023, so this small sample does not establish a stable trend. The 2022-06 and 2023-11 months are partial and excluded from that comparison.

## Baseline and sensitivity interpretation

The baseline top issue is I_PLAYBACK_01, but it is marked coherent=no and actionable=no, with a broad mix of behaviors. Generic actionable=no issues are not treated as focused product recommendations. Sensitivity correction can elevate such an issue only if a fitted correction actually changes its estimate or ranking; none was fitted here, so I_PLAYBACK_01 remains the unadjusted baseline top and its status is unchanged.

The sensitivity summary reports the same issue and topic ordering as baseline and explicitly says no verifier-based correction was fitted. Thus generic actionable=no issues such as I_PLAYBACK_01 are not promoted by a correction in this pack. If a fitted sensitivity model did elevate one, that would be a separate analytical signal warranting review, not an automatic override of the coherence/actionability criteria. No sensitivity intervals are supplied.

## Traceable baseline issue claims

- The baseline records nine complaints for restrictions on basic playback controls and features to Premium. [C001; I_BILLING_01; complaint_count=9; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline severity sum for this issue is 24. [C002; I_BILLING_01; severity_sum=24; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline mean severity for this issue is 2.666667. [C003; I_BILLING_01; mean_severity=2.666667; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- Using complaint count × mean severity, the baseline priority score is 24. [C004; I_BILLING_01; priority_score=24; reviews: a78d831a-69f4-428a-9235-cfdd9775c509, 7084ced1-0428-4093-9328-442f74719b4f, d146397f-4a94-4980-be00-ae9212eca3a7]
- The baseline records two complaints about account sign-in problems. [C005; I_ACCESS_01; complaint_count=2; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- The baseline severity sum for this issue is 7. [C006; I_ACCESS_01; severity_sum=7; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- The baseline mean severity for this issue is 3.500000. [C007; I_ACCESS_01; mean_severity=3.500000; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- Using complaint count × mean severity, the baseline priority score is 7. [C008; I_ACCESS_01; priority_score=7; reviews: de0a9cdf-8974-48bc-802f-331228df4619]
- I_DOWNLOADS_01 has baseline complaint_count 1. [C009; I_DOWNLOADS_01; complaint_count=1; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline severity_sum 3. [C010; I_DOWNLOADS_01; severity_sum=3; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline mean_severity 3.000000. [C011; I_DOWNLOADS_01; mean_severity=3.000000; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]
- I_DOWNLOADS_01 has baseline priority_score 3. [C012; I_DOWNLOADS_01; priority_score=3; reviews: dcff57c6-abd6-413b-b255-aec3dc34fcbd]

Representative review IDs are typical members of each issue (the most common severity, closest to the issue centroid), not its most severe reviews. Each number above is copied by code from the saved baseline ranking.

## Alternatives not selected

- I_ACCESS_01 (access): It is coherent and actionable, but the actionable ranking places it second with only two complaints, versus nine for the recommended billing issue.
- I_DOWNLOADS_01 (downloads) was not selected: its baseline priority score is 3 versus 24 for I_BILLING_01. [code-filled]

## Limitations

- The data is a historical, self-selected app-review sample and does not represent the complete customer population.
- The pack has no complete customer population, revenue, plan-tier, or confirmed-churn data.
- Cancellation language records expressed intent, not observed churn.
- Independent-verifier agreement in the pack is intent 0.7, severity exact 0.6, severity MAE 0.7, and topic 0.8; these figures indicate imperfect agreement.
- No verifier-based sensitivity correction was fitted. The sensitivity summary supplies no corrected estimate or interval, so ranks remain unadjusted; issue-level correction also cannot allocate predicted non-complaints into latent issues.
- Grouping remains partly incoherent: the adopted embedding_group_v3 grouping has eight issues, including low-coherence groups such as playback and usability; only one grouping method is run in this pack.
- There were zero quarantined rows (0 of 100); thus there are no quarantined row IDs or reasons to list as unresolved classifications. Coverage reports 100 source rows, 100 classified rows, 0 unaccounted rows, and 52 complaint-or-cancellation rows, with 5 classified rows from gpt-6-luna and 95 from local_classifier.
- Evidence IDs are typical examples selected at the most common issue severity and closest to the issue centroid, not necessarily the most severe cases; they should not be generalized beyond what those examples show.
- Monthly trend shares use complaint-or-cancellation rows over classified rows; June 2022 and November 2023 are partial months. The monthly denominators vary, so trend is context only and does not establish a durable change.
