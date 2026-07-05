"""Tests for the archie-agent application."""

from archie_agent.app import app
from starlette.testclient import TestClient


def test_status_returns_ok():
    client = TestClient(app)
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_status_content_type():
    client = TestClient(app)
    response = client.get("/status")
    assert response.headers["content-type"] == "application/json"
