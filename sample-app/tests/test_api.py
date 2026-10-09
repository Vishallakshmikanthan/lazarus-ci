"""Basic tests for the sample API service."""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.api.app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_health_endpoint(client):
    """Test the health check endpoint returns healthy status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "healthy"
    assert data["service"] == "api"


def test_list_items(client):
    """Test listing all items returns expected data."""
    response = client.get("/items")
    assert response.status_code == 200
    data = response.get_json()
    assert "items" in data
    assert data["count"] == 3
    assert len(data["items"]) == 3


def test_get_item_exists(client):
    """Test getting an existing item by ID."""
    response = client.get("/items/1")
    assert response.status_code == 200
    data = response.get_json()
    assert data["id"] == 1
    assert data["name"] == "Widget A"


def test_get_item_not_found(client):
    """Test getting a non-existent item returns 404."""
    response = client.get("/items/999")
    assert response.status_code == 404
    data = response.get_json()
    assert "error" in data
