# CoCo POC: Trustee + CDH mock + confapp (step by step)

Chain being proven:

`confapp -> cdhmock (T2) -> kbs-client -> Trustee (KBS/AS/RVPS, sample attestation) -> secret`

Terminals: **T1** confapp, **T2** cdhmock, **T3** curl/test. Trustee runs in Docker (no terminal needed).

`<COMPOSE_DIR>` = the folder holding `docker-compose.yml` and `kbs/`
(laptop: `.../applications/custom-docker-compose`, env2: `.../applications/trustee-custom/custom-docker-compose`).

---

## Part A. On the laptop (push the missing pieces)

1. Copy the `kbs-client` file into the repo and make it runnable:
   ```
   cp ~/kbs-client-dl/kbs-client <COMPOSE_DIR>/kbs-client
   chmod +x <COMPOSE_DIR>/kbs-client
   ```

2. Commit and push (`kbs-client`, the edited `cdhmock.py`, and this MD file):
   ```
   git add kbs-client cdhmock.py TRUSTEE-POC-STEPS.md && git commit -m "Trustee POC: kbs-client, cdhmock via Trustee, steps" && git push
   ```
   Adjust the paths if you run it from a different folder (`cdhmock.py` lives in `cdhmock/`).

---

## Part B. In env2

Proxy workaround, in EVERY terminal:
```
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
```

### 1. Pull the repo and make kbs-client runnable
```
git pull
chmod +x <COMPOSE_DIR>/kbs-client
```

### 2. Start Trustee (from `<COMPOSE_DIR>`)
```
cd <COMPOSE_DIR>
docker compose up -d && sleep 10 && docker compose ps
```
- Expect kbs, as, rvps running. `setup` exits (normal).
- If they are already running, skip this.

### 3. Load the secret (from `<COMPOSE_DIR>`)
Stay in `<COMPOSE_DIR>`: the client finds `kbs/config/docker-compose/admin-token` relative to the current folder.
```
printf 'some-test-secret\n' > /tmp/key1
./kbs-client --url http://127.0.0.1:8090 config set-resource --path default/kbsres1/key1 --resource-file /tmp/key1
```
- Expect: `Set resource success`.

### 4. Set the two policies (allow all, test mode)
```
printf 'package policy\ndefault allow = true\n' > /tmp/res-policy.rego
./kbs-client --url http://127.0.0.1:8090 config set-resource-policy --policy-file /tmp/res-policy.rego
```
```
printf 'package policy\nimport rego.v1\ndefault allow := true\n' > /tmp/att-policy.rego
./kbs-client --url http://127.0.0.1:8090 config set-attestation-policy --policy-file /tmp/att-policy.rego
```
- Expect: `Set resource policy success` and `Set attestation policy success`.
- The "no policy_id set" warning is harmless.

### 5. Test fetching the secret with sample attestation
```
./kbs-client --url http://127.0.0.1:8090 get-resource --path default/kbsres1/key1 2>/dev/null | base64 -d
```
- Expect: `some-test-secret`.

### 6. Start cdhmock (T2, from the `cdhmock` folder)
```
TRUSTEE_URL=http://127.0.0.1:8090 KBS_CLIENT=<COMPOSE_DIR>/kbs-client MOCK_MODE=ok python3 cdhmock.py
```
- Expect: `Running on http://127.0.0.1:8006`. Leave it running.

Check (T3):
```
curl --noproxy '*' -s http://127.0.0.1:8006/cdh/resource/default/kbsres1/key1
```
- Expect: `some-test-secret`.

### 7. Start confapp (T1, from the `confapp` folder)
```
python3 confapp.py
```
- Expect: a "starting" log line on port 8080. Leave it running.

### 8. Final check (T3)
```
curl --noproxy '*' -s -X POST http://127.0.0.1:8080/api/v1/compute -H 'Content-Type: application/json' -d '{"message":"hello"}'
```
- Expect: JSON with `"algorithm":"HMAC-SHA256"` and a hex `result`.
- On the laptop, `hello` gave `efcd9787e3a1dcee58ed9b4e94a4e09f589ff84ad19dfefc315348ffba01dcbb`. The same result in env2 confirms the same secret.

---

## If something fails

| Symptom | Likely cause |
|---|---|
| `python: not found` | use `python3` |
| `No module named flask` | `sudo apt install -y python3-pip python3-flask` (or the mirror's equivalent) |
| `kbs-client: Permission denied` | `chmod +x kbs-client` |
| `config` commands give auth errors | not in `<COMPOSE_DIR>`, or `admin-token` missing (run `docker compose up -d` so `setup` creates it) |
| cdhmock returns `trustee error` (502) | wrong `KBS_CLIENT` path, or Trustee not running (`docker compose ps`) |
| Trustee containers gone after a restart | run step 2 again, then repeat steps 3 and 4 |

## Notes

- `kbs-client` is the `sample_only` build matching Trustee commit `9dd128c83ad94e1e37db706b8db8881e56445d1e`. It fakes TEE evidence, so it is for this POC only.
- Frontend was skipped: it only calls confapp, which is unchanged.
- Later: undo the frontend IDE-only workarounds before Dockerizing, then in the real CVM replace cdhmock + kbs-client with the real CDH/AA and a real TDX verifier.
- Do not ship `cdhmock.py` or `kbs-client` in the production images.
