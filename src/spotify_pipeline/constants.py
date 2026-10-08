from __future__ import annotations

SOURCE_FIELDS = (
    "review_id",
    "review_text",
    "review_rating",
    "review_likes",
    "app_version",
    "review_timestamp",
)

TOPICS = (
    "access",
    "usability",
    "playback",
    "downloads",
    "catalog",
    "billing",
    "support",
    "other",
)

INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")
RANKED_INTENTS = {"cancellation", "complaint"}

TOPIC_ISSUES = {
    "access": ("I_ACCESS", "Account access and authentication"),
    "usability": ("I_USABILITY", "Controls navigation and ad interruptions"),
    "playback": ("I_PLAYBACK", "Playback reliability and performance"),
    "downloads": ("I_DOWNLOADS", "Downloads saved music and offline listening"),
    "catalog": ("I_CATALOG", "Catalog search recommendations and lyrics"),
    "billing": ("I_BILLING", "Pricing charges subscriptions and entitlement"),
    "support": ("I_SUPPORT", "Support access and response"),
    "other": ("I_OTHER", "Other product complaints"),
}

AUDIT_VERSION = "a5-audit-v1"
SCHEMA_VERSION = "enrichment-v2"
PROMPT_VERSION = "2026-10-02-v3"
DEFAULT_LABEL_CONFIG = "gpt-6-luna:none:classify-2026-10-02-v3:enrichment-v2"
