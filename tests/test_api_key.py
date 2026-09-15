from models import Merchant


def test_generate_api_key_sets_and_returns_key(db):
    m = Merchant(merchant_id="2222222222", name="Store B")
    key = m.generate_api_key()

    assert key
    assert m.api_key == key
    assert len(key) >= 40


def test_generate_api_key_is_unique_per_merchant(db):
    a = Merchant(merchant_id="3333333333", name="Store C")
    b = Merchant(merchant_id="4444444444", name="Store D")
    a.generate_api_key()
    b.generate_api_key()

    assert a.api_key != b.api_key


def test_merchant_fixture_has_key_and_tokens(merchant):
    assert merchant.api_key
    assert merchant.access_token == "access-aaa"
    assert merchant.refresh_token == "refresh-aaa"


def test_backfill_generates_keys_for_existing_merchants(app, db):
    from app import _backfill_api_keys

    a = Merchant(merchant_id="5555555555", name="Old Store A", active=True)
    b = Merchant(merchant_id="6666666666", name="Old Store B", active=True)
    db.session.add_all([a, b])
    db.session.commit()

    assert a.api_key is None
    assert b.api_key is None

    updated = _backfill_api_keys()

    assert updated == 2
    assert a.api_key
    assert b.api_key
    assert a.api_key != b.api_key


def test_backfill_leaves_existing_keys_untouched(app, db, merchant):
    from app import _backfill_api_keys

    original = merchant.api_key
    updated = _backfill_api_keys()

    assert updated == 0
    assert merchant.api_key == original
