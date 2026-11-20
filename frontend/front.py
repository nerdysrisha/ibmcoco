"""App 2: a small web front that runs on a normal worker (not in the TEE).

A user types some text and clicks Run. App 2 calls App 1 (the confidential API) and
shows the result. App 2 holds no secrets and never talks to KBS, CDH or AA.

Only the Flask dependency is needed (the App 1 client uses the standard library).
"""
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

APP_NAME = "app2-web-ui"
APP_VERSION = "1.0.0"

MAX_MESSAGE_CHARS = 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
CSRF_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
CSRF_COOKIE = "csrf_token"

# Headers forwarded to App 1 so Istio/Envoy traces connect across the hop.
TRACE_HEADERS = (
    "x-request-id", "x-b3-traceid", "x-b3-spanid", "x-b3-parentspanid",
    "x-b3-sampled", "x-b3-flags", "b3", "traceparent", "tracestate",
)

log = logging.getLogger("app2")


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    port: int = 8080
    app1_base_url: str = "http://app1:8080"
    app1_timeout_seconds: float = 60.0  # the first call to App 1 runs a full attestation
    status_timeout_seconds: float = 5.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        try:
            cfg = cls(
                port=int(env.get("PORT", cls.port)),
                app1_base_url=env.get("APP1_BASE_URL", cls.app1_base_url).rstrip("/"),
                app1_timeout_seconds=float(env.get("APP1_TIMEOUT_SECONDS", cls.app1_timeout_seconds)),
                status_timeout_seconds=float(env.get("APP1_STATUS_TIMEOUT_SECONDS", cls.status_timeout_seconds)),
                log_level=env.get("LOG_LEVEL", cls.log_level).upper(),
            )
        except ValueError as exc:
            raise SystemExit(f"invalid configuration value: {exc}")
        if not cfg.app1_base_url.startswith(("http://", "https://")):
            raise SystemExit("APP1_BASE_URL must start with http:// or https://")
        if cfg.app1_timeout_seconds <= 0 or cfg.status_timeout_seconds <= 0:
            raise SystemExit("timeouts must be greater than 0")
        return cfg


# --------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}) or {})
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("app2")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- App 1 client
@dataclass
class Reply:
    ok: bool
    http_status: int | None = None
    data: dict | None = None
    kind: str = "ok"  # ok | timeout | unreachable | http_error
    text: str = ""  # short, truncated upstream text (used only to classify errors)
    elapsed_ms: int = 0


def call_app1(cfg: Config, method: str, path: str, payload: dict | None, timeout: float,
              forward_headers: dict) -> Reply:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = dict(forward_headers)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(cfg.app1_base_url + path, data=body, method=method, headers=headers)
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL comes from config)
            raw = resp.read(65536)
            data = _parse_json(raw)
            return Reply(True, resp.status, data, "ok", "", elapsed())
    except urllib.error.HTTPError as exc:
        raw = exc.read(2048)
        exc.close()
        return Reply(False, exc.code, _parse_json(raw), "http_error", raw.decode("utf-8", "replace"), elapsed())
    except (socket.timeout, TimeoutError):
        return Reply(False, None, None, "timeout", "", elapsed())
    except urllib.error.URLError as exc:
        kind = "timeout" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "unreachable"
        return Reply(False, None, None, kind, "", elapsed())
    except OSError:
        return Reply(False, None, None, "unreachable", "", elapsed())


def _parse_json(raw: bytes) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except ValueError:
        return None


def explain(reply: Reply) -> str:
    """Map an App 1 failure to a message a person can act on."""
    if reply.kind == "timeout" or reply.kind == "unreachable":
        return "App 1 is not responding"
    status = reply.http_status
    data = reply.data or {}
    if status == 403 and reply.text.lstrip().startswith("RBAC: access denied"):
        return "Access denied by the service mesh policy"
    if status == 403:
        return "App 1 refused the request (HTTP 403)"
    if status == 503 and data.get("error") == "cdh_unreachable":
        return "App 1 cannot reach its Confidential Data Hub (check the confidential VM's guest services)"
    if status == 503:
        return "App 1 unavailable (check sidecar, mTLS or readiness)"
    if status == 502 and data.get("error") == "secret_unavailable":
        return "App 1 could not obtain its secret (attestation or policy failure)"
    if status == 400:
        return "App 1 rejected the input: " + str(data.get("detail", "invalid input"))[:200]
    return f"Unexpected response from App 1 (HTTP {status})"


# --------------------------------------------------------------------------- app
def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    def forward_headers() -> dict:
        out = {}
        for name in TRACE_HEADERS:
            value = request.headers.get(name)
            if value:
                out[name] = value
        out["x-request-id"] = g.request_id
        return out

    def is_https() -> bool:
        return request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def status_panel() -> dict:
        reply = call_app1(cfg, "GET", "/api/v1/status", None, cfg.status_timeout_seconds, forward_headers())
        if reply.ok and reply.data is not None:
            return {"reachable": True, "data": reply.data, "error": None}
        return {"reachable": False, "data": None, "error": explain(reply)}

    @app.before_request
    def start_request():
        g.start = time.perf_counter()
        g.upstream_status = None
        incoming = request.headers.get("X-Request-Id", "")
        g.request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        cookie = request.cookies.get(CSRF_COOKIE, "")
        g.csrf_token = cookie if CSRF_RE.match(cookie) else secrets.token_urlsafe(32)
        g.set_csrf_cookie = g.csrf_token != cookie

    @app.after_request
    def finish_request(resp):
        elapsed_ms = int((time.perf_counter() - getattr(g, "start", time.perf_counter())) * 1000)
        resp.headers["X-Request-Id"] = getattr(g, "request_id", "-")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        if request.endpoint != "static":
            resp.headers["Cache-Control"] = "no-store"
        if getattr(g, "set_csrf_cookie", False) and request.endpoint in ("index", "run"):
            resp.set_cookie(CSRF_COOKIE, g.csrf_token, httponly=True, samesite="Strict",
                            secure=is_https(), path="/")
        level = logging.DEBUG if request.path == "/healthz" else logging.INFO
        # Never log user input or App 1 result values.
        log.log(level, "request", extra={"fields": {
            "request_id": getattr(g, "request_id", "-"),
            "method": request.method,
            "route": request.path,
            "status": resp.status_code,
            "upstream_status": getattr(g, "upstream_status", None),
            "elapsed_ms": elapsed_ms,
        }})
        return resp

    def page(status_code: int = 200, **context):
        context.setdefault("result", None)
        context.setdefault("error", None)
        context.setdefault("sent_message", "")
        context["csrf_token"] = g.csrf_token
        context["app_version"] = APP_VERSION
        context.setdefault("status", status_panel())
        return render_template("index.html", **context), status_code

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return page()

    @app.post("/run")
    def run():
        form_token = request.form.get("csrf_token", "")
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        if not (form_token and cookie_token and hmac.compare_digest(form_token, cookie_token)):
            return page(400, error={"message": "Invalid or missing form token. Reload the page and try again.",
                                    "http_status": None, "request_id": g.request_id})

        message = request.form.get("message", "")
        if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            return page(400, sent_message=message[:MAX_MESSAGE_CHARS], error={
                "message": f"Enter some text (1 to {MAX_MESSAGE_CHARS} characters).",
                "http_status": None, "request_id": g.request_id})

        reply = call_app1(cfg, "POST", "/api/v1/compute", {"message": message},
                          cfg.app1_timeout_seconds, forward_headers())
        g.upstream_status = reply.http_status
        if reply.ok and reply.data and isinstance(reply.data.get("result"), str):
            return page(200, sent_message=message, result={
                "result": reply.data["result"],
                "algorithm": reply.data.get("algorithm", ""),
                "app1_ms": reply.data.get("elapsed_ms"),
                "app2_ms": reply.elapsed_ms,
                "request_id": reply.data.get("request_id", g.request_id),
                "http_status": reply.http_status,
            })
        request_id = (reply.data or {}).get("request_id") or g.request_id
        return page(400 if reply.http_status == 400 else 502, sent_message=message, error={
            "message": explain(reply), "http_status": reply.http_status, "request_id": request_id})

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException):
            return jsonify({"error": (exc.name or "error").lower().replace(" ", "_")}), exc.code or 500
        log.error("unhandled error", extra={"fields": {
            "request_id": getattr(g, "request_id", "-"), "type": type(exc).__name__}})
        return jsonify({"error": "internal_error"}), 500

    return app


# gunicorn entry point: gunicorn -b 0.0.0.0:8080 --workers 2 --threads 4 front:app
configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
app = create_app()

if __name__ == "__main__":
    run_cfg = Config.from_env()
    log.info("starting", extra={"fields": {"port": run_cfg.port, "app1": run_cfg.app1_base_url}})
    app.run(host="0.0.0.0", port=run_cfg.port, threaded=True)
