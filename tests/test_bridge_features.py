import json
import re
from unittest import mock

import pytest
import requests

import tasks
from models import Merchant, WebhookLog, extract_payload_refs


def make_response(status=200, body=b"{}", cookies=None, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.headers.update(headers or {})
    for name, value in (cookies or {}).items():
        resp.cookies.set(name, value)
    return resp


def test_order_event_refs():
    refs = extract_payload_refs({
        "event": "order.updated",
        "data": {
            "id": 1963119847,
            "reference_id": 283565892,
            "customer": {"id": 555},
            "items": [
                {"product": {"id": 111}},
                {"product": {"id": 222}},
                {"product": {"id": 111}},
            ],
        },
    })

    assert refs == {
        "order_id": "1963119847",
        "order_reference": "283565892",
        "customer_id": "555",
        "product_ids": ",111,222,",
    }


def test_product_and_shipment_refs():
    assert extract_payload_refs({"event": "product.updated", "data": {"id": 42}})["product_ids"] == ",42,"
    refs = extract_payload_refs({"event": "shipment.created", "data": {"order_id": 9, "order_reference_id": 10}})
    assert refs["order_id"] == "9"
    assert refs["order_reference"] == "10"


def test_refs_tolerate_garbage():
    assert extract_payload_refs({"event": "order.created", "data": None})["order_id"] is None
    assert extract_payload_refs("not a dict")["product_ids"] is None


def test_backfill_indexes_existing_logs(app, db):
    from app import _backfill_log_refs

    log = WebhookLog(
        request_id="req-1",
        event_type="order.created",
        payload='{"event": "order.created", "data": {"id": 77, "reference_id": 88, "items": [{"product": {"id": 5}}]}}',
    )
    db.session.add(log)
    db.session.commit()

    assert _backfill_log_refs() == 1
    assert log.salla_order_id == "77"
    assert log.salla_product_ids == ",5,"


def test_product_filter_matches_whole_ids(app, db):
    from app import ProductIdFilter

    db.session.add_all([
        WebhookLog(request_id="a", salla_product_ids=",12,34,"),
        WebhookLog(request_id="b", salla_product_ids=",123,"),
    ])
    db.session.commit()

    flt = ProductIdFilter(WebhookLog.salla_product_ids, "Product ID")
    found = flt.apply(WebhookLog.query, "12").all()

    assert [log.request_id for log in found] == ["a"]


def test_log_list_filters_render(client, db, merchant):
    from models import User

    user = User(username="admin")
    user.set_password("pw")
    db.session.add(user)
    db.session.add(WebhookLog(request_id="r1", event_type="order.updated", salla_merchant_id=merchant.merchant_id,
                              salla_order_id="1963119847", status="forwarded"))
    db.session.add(WebhookLog(request_id="r2", event_type="product.updated", salla_merchant_id=merchant.merchant_id,
                              status="failed"))
    db.session.commit()

    client.post("/login", data={"username": "admin", "password": "pw"})
    resp = client.get("/admin/logs/?search=1963119847")

    assert resp.status_code == 200
    assert b"r1..." in resp.data
    assert b"r2..." not in resp.data
    html = resp.data.decode()
    groups = json.loads(re.search(r'filter-groups-data" style="display:none;">(.*?)</div>', html, re.S).group(1))
    assert ["product.updated", "product.updated"] in groups["Event"][0]["options"]
    status_arg = next(f["arg"] for f in groups["Status"] if f["operation"] == "equals")

    resp = client.get(f"/admin/logs/?flt0_{status_arg}=failed")
    assert b"r2..." in resp.data
    assert b"r1..." not in resp.data


def test_merchant_list_has_copy_buttons(client, db, merchant):
    from models import User

    user = User(username="admin")
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    client.post("/login", data={"username": "admin", "password": "pw"})
    resp = client.get("/admin/merchants/")

    assert resp.status_code == 200
    assert b'data-copy="access-aaa"' in resp.data
    assert b'data-copy="refresh-aaa"' in resp.data


def db_list(names=("yaqoot",)):
    return make_response(body=json.dumps({"jsonrpc": "2.0", "result": list(names)}).encode())


class FakeMerchant:
    def __init__(self, database=None):
        self.odoo_url = "https://odoo.example.com/salla/webhook/orders"
        self.odoo_database = database


def setup_function():
    tasks._odoo_db_sessions.clear()


def test_single_database_posts_without_cookie():
    session = mock.Mock()
    session.post.return_value = make_response()

    tasks.post_to_odoo(session, FakeMerchant(), b"{}", {"X-Salla-Signature": "sig"}, 5)

    headers = session.post.call_args.kwargs["headers"]
    assert "Cookie" not in headers


def test_database_session_is_opened_and_reused():
    session = mock.Mock()
    session.post.return_value = make_response(body=b'{"jsonrpc": "2.0", "result": ["yaqoot"]}')
    session.get.return_value = make_response(302, cookies={"session_id": "sid-1"}, headers={"Location": "/web/login"})

    tasks.post_to_odoo(session, FakeMerchant("yaqoot"), b"{}", {}, 5)
    tasks.post_to_odoo(session, FakeMerchant("yaqoot"), b"{}", {}, 5)

    assert session.get.call_count == 1
    assert session.get.call_args.kwargs["params"] == {"db": "yaqoot"}
    assert session.get.call_args.args[0] == "https://odoo.example.com/web/login"
    assert session.post.call_args.kwargs["headers"]["Cookie"] == "session_id=sid-1"


def test_expired_database_session_is_refreshed_once():
    session = mock.Mock()
    not_found = make_response(body=b'{"jsonrpc": "2.0", "error": {"code": 404, "message": "404: Not Found"}}')
    session.post.side_effect = [db_list(), not_found, db_list(), make_response()]
    session.get.side_effect = [
        make_response(302, cookies={"session_id": "old"}, headers={"Location": "/web/login"}),
        make_response(302, cookies={"session_id": "new"}, headers={"Location": "/web/login"}),
    ]

    tasks.post_to_odoo(session, FakeMerchant("yaqoot"), b"{}", {}, 5)

    assert session.post.call_count == 4
    assert session.post.call_args.kwargs["headers"]["Cookie"] == "session_id=new"


def test_database_without_salla_route_fails_clearly():
    session = mock.Mock()
    not_found = make_response(404)
    session.post.side_effect = [db_list(), not_found, db_list(), not_found]
    session.get.return_value = make_response(302, cookies={"session_id": "sid"}, headers={"Location": "/web/login"})

    with pytest.raises(tasks.OdooDatabaseError, match="no Salla webhook route"):
        tasks.post_to_odoo(session, FakeMerchant("yaqoot"), b"{}", {}, 5)


def test_database_missing_from_odoo_list_fails_before_login():
    session = mock.Mock()
    session.post.return_value = db_list()

    with pytest.raises(tasks.OdooDatabaseError, match="does not exist"):
        tasks.get_odoo_db_session(session, "https://odoo.example.com/salla/webhook/orders", "nope", 5)
    session.get.assert_not_called()


def test_single_database_server_answers_200_with_cookie():
    session = mock.Mock()
    session.post.return_value = make_response(body=b'{"jsonrpc": "2.0", "error": {"code": 200, "message": "Access Denied"}}')
    session.get.return_value = make_response(200, cookies={"session_id": "mono"})

    assert tasks.get_odoo_db_session(session, "https://odoo.example.com/salla/webhook/orders", "only_db", 5) == "mono"


def test_unknown_database_raises():
    session = mock.Mock()
    session.post.return_value = make_response(body=b"not json")
    session.get.return_value = make_response(303, cookies={"session_id": "x"}, headers={"Location": "/web/database/selector"})

    with pytest.raises(tasks.OdooDatabaseError, match="nope"):
        tasks.get_odoo_db_session(session, "https://odoo.example.com/salla/webhook/orders", "nope", 5)


def test_retry_resends_forwarded_webhook(app, db, merchant):
    log = WebhookLog(
        request_id="req-fwd",
        event_type="order.updated",
        salla_merchant_id=merchant.merchant_id,
        status="forwarded",
        payload='{"event": "order.updated", "data": {"id": 1}}',
        headers="{}",
    )
    db.session.add(log)
    db.session.commit()

    ok = make_response(body=b'{"jsonrpc": "2.0", "result": {"status": "success", "result": {"status": "success"}}}')
    with mock.patch.object(tasks, "post_to_odoo", return_value=ok) as post:
        result = tasks.retry_webhook_by_id.run(log.id)

    assert post.called
    assert result["status"] == "forwarded"
    assert log.retry_count == 1
