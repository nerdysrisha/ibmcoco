# =============================================================================
# CoCo POC commands (copy and paste one at a time, not meant to run as a script)
# Chain: frontend (8081) -> confapp (8080) -> cdhmock (8006) -> kbs-client -> Trustee (8090)
# =============================================================================


# ----------------------------- ON THE LAPTOP (one time) ----------------------

# copy kbs-client next to docker-compose.yml and make it runnable
cp ~/kbs-client-dl/kbs-client <COMPOSE_DIR>/kbs-client
chmod +x <COMPOSE_DIR>/kbs-client

# commit and push (kbs-client, cdhmock.py, confapp.py, frontend.py, templates/index.html, this file)
git add -A && git commit -m "Trustee POC" && git push


# ----------------------------- IN ENV2 ---------------------------------------
# <COMPOSE_DIR> = folder with docker-compose.yml, kbs/ and kbs-client
# Run the proxy line below in EVERY terminal

# skip the proxy for local calls
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost

# get the latest files from GitHub
git pull

# make kbs-client runnable
chmod +x <COMPOSE_DIR>/kbs-client


# ----------------------------- STEP 1: START TRUSTEE -------------------------

# go to the compose folder
cd <COMPOSE_DIR>

# start Trustee (kbs, as, rvps should be running; setup exits, that is normal)
docker compose up -d && sleep 10 && docker compose ps

# check KBS is up (expect HTTP 401)
curl --noproxy '*' -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST http://127.0.0.1:8090/kbs/v0/attest


# ----------------------------- STEP 2: LOAD SECRET + POLICIES ----------------
# stay in <COMPOSE_DIR>, kbs-client finds kbs/config/docker-compose/admin-token from here

# create the secret file (with a trailing newline)
printf 'some-test-secret\n' > /tmp/key1

# store the secret in Trustee (expect: Set resource success)
./kbs-client --url http://127.0.0.1:8090 config set-resource --path default/kbsres1/key1 --resource-file /tmp/key1

# resource policy: allow all (test mode)
printf 'package policy\ndefault allow = true\n' > /tmp/res-policy.rego
./kbs-client --url http://127.0.0.1:8090 config set-resource-policy --policy-file /tmp/res-policy.rego

# attestation policy: accept the sample (fake) evidence (test mode)
printf 'package policy\nimport rego.v1\ndefault allow := true\n' > /tmp/att-policy.rego
./kbs-client --url http://127.0.0.1:8090 config set-attestation-policy --policy-file /tmp/att-policy.rego

# read the secret back through a full sample attestation (expect: some-test-secret)
./kbs-client --url http://127.0.0.1:8090 get-resource --path default/kbsres1/key1 2>/dev/null | base64 -d


# ----------------------------- STEP 3: CDHMOCK (terminal T2) -----------------

# go to the cdhmock folder
cd <APPS_DIR>/cdhmock

# start it (expect: Running on http://127.0.0.1:8006), leave it running
TRUSTEE_URL=http://127.0.0.1:8090 KBS_CLIENT=<COMPOSE_DIR>/kbs-client MOCK_MODE=ok python3 cdhmock.py

# check from a spare terminal (expect: some-test-secret)
curl --noproxy '*' -s http://127.0.0.1:8006/cdh/resource/default/kbsres1/key1


# ----------------------------- STEP 4: CONFAPP (terminal T1) -----------------

# go to the confapp folder
cd <APPS_DIR>/confapp

# start it (expect a "starting" line on port 8080), leave it running
python3 confapp.py

# encrypt "Hello" (copy the "result" text from the answer)
curl --noproxy '*' -s -X POST http://127.0.0.1:8080/api/v1/encrypt -H 'Content-Type: application/json' -d '{"message":"Hello"}'

# decrypt it back (replace PASTE_HERE with that result; expect: Hello)
curl --noproxy '*' -s -X POST http://127.0.0.1:8080/api/v1/decrypt -H 'Content-Type: application/json' -d '{"ciphertext":"PASTE_HERE"}'

# old one-way HMAC check (still works)
curl --noproxy '*' -s -X POST http://127.0.0.1:8080/api/v1/compute -H 'Content-Type: application/json' -d '{"message":"hello"}'


# ----------------------------- STEP 5: FRONTEND (terminal T3) ----------------

# go to the frontend folder
cd <APPS_DIR>/frontend

# start it on port 8081, leave it running
PORT=8081 APP1_BASE_URL=http://127.0.0.1:8080 python3 frontend.py

# open the page: VS Code Ports tab -> add port 8081 -> open the URL (ends with /http/8081/)


# ----------------------------- STOP ------------------------------------------

# Ctrl+C in the cdhmock, confapp and frontend terminals, then stop Trustee
cd <COMPOSE_DIR>
docker compose down

# the secret stays on disk in <COMPOSE_DIR>/kbs/data, do not delete that folder
# after a restart: redo STEP 1 and STEP 2 (safe to repeat)


# ----------------------------- IF SOMETHING FAILS ----------------------------

# python3: command not found       -> sudo apt install -y python3-pip python3-flask
# kbs-client: Permission denied    -> chmod +x <COMPOSE_DIR>/kbs-client
# STEP 2 auth errors               -> not in <COMPOSE_DIR>, or admin-token missing (redo STEP 1)
# cdhmock says "trustee error"     -> wrong kbs-client path, or Trustee not running
# secret shows "dummy-test-secret" -> old cdhmock.py, or TRUSTEE_URL not set
# frontend page has no styling     -> frontend.py CSP line needs style-src 'self' 'unsafe-inline'


# ----------------------------- NOTES -----------------------------------------

# kbs-client is the sample_only build for Trustee commit 9dd128c83ad94e1e37db706b8db8881e56445d1e (fake evidence, POC only)
# before building the frontend Docker image undo the IDE-only workarounds:
#   index.html: action="encrypt" -> action="/encrypt", action="decrypt" -> action="/decrypt"
#   frontend.py: delete the /http/8081 redirect route
# in the real CVM: real CDH + AA replace cdhmock + kbs-client, and a real TDX verifier replaces the sample evidence
# do not ship cdhmock.py or kbs-client in production images
