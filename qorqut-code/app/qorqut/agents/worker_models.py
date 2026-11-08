"""Model identities for bounded operational workers and retirement compatibility."""

LUNA_MODEL = "gpt-6-luna"
LEGACY_LUNA_MODEL = "gpt-5.6-luna"
# Old in-flight journal workers may finish; no new old-model work is created.
LUNA_RESULT_MODELS = frozenset({LUNA_MODEL, LEGACY_LUNA_MODEL})

# Owner-set delegated worker configuration: always Luna; browser work gets more effort.
WORKER_MODEL = LUNA_MODEL
WORKER_EFFORT = {"browser": "high", "other": "medium"}
BROWSER_GRANTS = frozenset({"browser_use", "browser_control"})


def worker_effort(allowed_operations) -> str:
    return WORKER_EFFORT["browser" if BROWSER_GRANTS.intersection(allowed_operations) else "other"]
