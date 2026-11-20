import hashlib
import hmac
import io
import logging
import os
import sys
import threading

import pytest
from werkzeug.serving import make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import Config, JsonFormatter, create_app  # noqa: E402
from mock_cdh import create_mock  # noqa: E402

SECRET = b"test-secret-value-9f3a\n"
SECRET_TEXT = "test-secret-value-9f3a"


class Server:
    def __init__(self, flask_app):
        self.app = flask_app
        self.srv = make_server("127.0.0.1", 0, flask_app, threaded=True)
        self.port = self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def state(self):
        return self.app.extensions["mock_state"]

    def stop(self):
        self.srv.shutdown()


@pytest.fixture()
def mock_cdh():
    server = Server(create_mock(secret=SECRET))
    yield server
    server.stop()


@pytest.fixture()
def log_stream():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app1")
    logger.addHandler(handler)
    old = logger.level
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(old)


def make_client(cdh_url, **overrides):
    cfg = Config(cdh_base_url=cdh_url, cdh_timeout_seconds=5.0, cdh_retries=0, **overrides)
    return create_app(cfg).test_client()


def expected_hmac(message: str) -> str:
    return hmac.new(SECRET, message.encode(), hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ happy path
def test_compute_matches_local_hmac(mock_cdh):
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["result"] == expected_hmac("hello")
    assert body["algorithm"] == "HMAC-SHA256"
    assert body["resource"] == "default/kbsres1/key1"
    assert set(body) == {"request_id", "result", "algorithm", "resource", "elapsed_ms"}


def test_healthz_readyz_status(mock_cdh):
    client = make_client(mock_cdh.url)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    status = client.get("/api/v1/status").get_json()
    assert status["cdh_reachable"] is True
    assert status["cdh_tee"] == "mock"
    assert status["secret_available"] is False  # nothing cached yet
    assert isinstance(status["tdx_guest_flag"], bool)
    client.post("/api/v1/compute", json={"message": "x"})
    assert client.get("/api/v1/status").get_json()["secret_available"] is True


def test_request_id_is_echoed_and_generated(mock_cdh):
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", json={"message": "a"}, headers={"X-Request-Id": "abc-123"})
    assert resp.headers["X-Request-Id"] == "abc-123"
    assert resp.get_json()["request_id"] == "abc-123"
    resp = client.post("/api/v1/compute", json={"message": "a"}, headers={"X-Request-Id": "bad id!"})
    assert resp.headers["X-Request-Id"] != "bad id!"


# ------------------------------------------------------------------ input validation
@pytest.mark.parametrize("payload", [
    {}, {"message": ""}, {"message": 5}, {"message": None}, {"msg": "x"}, {"message": "x" * 1025}, [],
])
def test_invalid_input_is_400(mock_cdh, payload):
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", json=payload)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_input"
    assert mock_cdh.state["resource_calls"] == 0  # no secret fetch for bad input


def test_non_json_body_is_400(mock_cdh):
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", data="message=hello", content_type="text/plain")
    assert resp.status_code == 400


def test_max_length_message_is_accepted(mock_cdh):
    client = make_client(mock_cdh.url)
    assert client.post("/api/v1/compute", json={"message": "x" * 1024}).status_code == 200


def test_oversized_body_is_413(mock_cdh):
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", data="x" * 20000, content_type="application/json")
    assert resp.status_code == 413


def test_unknown_route_and_method(mock_cdh):
    client = make_client(mock_cdh.url)
    assert client.get("/nope").status_code == 404
    assert client.get("/api/v1/compute").status_code == 405


# ------------------------------------------------------------------ CDH failures
def test_cdh_403_is_502_and_leaks_nothing(mock_cdh):
    mock_cdh.state["mode"] = "forbidden"
    client = make_client(mock_cdh.url)
    resp = client.post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 502
    body = resp.get_json()
    assert body["error"] == "secret_unavailable"
    assert body["detail"] == "cdh_status_403"
    assert "forbidden by mock policy" not in resp.get_data(as_text=True)  # upstream body is not forwarded


def test_cdh_404_is_502(mock_cdh):
    mock_cdh.state["mode"] = "notfound"
    resp = make_client(mock_cdh.url).post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 502
    assert resp.get_json()["detail"] == "cdh_status_404"


def test_cdh_unreachable_is_503():
    client = make_client("http://127.0.0.1:1")  # nothing listens here
    resp = client.post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "cdh_unreachable"
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200  # liveness does not depend on CDH
    assert client.get("/api/v1/status").get_json()["cdh_reachable"] is False


def test_cdh_timeout_is_503(mock_cdh):
    mock_cdh.state["delay"] = 1.5
    cfg = Config(cdh_base_url=mock_cdh.url, cdh_timeout_seconds=0.3, cdh_retries=0)
    resp = create_app(cfg).test_client().post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 503
    assert resp.get_json()["detail"] == "cdh_timeout"


def test_empty_secret_is_502(mock_cdh):
    mock_cdh.state["secret"] = b""
    resp = make_client(mock_cdh.url).post("/api/v1/compute", json={"message": "hello"})
    assert resp.status_code == 502
    assert resp.get_json()["detail"] == "empty_secret"


# ------------------------------------------------------------------ cache behaviour
def test_cache_ttl_300_fetches_once(mock_cdh):
    client = make_client(mock_cdh.url, secret_ttl_seconds=300)
    for _ in range(3):
        assert client.post("/api/v1/compute", json={"message": "hello"}).status_code == 200
    assert mock_cdh.state["resource_calls"] == 1


def test_cache_ttl_0_fetches_every_time(mock_cdh):
    client = make_client(mock_cdh.url, secret_ttl_seconds=0)
    for _ in range(3):
        assert client.post("/api/v1/compute", json={"message": "hello"}).status_code == 200
    assert mock_cdh.state["resource_calls"] == 3
    assert client.get("/api/v1/status").get_json()["secret_available"] is False


def test_failures_are_not_cached(mock_cdh):
    mock_cdh.state["mode"] = "forbidden"
    client = make_client(mock_cdh.url)
    assert client.post("/api/v1/compute", json={"message": "a"}).status_code == 502
    mock_cdh.state["mode"] = "ok"
    assert client.post("/api/v1/compute", json={"message": "a"}).status_code == 200


# ------------------------------------------------------------------ secret hygiene
def test_secret_never_in_responses_or_logs(mock_cdh, log_stream):
    client = make_client(mock_cdh.url)
    bodies = []
    bodies.append(client.post("/api/v1/compute", json={"message": "hello"}).get_data(as_text=True))
    bodies.append(client.get("/api/v1/status").get_data(as_text=True))
    bodies.append(client.get("/readyz").get_data(as_text=True))
    mock_cdh.state["mode"] = "forbidden"
    failing = make_client(mock_cdh.url)
    bodies.append(failing.post("/api/v1/compute", json={"message": "hello"}).get_data(as_text=True))
    logs = log_stream.getvalue()
    assert logs, "expected some log output"
    for text in bodies + [logs]:
        assert SECRET_TEXT not in text
    assert "hello" not in logs  # request bodies are not logged either


def test_security_headers(mock_cdh):
    resp = make_client(mock_cdh.url).get("/healthz")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"


# ------------------------------------------------------------------ config
def test_config_rejects_bad_resource_path(monkeypatch):
    monkeypatch.setenv("KBS_RESOURCE_PATH", "only/two")
    with pytest.raises(SystemExit):
        Config.from_env()


def test_config_defaults(monkeypatch):
    for name in ("PORT", "CDH_BASE_URL", "KBS_RESOURCE_PATH", "SECRET_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config.from_env()
    assert (cfg.port, cfg.cdh_base_url, cfg.kbs_resource_path, cfg.secret_ttl_seconds) == (
        8080, "http://127.0.0.1:8006", "default/kbsres1/key1", 300)
