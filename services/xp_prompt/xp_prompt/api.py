"""HTTP health surface for the KOT5 XP Prompt API process."""

from __future__ import annotations

from flask import Flask, jsonify

from xp_prompt import __version__
from xp_prompt.config import Settings
from xp_prompt.health import readiness


def create_app(settings: Settings | None = None) -> Flask:
    """Create the XP Prompt API application."""
    app = Flask(__name__)
    resolved_settings = settings or Settings.from_env()

    @app.get("/health/live")
    def health_live():
        return jsonify({"service": "xp-prompt-api", "status": "alive"}), 200

    @app.get("/health/ready")
    def health_ready():
        ready, dependencies = readiness(resolved_settings)
        return (
            jsonify(
                {
                    "service": "xp-prompt-api",
                    "status": "ready" if ready else "unavailable",
                    "version": __version__,
                    "dependencies": dependencies,
                }
            ),
            200 if ready else 503,
        )

    return app


app = create_app()

