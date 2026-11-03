"""Initial XP Prompt worker tasks."""

from __future__ import annotations

from xp_prompt.celery_app import app


@app.task(name="xp_prompt.health_probe")
def health_probe() -> dict[str, str]:
    """Return a deterministic worker liveness response."""
    return {"service": "xp-prompt-worker", "status": "alive"}

