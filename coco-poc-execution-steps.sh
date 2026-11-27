#!/usr/bin/env bash
# =============================================================================
# CoCo POC runbook: Trustee + CDH mock + confapp + frontend
#
# What it proves:
#   browser -> frontend (8081) -> confapp (8080) -> cdhmock (8006)
#           -> kbs-client -> Trustee (KBS 8090 / AS / RVPS, sample attestation)
#
# Usage (run one step at a time, in this order):
#   ./coco-poc.sh trustee    # start Trustee in Docker and check it answers
#   ./coco-poc.sh load       # load the secret + the 2 allow-all policies, then verify
#   ./coco-poc.sh cdhmock    # terminal T2 (stays running)
#   ./coco-poc.sh confapp    # terminal T1 (stays running)
#   ./coco-poc.sh frontend   # terminal T3 (stays running)
#   ./coco-poc.sh test       # quick curl check: encrypt, decrypt, HMAC
#   ./coco-poc.sh stop       # stop Trustee containers (Ctrl+C the 3 apps yourself)
# =============================================================================

# -----------------------------------------------------------------------------
# PART A: ON THE LAPTOP (one time, push what env2 is missing)
#   1. Copy the kbs-client file next to docker-compose.yml and make it runnable:
#        cp ~/kbs-client-dl/kbs-client <COMPOSE_DIR>/kbs-client
#        chmod +x <COMPOSE_DIR>/kbs-client
#   2. Commit and push: kbs-client, cdhmock.py, confapp.py, frontend.py,
#      templates/index.html and this script.
#        git add -A && git commit -m "Trustee POC" && git push
#      (never add kbs/data or the generated keys/tokens: they are root-owned
#       files created by Docker; the repo's .gitignore already covers them)
#   3. In env2: git pull
#
# PART B: IN ENV2, edit the 3 paths below once, then run the steps above.
# -----------------------------------------------------------------------------

# --- settings: change these to match env2 ------------------------------------
# folder with docker-compose.yml, kbs/ and kbs-client
COMPOSE_DIR="${COMPOSE_DIR:-$HOME/wssri/ibmcoco/gitlab-apps/applications/trustee-custom/custom-docker-compose}"
# folder that holds cdhmock/, confapp/ and frontend/
APPS_DIR="${APPS_DIR:-$HOME/wssri/IBMCoCo/gitlab-apps/applications/poc2}"
PYTHON="${PYTHON:-python3}"

# --- fixed values (normally no change needed) --------------------------------
TRUSTEE_URL="http://127.0.0.1:8090"
KBS_CLIENT="$COMPOSE_DIR/kbs-client"
SECRET_PATH="default/kbsres1/key1"
CONFAPP_URL="http://127.0.0.1:8080"

# The sandbox proxy would block local calls, so skip it for localhost
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

die() { echo "ERROR: $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# trustee: start KBS, AS and RVPS and check KBS answers
# -----------------------------------------------------------------------------
cmd_trustee() {
  cd "$COMPOSE_DIR" || die "COMPOSE_DIR not found: $COMPOSE_DIR"
  docker compose up -d && sleep 10 && docker compose ps
  # kbs, as and rvps should be running ("setup" exits, that is normal)
  echo
  echo "KBS check (expect 401, that means KBS is up):"
  curl --noproxy '*' -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$TRUSTEE_URL/kbs/v0/attest"
}

# -----------------------------------------------------------------------------
# load: put the secret and the policies into Trustee, then read it back
# Must run from COMPOSE_DIR: kbs-client finds kbs/config/docker-compose/admin-token there
# -----------------------------------------------------------------------------
cmd_load() {
  cd "$COMPOSE_DIR" || die "COMPOSE_DIR not found: $COMPOSE_DIR"
  [ -x "$KBS_CLIENT" ] || die "kbs-client missing or not executable: $KBS_CLIENT (run: chmod +x $KBS_CLIENT)"

  # the secret, with a trailing newline (same as the old cdhmock)
  printf 'some-test-secret\n' > /tmp/key1
  "$KBS_CLIENT" --url "$TRUSTEE_URL" config set-resource --path "$SECRET_PATH" --resource-file /tmp/key1

  # resource policy: allow everything (test mode)
  printf 'package policy\ndefault allow = true\n' > /tmp/res-policy.rego
  "$KBS_CLIENT" --url "$TRUSTEE_URL" config set-resource-policy --policy-file /tmp/res-policy.rego

  # attestation policy: accept the sample (fake) evidence (test mode)
  printf 'package policy\nimport rego.v1\ndefault allow := true\n' > /tmp/att-policy.rego
  "$KBS_CLIENT" --url "$TRUSTEE_URL" config set-attestation-policy --policy-file /tmp/att-policy.rego

  # read it back through a full sample attestation (expect: some-test-secret)
  echo
  echo "Read back (expect: some-test-secret):"
  "$KBS_CLIENT" --url "$TRUSTEE_URL" get-resource --path "$SECRET_PATH" 2>/dev/null | base64 -d
}

# -----------------------------------------------------------------------------
# cdhmock (T2): the fake CDH on 127.0.0.1:8006
# TRUSTEE_URL makes it ask Trustee through kbs-client (without it, it serves a dummy secret)
# Expect: "Running on http://127.0.0.1:8006"
# -----------------------------------------------------------------------------
cmd_cdhmock() {
  cd "$APPS_DIR/cdhmock" || die "not found: $APPS_DIR/cdhmock"
  TRUSTEE_URL="$TRUSTEE_URL" KBS_CLIENT="$KBS_CLIENT" MOCK_MODE=ok exec "$PYTHON" cdhmock.py
}

# -----------------------------------------------------------------------------
# confapp (T1): the confidential app on port 8080
# Gets the secret from cdhmock; offers /api/v1/encrypt, /decrypt and /compute
# -----------------------------------------------------------------------------
cmd_confapp() {
  cd "$APPS_DIR/confapp" || die "not found: $APPS_DIR/confapp"
  exec "$PYTHON" confapp.py
}

# -----------------------------------------------------------------------------
# frontend (T3): the web page on port 8081
# Open it from the VS Code Ports tab (add port 8081); the URL ends with /http/8081/
# -----------------------------------------------------------------------------
cmd_frontend() {
  cd "$APPS_DIR/frontend" || die "not found: $APPS_DIR/frontend"
  PORT=8081 APP1_BASE_URL="$CONFAPP_URL" exec "$PYTHON" frontend.py
}

# -----------------------------------------------------------------------------
# test: quick curl check of the whole chain (run in a spare terminal)
# -----------------------------------------------------------------------------
cmd_test() {
  echo "1) encrypt 'Hello':"
  enc=$(curl --noproxy '*' -s -X POST "$CONFAPP_URL/api/v1/encrypt" \
        -H 'Content-Type: application/json' -d '{"message":"Hello"}')
  echo "$enc"

  token=$(printf '%s' "$enc" | "$PYTHON" -c 'import sys,json; print(json.load(sys.stdin)["result"])') \
    || die "encrypt did not return a result"

  echo
  echo "2) decrypt it back (expect Hello):"
  curl --noproxy '*' -s -X POST "$CONFAPP_URL/api/v1/decrypt" \
       -H 'Content-Type: application/json' -d "{\"ciphertext\":\"$token\"}"

  echo
  echo
  echo "3) HMAC of 'hello' (one-way fingerprint, still available):"
  curl --noproxy '*' -s -X POST "$CONFAPP_URL/api/v1/compute" \
       -H 'Content-Type: application/json' -d '{"message":"hello"}'
  echo
}

# -----------------------------------------------------------------------------
# stop: stop Trustee. Stored secret and keys stay on disk in COMPOSE_DIR/kbs/data
# (do NOT delete that folder, or run "load" again afterwards)
# -----------------------------------------------------------------------------
cmd_stop() {
  cd "$COMPOSE_DIR" || die "COMPOSE_DIR not found: $COMPOSE_DIR"
  docker compose down
}

case "${1:-}" in
  trustee)  cmd_trustee ;;
  load)     cmd_load ;;
  cdhmock)  cmd_cdhmock ;;
  confapp)  cmd_confapp ;;
  frontend) cmd_frontend ;;
  test)     cmd_test ;;
  stop)     cmd_stop ;;
  *) sed -n '2,17p' "$0" ;;   # no or unknown step: show the usage block at the top
esac

# =============================================================================
# TROUBLESHOOTING
#   python3: command not found     -> sudo apt install -y python3-pip python3-flask
#   kbs-client: Permission denied  -> chmod +x <COMPOSE_DIR>/kbs-client
#   "load" gives auth errors       -> kbs/config/docker-compose/admin-token missing:
#                                     run "trustee" first so the setup container creates it
#   cdhmock gives "trustee error"  -> wrong KBS_CLIENT path, or Trustee not running
#   secret is "dummy-test-secret"  -> cdhmock.py is the old copy, or TRUSTEE_URL not set
#   frontend page has no styling   -> frontend.py needs style-src 'unsafe-inline' in its CSP line
#   after a restart                -> run "trustee" then "load" again (safe to repeat)
#
# NOTES
#   - kbs-client is the sample_only build for Trustee commit
#     9dd128c83ad94e1e37db706b8db8881e56445d1e. It fakes TEE evidence: POC only.
#   - IDE-only workarounds in the frontend, undo BEFORE building its Docker image:
#       templates/index.html: action="encrypt" -> action="/encrypt"
#                             action="decrypt" -> action="/decrypt"
#       frontend.py: delete the /http/8081 redirect route
#   - Real CVM later: the real CDH + AA replace cdhmock + kbs-client, and a real
#     TDX verifier replaces the sample evidence.
#   - Do not ship cdhmock.py or kbs-client in the production images.
# =============================================================================
