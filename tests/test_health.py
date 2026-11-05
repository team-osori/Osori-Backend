from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    """Verify health against the observable API or database contract."""
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
