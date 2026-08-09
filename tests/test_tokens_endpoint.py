from models import Merchant


def test_returns_tokens_for_valid_key(client, merchant):
    resp = client.post("/api/tokens", headers={"X-Api-Key": merchant.api_key})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["access_token"] == "access-aaa"
    assert body["refresh_token"] == "refresh-aaa"
    assert body["merchant_id"] == "1111111111"
    assert body["name"] == "Test Store"


def test_rejects_missing_key(client, merchant):
    resp = client.post("/api/tokens")

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_api_key"


def test_rejects_unknown_key(client, merchant):
    resp = client.post("/api/tokens", headers={"X-Api-Key": "not-a-real-key"})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_api_key"


def test_rejects_inactive_merchant(client, db, merchant):
    merchant.active = False
    db.session.commit()

    resp = client.post("/api/tokens", headers={"X-Api-Key": merchant.api_key})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "invalid_api_key"


def test_reports_when_tokens_not_yet_issued(client, db, merchant):
    merchant.access_token = None
    merchant.refresh_token = None
    db.session.commit()

    resp = client.post("/api/tokens", headers={"X-Api-Key": merchant.api_key})

    assert resp.status_code == 409
    assert resp.get_json()["error"] == "not_authorized_yet"


def test_one_merchant_cannot_read_another_merchants_tokens(client, db, merchant):
    """The key IS the identity — there is no parameter to forge."""
    other = Merchant(
        merchant_id="9999999999",
        name="Other Store",
        odoo_url="https://store-b.example.com/salla/webhook/orders",
        active=True,
    )
    other.update_tokens("access-bbb", "refresh-bbb")
    other.generate_api_key()
    db.session.add(other)
    db.session.commit()

    resp = client.post(
        "/api/tokens",
        headers={"X-Api-Key": merchant.api_key},
        json={"merchant_id": "9999999999", "odoo_url": other.odoo_url},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["merchant_id"] == "1111111111"
    assert body["access_token"] == "access-aaa"


def test_key_and_tokens_never_appear_in_logs(client, merchant, caplog):
    with caplog.at_level("INFO"):
        client.post("/api/tokens", headers={"X-Api-Key": merchant.api_key})

    logged = caplog.text
    assert merchant.api_key not in logged
    assert "access-aaa" not in logged
    assert "refresh-aaa" not in logged
