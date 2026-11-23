"""DEV ONLY: a tiny mock of the CoCo REST API server (CDH) for local testing.

Run:   MOCK_SECRET=some-test-secret MOCK_MODE=ok python mock_cdh.py
Modes: ok | forbidden (403) | notfound (404) | slow (sleeps MOCK_DELAY seconds, then ok)

It listens on 127.0.0.1:8006 by default (what the real API server uses in the pod).
Do NOT ship this file in the production image (it is listed in .dockerignore).
"""
import os
import threading
import time

from flask import Flask, Response, jsonify


def create_mock(secret: bytes = b"dummy-test-secret\n", mode: str = "ok", delay: float = 0.0) -> Flask:
    app = Flask("mock_cdh")
    state = {"mode": mode, "secret": secret, "delay": delay, "resource_calls": 0, "info_calls": 0}
    lock = threading.Lock()
    app.extensions["mock_state"] = state

    @app.get("/info")
    def info():
        with lock:
            state["info_calls"] += 1
        return jsonify({"additional_tees": [], "feature": "resource", "tee": "mock", "version": "v0-mock"})

    @app.get("/cdh/resource/<repository>/<rtype>/<tag>")
    def resource(repository, rtype, tag):
        with lock:
            state["resource_calls"] += 1
            current = state["mode"]
        if current == "forbidden":
            return Response("forbidden by mock policy", status=403)
        if current == "notfound":
            return Response("not found", status=404)
        if current == "slow" or state["delay"]:
            time.sleep(state["delay"])
        return Response(state["secret"], mimetype="application/octet-stream")

    return app


if __name__ == "__main__":
    mock = create_mock(
        secret=os.environ.get("MOCK_SECRET", "dummy-test-secret").encode() + b"\n",
        mode=os.environ.get("MOCK_MODE", "ok"),
        delay=float(os.environ.get("MOCK_DELAY", "0")),
    )
    mock.run(host="127.0.0.1", port=int(os.environ.get("MOCK_PORT", "8006")), threaded=True)
