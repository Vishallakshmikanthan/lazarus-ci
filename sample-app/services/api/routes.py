"""API route definitions for the sample service."""

import logging

from flask import Flask, g, jsonify

_log = logging.getLogger("api.routes")


def register_routes(app: Flask) -> None:
    @app.route("/health", methods=["GET"])
    def health():
        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})
        return jsonify({"status": "healthy", "service": "api"})

    @app.route("/items", methods=["GET"])
    def list_items():
        items = [
            {"id": 1, "name": "Widget A", "price": 9.99},
            {"id": 2, "name": "Widget B", "price": 19.99},
            {"id": 3, "name": "Gadget C", "price": 29.99},
        ]
        _log.info("Listed %d items", len(items), extra={"request_id": getattr(g, "request_id", "")})
        return jsonify({"items": items, "count": len(items)})

    @app.route("/items/<int:item_id>", methods=["GET"])
    def get_item(item_id: int):
        items = {
            1: {"id": 1, "name": "Widget A", "price": 9.99},
            2: {"id": 2, "name": "Widget B", "price": 19.99},
            3: {"id": 3, "name": "Gadget C", "price": 29.99},
        }
        item = items.get(item_id)
        if item is None:
            _log.warning("Item %d not found", item_id, extra={"request_id": getattr(g, "request_id", "")})
            return jsonify({"error": "Item not found"}), 404
        return jsonify(item)
