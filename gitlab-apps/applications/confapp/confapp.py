"""App 1: runs inside the confidential peer pod (TDX VM).

It fetches a secret from the Confidential Data Hub through the CoCo REST API server
(default http://127.0.0.1:8006) and exposes a small API for other apps.

The secret is only used inside this process (HMAC-SHA256). It is never returned,
logged, or included in an error message.

Only the Flask dependency is needed (the CDH client uses the standard library).
"""
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

APP_NAME = "app1-cvm-api"
APP_VERSION = "1.0.0"

RESOURCE_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_MESSAGE_CHARS = 1024
PROBE_PATHS = ("/healthz", "/readyz")

log = logging.getLogger("app1")


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    port: int = 8080
    cdh_base_url: str = "http://127.0.0.1:8006"
    kbs_resource_path: str = "default/kbsres1/key1"
    secret_ttl_seconds: int = 300  # 0 = fetch the secret on every request
    cdh_timeout_seconds: float = 45.0  # the first call runs a full attestation
    cdh_retries: int = 2  # retries on connection errors only
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        try:
            cfg = cls(
                port=int(env.get("PORT", cls.port)),
                cdh_base_url=env.get("CDH_BASE_URL", cls.cdh_base_url).rstrip("/"),
                kbs_resource_path=env.get("KBS_RESOURCE_PATH", cls.kbs_resource_path).strip("/"),
                secret_ttl_seconds=int(env.get("SECRET_TTL_SECONDS", cls.secret_ttl_seconds)),
                cdh_timeout_seconds=float(env.get("CDH_TIMEOUT_SECONDS", cls.cdh_timeout_seconds)),
                cdh_retries=int(env.get("CDH_RETRIES", cls.cdh_retries)),
                log_level=env.get("LOG_LEVEL", cls.log_level).upper(),
            )
        except ValueError as exc:
            raise SystemExit(f"invalid configuration value: {exc}")
        if not RESOURCE_PATH_RE.match(cfg.kbs_resource_path):
            raise SystemExit("KBS_RESOURCE_PATH must look like <repository>/<type>/<tag>")
        if cfg.secret_ttl_seconds < 0 or cfg.cdh_retries < 0 or cfg.cdh_timeout_seconds <= 0:
            raise SystemExit("SECRET_TTL_SECONDS, CDH_RETRIES must be >= 0 and CDH_TIMEOUT_SECONDS > 0")
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
    root = logging.getLogger("app1")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- CDH client
class CdhError(Exception):
    """Raised for any failure to get the secret. Never carries upstream bodies."""

    def __init__(self, kind: str, upstream_status: int | None = None):
        super().__init__(kind)
        self.kind = kind  # cdh_unreachable | cdh_timeout | cdh_status | empty_secret
        self.upstream_status = upstream_status


class CdhClient:
    """Thin client for the CoCo REST API (api-server-rest/openapi/api.json)."""

    def __init__(self, base_url: str, timeout: float, retries: int):
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries

    def _get(self, path: str, timeout: float) -> bytes:
        req = urllib.request.Request(self.base_url + path, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed local URL)
            return resp.read()

    def info(self, timeout: float = 3.0) -> dict | None:
        """GET /info. Returns the parsed JSON, or None if CDH is not reachable."""
        try:
            data = json.loads(self._get("/info", timeout))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, urllib.error.URLError):
            return None

    def get_resource(self, resource_path: str) -> bytes:
        """GET /cdh/resource/{repository}/{type}/{tag} -> raw bytes."""
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._get("/cdh/resource/" + resource_path, self.timeout)
            except urllib.error.HTTPError as exc:
                exc.close()  # do not read the body
                raise CdhError("cdh_status", exc.code) from None
            except (socket.timeout, TimeoutError):
                raise CdhError("cdh_timeout") from None
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    raise CdhError("cdh_timeout") from None
                if isinstance(reason, ConnectionError) and attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise CdhError("cdh_unreachable") from None
            except ConnectionError:
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise CdhError("cdh_unreachable") from None
            except OSError:
                raise CdhError("cdh_unreachable") from None
        raise CdhError("cdh_unreachable")  # pragma: no cover


class SecretCache:
    """Holds the secret in memory for ttl seconds (0 = never cache)."""

    def __init__(self, client: CdhClient, resource_path: str, ttl: int):
        self._client = client
        self._path = resource_path
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: bytes | None = None
        self._expires = 0.0

    def get(self) -> bytes:
        with self._lock:  # one fetch at a time, so a burst makes a single attestation
            now = time.monotonic()
            if self._ttl > 0 and self._value is not None and now < self._expires:
                return self._value
            value = self._client.get_resource(self._path)
            if not value:
                raise CdhError("empty_secret")
            if self._ttl > 0:
                self._value, self._expires = value, now + self._ttl
            else:
                self._value = None
            return value

    def available(self) -> bool:
        return self._value is not None and time.monotonic() < self._expires


# --------------------------------------------------------------------------- helpers
def tdx_guest_flag() -> bool:
    """Informational only: the Linux 'tdx_guest' CPU flag. Not a security proof."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("flags"):
                    return "tdx_guest" in line.split()
    except OSError:
        pass
    return False


def error_response(status: int, error: str, detail: str):
    resp = jsonify({"error": error, "detail": detail, "request_id": getattr(g, "request_id", None)})
    resp.status_code = status
    return resp


# --------------------------------------------------------------------------- app
def create_app(config: Config | None = None) -> Flask:
    cfg = config or Config.from_env()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024
    app.config["JSON_SORT_KEYS"] = False

    client = CdhClient(cfg.cdh_base_url, cfg.cdh_timeout_seconds, cfg.cdh_retries)
    cache = SecretCache(client, cfg.kbs_resource_path, cfg.secret_ttl_seconds)
    app.extensions["cdh_client"] = client
    app.extensions["secret_cache"] = cache

    @app.before_request
    def start_request():
        g.start = time.perf_counter()
        incoming = request.headers.get("X-Request-Id", "")
        g.request_id = incoming if REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex

    @app.after_request
    def finish_request(resp):
        elapsed_ms = int((time.perf_counter() - getattr(g, "start", time.perf_counter())) * 1000)
        resp.headers["X-Request-Id"] = getattr(g, "request_id", "-")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        level = logging.DEBUG if request.path in PROBE_PATHS else logging.INFO
        # Never log bodies, query strings or headers other than the request id.
        log.log(level, "request", extra={"fields": {
            "request_id": getattr(g, "request_id", "-"),
            "method": request.method,
            "route": request.path,
            "status": resp.status_code,
            "elapsed_ms": elapsed_ms,
        }})
        return resp

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():
        if client.info(timeout=5.0) is None:
            return error_response(503, "not_ready", "cdh_unreachable")
        return jsonify({"status": "ready"})

    @app.get("/api/v1/status")
    def status():
        info = client.info(timeout=3.0)
        return jsonify({
            "app": APP_NAME,
            "version": APP_VERSION,
            "hostname": socket.gethostname(),
            "tdx_guest_flag": tdx_guest_flag(),
            "cdh_reachable": info is not None,
            "cdh_tee": (info or {}).get("tee"),
            "resource_path": cfg.kbs_resource_path,
            "secret_available": cache.available(),  # true only while a secret is cached
        })

    @app.post("/api/v1/compute")
    def compute():
        started = time.perf_counter()
        body = request.get_json(silent=True)
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str):
            return error_response(400, "invalid_input", "body must be JSON with a string field 'message'")
        if not 1 <= len(message) <= MAX_MESSAGE_CHARS:
            return error_response(400, "invalid_input", f"message must be 1 to {MAX_MESSAGE_CHARS} characters")

        try:
            secret = cache.get()
        except CdhError as exc:
            # Log the failure kind and the upstream status code only.
            log.warning("secret fetch failed", extra={"fields": {
                "request_id": g.request_id, "kind": exc.kind, "upstream_status": exc.upstream_status}})
            if exc.kind in ("cdh_unreachable", "cdh_timeout"):
                return error_response(503, "cdh_unreachable", exc.kind)
            detail = f"cdh_status_{exc.upstream_status}" if exc.kind == "cdh_status" else exc.kind
            return error_response(502, "secret_unavailable", detail)

        digest = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
        return jsonify({
            "request_id": g.request_id,
            "result": digest,
            "algorithm": "HMAC-SHA256",
            "resource": cfg.kbs_resource_path,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        })

    @app.errorhandler(404)
    def not_found(_exc):
        return error_response(404, "not_found", "no such route")

    @app.errorhandler(405)
    def method_not_allowed(_exc):
        return error_response(405, "method_not_allowed", "method not allowed")

    @app.errorhandler(413)
    def too_large(_exc):
        return error_response(413, "payload_too_large", "request body too large")

    @app.errorhandler(Exception)
    def unexpected(exc):
        if isinstance(exc, HTTPException):  # 400, 415, ... keep their own status
            return error_response(exc.code or 500, (exc.name or "error").lower().replace(" ", "_"), exc.description or "")
        log.error("unhandled error", extra={"fields": {
            "request_id": getattr(g, "request_id", "-"), "type": type(exc).__name__}})
        return error_response(500, "internal_error", "unexpected error")

    return app


# gunicorn entry point: gunicorn -b 0.0.0.0:8080 --workers 1 --threads 8 backend:app
configure_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
app = create_app()

if __name__ == "__main__":
    run_cfg = Config.from_env()
    log.info("starting", extra={"fields": {"port": run_cfg.port, "resource": run_cfg.kbs_resource_path,
                                           "ttl": run_cfg.secret_ttl_seconds}})
    app.run(host="0.0.0.0", port=run_cfg.port, threaded=True)
