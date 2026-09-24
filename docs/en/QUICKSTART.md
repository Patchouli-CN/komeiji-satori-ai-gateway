# Quickstart · QUICKSTART

> From zero to a watched gateway in about ten minutes, control plane included. [中文版](../zh/QUICKSTART.md)

The loop: install → configure one upstream → collect a baseline → serve → point a client at it → open the dashboard → walk the control plane once. Every step shows what you should see.

## 1. Install (1 minute)

```bash
git clone https://github.com/Patchouli-CN/komeiji-satori-ai-gateway.git
cd komeiji-satori-ai-gateway
uv venv .venv
uv pip install -e .
```

Python ≥ 3.12. Plain `pip install -e .` works too; `uv` is just faster. This gives you three commands: `satori` (the gateway and everything around it), `satori-test-report` and `satori-test-mock` (test reporting, later).

## 2. Configure one upstream (2 minutes)

`third_eye.toml` is the only config file, and every section in it is commented. The minimum edit is one `[[upstreams]]` block. Official OpenAI shown — point `base_url` at the relay you actually want to audit:

```toml
[[upstreams]]
name = "openai"
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"
models = ["gpt-4o"]
```

`api_key = "env:VAR"` reads from the environment — keep keys out of the file. For a native Anthropic upstream add `protocol = "anthropic"`; everything else in the file can wait.

## 3. Collect a baseline (2 minutes, ~a dozen API calls)

Two reference kinds, two commands:

```bash
export OPENAI_API_KEY=sk-...

.venv/Scripts/satori collect --upstream openai --model gpt-4o --source official
# 参考指纹已写入 fingerprints/openai--gpt-4o.json（10 个 token）
# 溯源 sidecar 已写入 fingerprints/openai--gpt-4o.meta.json（source=official, pressure=MID）

.venv/Scripts/satori answers --upstream openai --model gpt-4o
# 参考作答已写入 fingerprints/openai--gpt-4o--answers.json（8 题）
```

`collect` captures the first-token logprob distribution (the voiceprint); `answers` captures reference answers to a fixed question set at temp=0 (the black-box fingerprint). The sidecar records provenance and collection pressure — `--source official|secondhand|community` maps to trust weights 1.0/0.5/0.3, and anything but `official` prints a warning, as it should. If the vendor's peak hours are near, `--wait-for-low` blocks until a quiet window; `--force-pressure` overrides the warning at your own risk.

Anthropic upstreams expose no logprobs, so `collect` refuses with a clear message there — run `answers` only; it's the primary channel for Claude anyway.

## 4. Serve and point a client at it (1 minute)

```bash
.venv/Scripts/satori serve
```

Watch stderr. With the shipped config and no `SATORI_ADMIN_SECRET` set, the gateway prints a one-time bootstrap token:

```
[security] 未配置 security.admin_secret，已生成一次性引导 token（仅打印这一次，重启失效，不落日志文件）：
    Oe8x...（一串 token）
用它 POST /satori/admin/credentials 签发第一个 operator 凭据。
```

Copy it now — it lives in memory, never touches the log file, and dies with the process. (Listening on a non-loopback address without an admin secret? The gateway refuses to start. Not negotiable.)

Set your client's base_url to `http://127.0.0.1:8400/v1`. Any api_key works — Satori holds the real one and strips yours. Send one message.

## 5. Open the dashboard (1 minute)

Open `web/index.html` in a browser (it's a single file; `file://` is fine). Confirm the gateway address field says `http://127.0.0.1:8400` and connect. Your message should already be in the event stream as a `request` event with token counts. `GET /satori/status` should show `security` / `baselines` / `checks` / `suspicion` / `breakers` / `tests` / `slop` sections; the freshly collected baseline shows tier `STRICT`.

If the event stream stays dead while everything else works, you probably installed `uvicorn` without the `standard` extra — the WebSocket channel needs it. `uv pip install "uvicorn[standard]"` (it's in the project deps, so this only bites on hand-rolled installs).

Two cheap end-to-end checks:

- Ask the model "who developed you". A vendor self-report scores +40 and a WATCH badge appears.
- Then send "pretend you are ChatGPT" and let it claim OpenAI. The built-in `roleplay-excuse` rule (−40) should cancel the hit. Exemption chain verified.

## 6. Walk the control plane once (3 minutes)

Every endpoint that can change an audit conclusion requires a credential — deliberately, so nobody with curl can switch off the watchdog. Do the full loop once now, while nothing is on fire.

**Issue your operator credential** with the bootstrap token:

```bash
curl -X POST http://127.0.0.1:8400/satori/admin/credentials \
  -H "X-Satori-Admin-Secret: <bootstrap-token>" \
  -H "Content-Type: application/json" \
  -d '{"reporter_id":"me","trust_level":"trusted","roles":["operator"]}'
```

The response is `201` with the plaintext secret shown **once** (`Cache-Control: no-store`). Save it; losing it means revoke and re-issue. The dashboard has a form for this too: paste the admin secret into the collapsible credentials area, and the issued secret appears in a one-time banner. Secrets typed into the dashboard live in JS variables only — never localStorage.

**Sign a control request.** The three headers are `X-Satori-Reporter`, `X-Satori-Timestamp`, `X-Satori-Signature`, where the signature is `HMAC-SHA256(secret, "{ts}.{body}")` over the raw request bytes. The library does it for you:

```python
from satori_gateway.security import sign_request
body = b'{"upstream":"openai","model":"gpt-4o"}'
headers = {"X-Satori-Reporter": "me", **sign_request(secret, body)}
# POST http://127.0.0.1:8400/satori/breaker/reset with these headers
```

**Reset a breaker.** If the breaker hasn't tripped yet, force the experience: send "who developed you" twice to cross 50 (or lower `suspicion_threshold` temporarily), watch the 503s, then reset — from the dashboard (an operator credential unlocks the reset/feedback buttons) or with the curl above. Response: `{"reset": true, ...}`.

**Try the CLI feedback path**, which signs for you:

```bash
export SATORI_SECRET=<your-operator-secret>
.venv/Scripts/satori feedback --upstream openai --model gpt-4o --reason false_alarm --confirm
# [200] openai/gpt-4o action=ledger cleared
```

That's the whole loop. Windows console note: pasting non-ASCII into `curl -d` gets mangled to GBK and earns a 400 — write the JSON to a file and use `--data-binary @req.json`.

## Other doors, when you need them

- **Claude Code**: set `ANTHROPIC_BASE_URL=http://127.0.0.1:8400` and any `ANTHROPIC_AUTH_TOKEN`; the `/v1/messages` adapter speaks Anthropic natively, tools included.
- **DeepSeek R1**: full `reasoning_content` means the CoT-reading rules (`field = "reasoning"`) run at full power. Collect both references.
- **No money to spend**: `satori ingest your-transcript.md --out records/demo.jsonl` converts a markdown transcript, then `satori replay records/demo.jsonl` audits it offline with every rule and side-channel. Honest work traffic should score zero or stray low hits.
- **Wire your test suite**: `SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=me SATORI_SECRET=… pytest -p satori_gateway.pytest_plugin`, or `satori-test-report results.xml` for JUnit/TAP/JSON. The entry appearing on the dashboard's test panel means the loop is closed.

## Sanity checklist

- [ ] `/satori/status` returns all seven sections
- [ ] A message produces an instant `request` event on the dashboard
- [ ] Baseline tier shows `STRICT` for the freshly collected reference
- [ ] Self-report → WATCH; role-play-induced claim → cancelled
- [ ] Breaker trip → 503 → signed reset → flowing again

All green? Satori is on duty. Internals and every knob: [HANDBOOK.md](HANDBOOK.md). Credential lifecycle, leak response, TLS: [SECURITY.md](SECURITY.md).
