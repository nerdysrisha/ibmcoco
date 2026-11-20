# CoCo POC apps (Flask)

Two small Flask apps for the Confidential Containers POC on IBM Cloud.

| App | File | Runs as | Port | Purpose |
|---|---|---|---|---|
| App 1 | `app1/backend.py` | Kata peer pod (TDX CVM) | 8080 | Gets the KBS secret from CDH (`127.0.0.1:8006`) and exposes a small API |
| App 2 | `app2/front.py` | Normal worker pod | 8080 | Web page. Calls App 1 and shows the result |

Both use only `Flask==3.0.3` at runtime. The HTTP calls (to CDH and to App 1) use the Python standard library, so there is no extra package to approve in Artifactory. Python 3.11 syntax is used.

## Layout

```
app1/  backend.py  mock_cdh.py (dev only)  requirements.txt  requirements-dev.txt  Dockerfile  .dockerignore  tests/
app2/  front.py  templates/index.html  static/style.css  requirements.txt  requirements-dev.txt  Dockerfile  .dockerignore  tests/
```

`mock_cdh.py`, `tests/` and the dev requirements are excluded from the images by `.dockerignore`.

## App 1 API (`backend.py`)

| Route | Result |
|---|---|
| `GET /healthz` | `200 {"status":"ok"}` (no CDH call) |
| `GET /readyz` | `200` if CDH answers `GET /info`, else `503` |
| `GET /api/v1/status` | `app, version, hostname, tdx_guest_flag, cdh_reachable, cdh_tee, resource_path, secret_available` |
| `POST /api/v1/compute` | Body `{"message":"<1..1024 chars>"}`. Returns `{request_id, result, algorithm, resource, elapsed_ms}`. `result` is the hex HMAC-SHA256 of the message, keyed with the secret bytes exactly as CDH returned them (including any trailing newline) |

Errors: `400 invalid_input`, `413`, `502 secret_unavailable` (CDH returned 403/404/other, or an empty secret), `503 cdh_unreachable` (CDH not reachable or timed out). The secret, upstream response bodies and request bodies are never returned or logged.

Environment variables (defaults in brackets): `PORT` [8080], `CDH_BASE_URL` [http://127.0.0.1:8006], `KBS_RESOURCE_PATH` [default/kbsres1/key1], `SECRET_TTL_SECONDS` [300; 0 = fetch on every request], `CDH_TIMEOUT_SECONDS` [45], `CDH_RETRIES` [2, connection errors only], `LOG_LEVEL` [INFO].

`tdx_guest_flag` (the `tdx_guest` CPU flag) and `cdh_tee` (from CDH's `/info`) are informational. They are hints, not attestation.

## App 2 (`front.py`)

`GET /` shows the App 1 status panel and a form. `POST /run` calls App 1 and renders the result or a clear failure message. `GET /healthz` does not call App 1.

Failure messages: App 1 not responding; access denied by the service mesh policy (403 `RBAC: access denied`); App 1 unavailable (503 from the mesh); App 1 cannot reach its CDH; App 1 could not obtain its secret (502 `secret_unavailable`); input rejected (400). Each shows the HTTP status and a request ID.

It forwards `x-request-id`, `x-b3-*`, `b3`, `traceparent` and `tracestate` to App 1. It has CSRF protection (double-submit cookie), `Content-Security-Policy: default-src 'self'`, no inline script or style, and Jinja autoescaping. It logs request IDs and status only, never user input or results.

Environment variables: `PORT` [8080], `APP1_BASE_URL` [http://app1:8080], `APP1_TIMEOUT_SECONDS` [60], `APP1_STATUS_TIMEOUT_SECONDS` [5], `LOG_LEVEL` [INFO].

## Run the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install Flask==3.0.3 -r app1/requirements-dev.txt
(cd app1 && python -m pytest -q tests)     # 26 tests
(cd app2 && python -m pytest -q tests)     # 28 tests
```

## Run everything locally (three terminals, no Docker)

```bash
# 1. mock CDH on 127.0.0.1:8006 (dev only)
cd app1 && MOCK_SECRET=some-test-secret python mock_cdh.py
# 2. App 1
cd app1 && python backend.py
# 3. App 2 on 8081, calling App 1 on 8080
cd app2 && PORT=8081 APP1_BASE_URL=http://127.0.0.1:8080 python front.py
```

Open http://localhost:8081/. The mock secret is `some-test-secret` plus a newline. To check a result:

```bash
python3 -c "import hmac,hashlib;print(hmac.new(b'some-test-secret\n',b'hello',hashlib.sha256).hexdigest())"
```

`MOCK_MODE=forbidden|notfound|slow` (with `MOCK_DELAY`) simulates CDH failures.

## Build and push

Put your real base image tag in `BASE_IMAGE` (the Dockerfiles carry your placeholder). `HTTP_PROXY` is used for `pip install --proxy`, as in your current Dockerfile.

```bash
ibmcloud cr login
docker build --platform linux/amd64 \
  --build-arg BASE_IMAGE=base-images-catalog.artifactory-bringer.group.bringernet/ibm9/python-311:<real-tag> \
  --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  -t fr2.icr.io/env-conf-container-cr/app1-cvm-api:v1 app1/
docker push fr2.icr.io/env-conf-container-cr/app1-cvm-api:v1

docker build --platform linux/amd64 \
  --build-arg BASE_IMAGE=base-images-catalog.artifactory-bringer.group.bringernet/ibm9/python-311:<real-tag> \
  --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  -t fr2.icr.io/env-conf-container-cr/app2-web-ui:v1 app2/
docker push fr2.icr.io/env-conf-container-cr/app2-web-ui:v1

ibmcloud cr image-digests --restrict env-conf-container-cr
```

If your package index needs a custom URL, add `--build-arg PIP_INDEX_URL=...` (and `PIP_TRUSTED_HOST` if needed).

## Notes

- The apps use Flask's built-in server, like your `front.py` hello world. That is fine for the POC. For anything longer-lived, add `gunicorn` to `requirements.txt` and use the commented `CMD` line in each Dockerfile. App 1 must keep `--workers 1`, so the secret cache stays single and one attestation covers a burst of requests.
- Tested with Python 3.12.3 on Flask 3.0.3 (Werkzeug 3.1.9, Jinja2 3.1.6, itsdangerous 2.2.0, click 8.5.1, blinker 1.9.0, MarkupSafe 3.0.4). The code is checked to parse as Python 3.11 syntax but was not run on 3.11. Run the tests once on your 3.11 base image.
- Flask 3.0.3 does not pin its dependencies tightly. For reproducible builds, pin the versions above in `requirements.txt` if your Artifactory carries them.
- Under OpenShift's `restricted` SCC the pod runs with a UID chosen by the platform. The files are readable by any UID and the apps write nothing to disk.
