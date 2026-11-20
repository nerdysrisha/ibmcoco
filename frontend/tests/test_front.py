import os
import re
import sys
import threading
import time

import pytest
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from front import Config, create_app  # noqa: E402

HEX = "ab" * 32


def make_mock_app1():
    mock = Flask("mock_app1")
    state = {"mode": "ok", "delay": 0.0, "headers": [], "compute_calls": 0}
    mock.extensions["state"] = state

    @mock.get("/api/v1/status")
    def status():
        return jsonify({"app": "app1-cvm-api", "version": "1.0.0", "hostname": "mock-host",
                        "tdx_guest_flag": True, "cdh_reachable": True, "cdh_tee": "tdx",
                        "resource_path": "default/kbsres1/key1", "secret_available": False})

    @mock.post("/api/v1/compute")
    def compute():
        state["compute_calls"] += 1
        state["headers"].append({k.lower(): v for k, v in request.headers.items()})
        if state["delay"]:
            time.sleep(state["delay"])
        mode = state["mode"]
        if mode == "rbac":
            return Response("RBAC: access denied", status=403)
        if mode == "plain403":
            return Response("nope", status=403)
        if mode == "503":
            return Response("upstream connect error or disconnect/reset before headers", status=503)
        if mode == "cdh503":
            resp = jsonify({"error": "cdh_unreachable", "detail": "cdh_unreachable"})
            resp.status_code = 503
            return resp
        if mode == "502":
            resp = jsonify({"error": "secret_unavailable", "detail": "cdh_status_403"})
            resp.status_code = 502
            return resp
        if mode == "400":
            resp = jsonify({"error": "invalid_input", "detail": "message must be 1 to 1024 characters"})
            resp.status_code = 400
            return resp
        if mode == "teapot":
            return Response("x", status=418)
        body = request.get_json()
        return jsonify({"request_id": request.headers.get("X-Request-Id", "none"), "result": HEX,
                        "algorithm": "HMAC-SHA256", "resource": "default/kbsres1/key1", "elapsed_ms": 12,
                        "echo_len": len(body["message"])})

    return mock


class Server:
    def __init__(self, flask_app):
        self.app = flask_app
        self.srv = make_server("127.0.0.1", 0, flask_app, threaded=True)
        self.port = self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self):
        return self.app.extensions["state"]

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def app1():
    server = Server(make_mock_app1())
    yield server
    server.stop()


def make_client(app1_url, **overrides):
    cfg = Config(app1_base_url=app1_url, app1_timeout_seconds=overrides.pop("timeout", 5.0),
                 status_timeout_seconds=2.0, **overrides)
    return create_app(cfg).test_client()


def csrf_token(client):
    html = client.get("/").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "form must carry a csrf token"
    return match.group(1)


def run(client, message="hello", token=None, headers=None):
    token = token or csrf_token(client)
    return client.post("/run", data={"message": message, "csrf_token": token}, headers=headers or {})


# ------------------------------------------------------------------ pages
def test_index_renders_status_and_form(app1):
    client = make_client(app1.url)
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "App 1 status" in html and "mock-host" in html
    assert 'name="csrf_token"' in html
    assert "Set-Cookie" in resp.headers and "csrf_token=" in resp.headers["Set-Cookie"]


def test_healthz_does_not_call_app1():
    client = make_client("http://127.0.0.1:1")
    assert client.get("/healthz").status_code == 200


def test_index_when_app1_is_down():
    client = make_client("http://127.0.0.1:1")
    html = client.get("/").get_data(as_text=True)
    assert "Not reachable" in html and "App 1 is not responding" in html


def test_security_headers(app1):
    resp = make_client(app1.url).get("/")
    assert resp.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Frame-Options"] == "DENY"
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_cookie_is_secure_behind_https_gateway(app1):
    resp = make_client(app1.url).get("/", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in resp.headers["Set-Cookie"]


def test_static_css_is_served(app1):
    resp = make_client(app1.url).get("/static/style.css")
    assert resp.status_code == 200 and "text/css" in resp.content_type
    assert "no-store" not in resp.headers.get("Cache-Control", "")


# ------------------------------------------------------------------ success
def test_run_success_shows_result(app1):
    client = make_client(app1.url)
    resp = run(client, "hello")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert HEX in html and "HMAC-SHA256" in html
    assert "12 ms" in html and "Round trip" in html
    assert "request id" in html


# ------------------------------------------------------------------ failure mapping
@pytest.mark.parametrize("mode,status,text", [
    ("rbac", 502, "Access denied by the service mesh policy"),
    ("plain403", 502, "App 1 refused the request (HTTP 403)"),
    ("503", 502, "App 1 unavailable (check sidecar, mTLS or readiness)"),
    ("cdh503", 502, "App 1 cannot reach its Confidential Data Hub"),
    ("502", 502, "App 1 could not obtain its secret (attestation or policy failure)"),
    ("400", 400, "App 1 rejected the input: message must be 1 to 1024 characters"),
    ("teapot", 502, "Unexpected response from App 1 (HTTP 418)"),
])
def test_app1_failures_have_clear_messages(app1, mode, status, text):
    app1.state["mode"] = mode
    resp = run(make_client(app1.url))
    html = resp.get_data(as_text=True)
    assert resp.status_code == status
    assert text in html
    assert "request id" in html


def test_app1_down_message():
    client = make_client("http://127.0.0.1:1")
    resp = client.post("/run", data={"message": "hello", "csrf_token": csrf_token(client)})
    assert "App 1 is not responding" in resp.get_data(as_text=True)


def test_app1_timeout_message(app1):
    app1.state["delay"] = 1.5
    client = make_client(app1.url, timeout=0.3)
    html = run(client).get_data(as_text=True)
    assert "App 1 is not responding" in html


# ------------------------------------------------------------------ input and CSRF
@pytest.mark.parametrize("message", ["", "   ", "x" * 1025])
def test_input_validation(app1, message):
    resp = run(make_client(app1.url), message)
    assert resp.status_code == 400
    assert "Enter some text" in resp.get_data(as_text=True)
    assert app1.state["compute_calls"] == 0


def test_max_length_message_is_accepted(app1):
    assert run(make_client(app1.url), "x" * 1024).status_code == 200


def test_missing_or_wrong_csrf_is_rejected(app1):
    client = make_client(app1.url)
    client.get("/")
    assert client.post("/run", data={"message": "hello"}).status_code == 400
    assert client.post("/run", data={"message": "hello", "csrf_token": "wrong-token-value-1234567"}).status_code == 400
    assert app1.state["compute_calls"] == 0


def test_csrf_without_cookie_is_rejected(app1):
    fresh = make_client(app1.url)
    # a token copied from another session has no matching cookie in this one
    token = csrf_token(make_client(app1.url))
    assert fresh.post("/run", data={"message": "hello", "csrf_token": token}).status_code == 400


def test_reflected_input_is_html_escaped(app1):
    payload = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    html = run(make_client(app1.url), payload).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html


def test_oversized_form_is_413(app1):
    client = make_client(app1.url)
    resp = client.post("/run", data="x" * 20000, content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 413


# ------------------------------------------------------------------ mesh headers
def test_trace_headers_are_forwarded(app1):
    client = make_client(app1.url)
    headers = {
        "x-request-id": "req-42", "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "x-b3-traceid": "463ac35c9f6413ad48485a3953bb6124", "tracestate": "a=b", "x-other": "dropped",
    }
    run(client, "hello", headers=headers)
    seen = app1.state["headers"][-1]
    assert seen["x-request-id"] == "req-42"
    assert seen["traceparent"].startswith("00-4bf92f35")
    assert seen["x-b3-traceid"] == "463ac35c9f6413ad48485a3953bb6124"
    assert seen["tracestate"] == "a=b"
    assert "x-other" not in seen


def test_request_id_is_generated_when_missing(app1):
    client = make_client(app1.url)
    run(client, "hello")
    assert re.fullmatch(r"[0-9a-f]{32}", app1.state["headers"][-1]["x-request-id"])


# ------------------------------------------------------------------ logs and config
def test_logs_do_not_contain_input_or_result(app1, capsys):
    import io, logging
    from front import JsonFormatter
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app2")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        run(make_client(app1.url), "super-private-input-123")
    finally:
        logger.removeHandler(handler)
    logs = stream.getvalue()
    assert logs
    assert "super-private-input-123" not in logs
    assert HEX not in logs


def test_config_validation(monkeypatch):
    monkeypatch.setenv("APP1_BASE_URL", "app1:8080")
    with pytest.raises(SystemExit):
        Config.from_env()
    monkeypatch.setenv("APP1_BASE_URL", "http://app1:8080/")
    assert Config.from_env().app1_base_url == "http://app1:8080"
