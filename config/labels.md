# Shared label definitions and examples

These definitions reproduce the assignment's common labels. `review_rating` is source metadata and never substitutes for text classification.

## Topic

- `access`: login, signup, password, or account access. Example: “I cannot sign in after resetting my password.”
- `usability`: navigation, controls, layout, queue or playlist management, or ad interruptions. Example: “The queue controls are hard to find.”
- `playback`: playback failure, crashes, lag, connection failures, audio quality, or resource use. Example: “Every song stops after ten seconds.”
- `downloads`: downloading, saved music, offline listening, or disappearing downloads. Example: “My downloaded songs vanished.”
- `catalog`: missing songs or artists, search/discovery, recommendations, or lyrics. Example: “Lyrics are missing for this track.”
- `billing`: price, charges, subscriptions, paywalls, premium entitlement, or explicitly premium-only controls. Examples: “I paid but Premium did not activate.”; “You have to pay just to skip songs.” A paid-plan mention alone is not billing (“I'm a Premium user and the app crashes” is playback).
- `support`: contacting support or the support response. Example: “Support has not answered my ticket.”
- `other`: general praise or criticism, unrelated content, or no supported specific topic. Example: “Great app.”

Choose the highest-severity supported specific problem. On a severity tie, choose the first specific problem mentioned. For praise, choose the first specifically praised feature.

## Intent

Precedence is `cancellation` > `complaint` > `request` > `praise` > `unclear`.

- `cancellation`: explicit leaving, uninstalling, cancelling, or a threat to do so.
- `complaint`: a negative experience, including mixed praise and criticism.
- `request`: a desired change without a reported failure.
- `praise`: positive experience without higher-precedence content.
- `unclear`: unrelated, meaningless, or unsupported content. Bare boycott slogans remain unclear unless a product complaint or personal departure is expressed.

## Severity

- `1`: no reported problem, praise, neutral/unclear content, or a pure feature request.
- `2`: dislike, generic criticism, minor annoyance, or cosmetic issue with no supported functional loss.
- `3`: degraded or restricted function with some use or workaround remaining.
- `4`: clearly blocked core task, such as inability to log in or play music.
- `5`: explicit serious financial, privacy, or data harm.

Stars, anger, price, crash language, or cancellation intent alone do not establish severity. Missing impact context sets `needs_review=true`.

## Other fields

- `sentiment`: a number from `-1` to `1`, based only on the review text. Negative text is below zero, neutral text is zero, and positive text is above zero. Decimals are allowed.
- `entities`: a JSON array containing only explicitly named product features, devices, platforms, or content. Use `[]` when none is explicit.
- `evidence_quote`: a nonempty, exact, case-sensitive substring of `review_text` that supports the selected label. The whole text is acceptable for a short review.
- `needs_review`: `true` when the text is ambiguous, the impact is missing, or the shared rules do not resolve the label cleanly; otherwise `false`.

For the human golden set, make every judgment without viewing model predictions. Rating, likes, app version, and timestamp are preserved source metadata and are not label evidence.
