"""Minimal Flask API service for CI/CD pipeline testing."""

import uuid

from flask import Flask, g, request

from services.api.logging_config import setup_logging
from services.api.routes import register_routes

_log = setup_logging()


def create_app() -> Flask:
    app = Flask(__name__)
    register_routes(app)

    @app.before_request
    def _attach_request_id():
        g.request_id = request.headers.get("X-Request-Id", uuid.uuid4().hex)

    @app.after_request
    def _log_request(response):
        _log.info(
            "%s %s -> %d",
            request.method,
            request.path,
            response.status_code,
            extra={"request_id": getattr(g, "request_id", "")},
        )
        return response

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
