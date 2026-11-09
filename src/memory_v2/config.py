"""Strict logical configuration; optional embeddings deliberately start disabled."""

from .decay import finite

DEFAULTS = dict(
    enabled=False,
    mode="shadow",
    embedding="none",
    graph_enabled=False,
    batch_size=30,
    idle_seconds=120,
    max_wait_seconds=300,
    daily_learning_tokens=20000,
    input_tokens=6000,
    output_tokens=1200,
    thinking_token_headroom=8000,
    retrieval_token_budget=1600,
    raw_retention_days=7,
    media_retention_hours=24,
    backup_retention_days=7,
    active_capacity=3000,
    cold_capacity=20000,
    ui_enabled=True,
    ui_port=6200,
)


def validate_config(options):
    if not isinstance(options, dict) or set(options) - set(DEFAULTS):
        raise ValueError("Unknown memory_v2 configuration")
    value = dict(DEFAULTS, **options)
    for key in ("enabled", "graph_enabled", "ui_enabled"):
        if not isinstance(value[key], bool):
            raise ValueError("Memory configuration boolean required")
    if value["mode"] not in {"shadow", "active"} or value["embedding"] != "none":
        raise ValueError(
            "Unsupported memory mode/embedding; never silently buy another API"
        )
    for key, low, high in [
        ("batch_size", 1, 60),
        ("idle_seconds", 1, 300),
        ("max_wait_seconds", 1, 600),
        ("daily_learning_tokens", 1200, 200000),
        ("input_tokens", 500, 6000),
        ("output_tokens", 100, 1200),
        ("thinking_token_headroom", 1000, 12000),
        ("retrieval_token_budget", 500, 3200),
        ("ui_port", 1024, 65535),
        ("active_capacity", 1, 3000),
        ("cold_capacity", 1, 20000),
    ]:
        if not isinstance(value[key], int) or not finite(value[key], low, high):
            raise ValueError("Invalid memory bound: " + key)
    if (
        value["raw_retention_days"] != 7
        or value["media_retention_hours"] != 24
        or value["backup_retention_days"] != 7
    ):
        raise ValueError("This schema requires the audited retention defaults")
    return value
