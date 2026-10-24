def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["db"] is True


def test_healthz_ages_are_int_or_null(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    payload = r.json()
    for key in ("billing_age_sec", "bot_age_sec"):
        assert key in payload
        assert payload[key] is None or isinstance(payload[key], int)
