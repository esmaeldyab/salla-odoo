import json

import requests

from tasks import classify_odoo_response


def make_response(body, status=200):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
    return resp


def rpc(result):
    return {"jsonrpc": "2.0", "id": None, "result": result}


def test_processed_webhook_is_forwarded():
    resp = make_response(rpc({
        "status": "success",
        "message": "Event order.updated processed",
        "result": {"status": "success", "salla_order_id": 7},
    }))

    assert classify_odoo_response(resp) == ("forwarded", None)


def test_server_traceback_behind_http_200_is_failed():
    resp = make_response({
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": 200, "message": "Odoo Server Error", "data": {"message": "boom"}},
    })

    outcome, detail = classify_odoo_response(resp)

    assert outcome == "failed"
    assert "boom" in detail


def test_rejected_signature_is_failed():
    resp = make_response(rpc({"error": "Invalid signature", "status": "failed"}))

    outcome, detail = classify_odoo_response(resp)

    assert outcome == "failed"
    assert "Invalid signature" in detail


def test_handler_error_is_failed():
    resp = make_response(rpc({
        "status": "success",
        "message": "Event order.status.updated processed",
        "result": {"error": "Order not found", "status": "failed"},
    }))

    outcome, detail = classify_odoo_response(resp)

    assert outcome == "failed"
    assert "Order not found" in detail


def test_partial_reversal_is_not_failed():
    resp = make_response(rpc({
        "status": "success",
        "message": "Event order.updated processed",
        "result": {"status": "partial_success", "error": "credit note left unpaid"},
    }))

    assert classify_odoo_response(resp) == ("forwarded", None)


def test_unhandled_event_is_ignored():
    resp = make_response(rpc({
        "status": "success",
        "message": "Event invoice.created processed",
        "result": {"message": "No handler for event invoice.created"},
    }))

    outcome, detail = classify_odoo_response(resp)

    assert outcome == "ignored"
    assert "invoice.created" in detail


def test_html_page_is_failed():
    resp = make_response("<html><body>Login</body></html>")

    outcome, _detail = classify_odoo_response(resp)

    assert outcome == "failed"
