from unittest.mock import patch


def test_integrations_list_listenbrainz(client):
    providers = [row["provider"] for row in client.get("/api/integrations").json()]
    assert providers == ["listenbrainz"]


def test_integrations_need_a_session(raw_client):
    assert raw_client.get("/api/integrations").status_code == 401
    assert raw_client.delete("/api/integrations/listenbrainz").status_code == 401


def test_connect_listenbrainz_rejects_a_bad_token(client):
    with patch("app.scrobbling.listenbrainz.validate_token", return_value=False):
        resp = client.post("/api/integrations/listenbrainz", json={"token": "11111111-2222-4333-8444-555555555555"})
    assert resp.status_code == 400
    assert "ListenBrainz" in resp.json()["detail"]


def test_connect_and_disconnect_listenbrainz(client):
    with patch("app.scrobbling.listenbrainz.validate_token", return_value="me") as check:
        resp = client.post("/api/integrations/listenbrainz", json={"token": " 11111111-2222-4333-8444-555555555555 "})
    assert resp.status_code == 200
    check.assert_called_once_with("11111111-2222-4333-8444-555555555555")
    linked = next(r for r in client.get("/api/integrations").json() if r["provider"] == "listenbrainz")
    assert linked["linked"] is True and linked["external_username"] == "me"
    assert client.delete("/api/integrations/listenbrainz").status_code == 200
    linked = next(r for r in client.get("/api/integrations").json() if r["provider"] == "listenbrainz")
    assert linked["linked"] is False


def test_connect_listenbrainz_validates_fields(client):
    assert client.post("/api/integrations/listenbrainz", json={"token": ""}).status_code == 422
    # A ListenBrainz token is a UUID; anything else never reaches ListenBrainz.
    assert client.post("/api/integrations/listenbrainz", json={"token": "not\na token"}).status_code == 422
    assert client.post("/api/integrations/lastfm", json={"token": "x"}).status_code in (404, 405)
