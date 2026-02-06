def test_http_exception_returns_standard_error_envelope(client, auth_headers):
    response = client.post(
        "/api/game/end",
        json={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "result": "resign",
            "pgn": "1. e4 e5",
        },
        headers=auth_headers(),
    )

    assert response.status_code == 404
    data = response.json()

    assert data["detail"] == "Game session not found"
    assert data["error"]["code"] == "http_404"
    assert data["error"]["message"] == "Game session not found"
    assert data["error"]["retryable"] is False


def test_validation_error_returns_standard_error_envelope(client, auth_headers):
    response = client.post(
        "/api/game/start",
        json={},
        headers=auth_headers(),
    )

    assert response.status_code == 422
    data = response.json()

    assert data["detail"] == "Validation error"
    assert data["error"]["code"] == "validation_error"
    assert data["error"]["message"] == "Validation error"
    assert data["error"]["retryable"] is False
    assert isinstance(data["error"]["details"], list)
