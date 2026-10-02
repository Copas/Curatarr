from curatarr import create_app


def test_unmigrated_app_returns_clear_error(tmp_path):
    app = create_app(
        {
            "TESTING": False,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'fresh.db'}",
            "SECRET_KEY": "test-only",
        }
    )
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json["error"] == "Database migration required"
    response = client.post("/api/v1/webhook/jellyfin", json={})
    assert response.status_code == 503
