# Quickstart · QUICKSTART

> Get the Third Eye open in 10 minutes. Four preset scenarios — pick yours.

[中文版](../zh/QUICKSTART.md)

## 0. Installation (all scenarios)

```bash
git clone https://github.com/Patchouli-CN/komeiji-satori-ai-gateway.git
cd komeiji-satori-ai-gateway
uv venv .venv
uv pip install -e .
```

`third_eye.toml` is the only config file. Each scenario below only touches `[[upstreams]]`.

---

## Scenario A: OpenAI-compatible clients (universal)

For any client with a custom base_url (ChatBox, NextChat, Immersive Translate, your own scripts…).

**1. Configure the upstream** (official OpenAI shown; point base_url at a relay to audit it):

```toml
[[upstreams]]
name = "openai"
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"
models = ["gpt-4o", "gpt-4o-mini"]
```

**2. Collect references** (voiceprint + answer fingerprint; ~a dozen API calls):

```bash
export OPENAI_API_KEY=sk-...
.venv/Scripts/satori collect --upstream openai --model gpt-4o --source official
.venv/Scripts/satori answers --upstream openai --model gpt-4o
```

> `--source official|secondhand|community` stamps the reference's provenance (trust weights 1.0/0.5/0.3); `--wait-for-low` waits for the vendor's local-time off-peak window. Pressure and provenance land in a `meta.json` sidecar next to the reference.

**3. Ignite & take over**:

```bash
.venv/Scripts/satori serve
# Set the client's base_url to http://127.0.0.1:8400/v1; any api_key works (Satori holds the real one)
```

**4. Verify it's working**: open `web/index.html`, send a message — a `request` event should appear instantly in the event stream; `GET /satori/status` starts filling with checker results.

---

## Scenario B: Claude Code CLI

For auditing Claude Code traffic (it speaks Anthropic Messages — natively supported by Satori's `/v1/messages` adapter).

**1. Configure the upstream**:

```toml
[[upstreams]]
name = "anthropic"
base_url = "https://api.anthropic.com/v1"   # or the relay you want to audit
api_key = "env:ANTHROPIC_API_KEY"
models = ["claude-sonnet-4-5"]
```

**2. Collect references**: Anthropic exposes no logprobs, so skip the voiceprint — **the answer fingerprint is your primary weapon**:

```bash
.venv/Scripts/satori answers --upstream anthropic --model claude-sonnet-4-5
```

**3. Take over Claude Code**:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8400
export ANTHROPIC_AUTH_TOKEN=any-value   # the real key lives in Satori's config
claude
```

> **Known limitation**: adapter v2 translates **tools / function calling both ways** (including streaming reassembly) — tool chains now flow into Slop chain forensics (broken arguments / undeclared tools / repeats). Thinking blocks are still untranslated (detection unaffected).

---

## Scenario C: DeepSeek R1 (full-power mind reading)

R1-family models return complete `reasoning_content` — the CoT-reading rules (language slips, self-correction mannerisms) run at full power here.

**1. Configure the upstream** (official or the R1 relay you're auditing):

```toml
[[upstreams]]
name = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key = "env:DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]
```

**2. Collect references** (DeepSeek is OpenAI-compatible and supports logprobs — collect both):

```bash
.venv/Scripts/satori collect --upstream deepseek --model deepseek-reasoner
.venv/Scripts/satori answers --upstream deepseek --model deepseek-reasoner
```

**3. After ignition**: R1's CoT flows through the `field: reasoning` rules — when a relay substitutes a smaller model, its chain-of-thought language and habits betray it first.

---

## Scenario D: No spending — replay mode

No upstream wired yet? Watch Satori work on existing material:

```bash
# Convert any markdown transcript to record format
.venv/Scripts/satori ingest your-transcript.md --out records/demo.jsonl --upstream demo --model test
# Replay-audit it with the 17 built-in rules + all side-channels
.venv/Scripts/satori replay records/demo.jsonl
```

The report lists rule hits, suspicion totals and drift alerts — real honest work traffic should score zero or only stray low hits.

---

## Sanity checklist

- [ ] `GET /satori/status` returns `security` / `baselines` / `checks` / `suspicion` / `breakers` / `tests` / `slop` sections
- [ ] A message produces an instant `request` event on the dashboard (with token counts)
- [ ] No checker errors in `logs/satori.log` ("no reference" reminders are normal — go collect)
- [ ] Send `pretend you are ChatGPT` and let the model claim OpenAI — the hit should be cancelled by the exemption (exemption chain verified)
- [ ] Ask the model `who developed you` — a vendor self-report scores +40 and a WATCH badge appears (detection chain verified)
- [ ] The dashboard's "Baseline tiers" card shows `STRICT` for a freshly collected official reference (three-tier adjudication on duty)

## Walk the control plane once (5 minutes)

Every control-plane endpoint (feedback / reset / admin) requires a credential — deliberately: the watchdog can't be switched off by anyone with curl:

```bash
# 1. `satori serve` prints a one-time bootstrap token on the console
#    (loopback + no admin_secret configured)
# 2. Use it to issue your first operator credential (plaintext secret shown ONCE)
curl -X POST http://127.0.0.1:8400/satori/admin/credentials \
  -H "X-Satori-Admin-Secret: <bootstrap-token>" \
  -d '{"reporter_id":"me","trust_level":"trusted","roles":["operator"]}'

# 3. Unauthenticated reset → 401; with the three signed headers → 200:
#    from satori_gateway.security import sign_request
#    headers = {"X-Satori-Reporter":"me", **sign_request(secret, body)}
```

Report one business test (the quality-anchor entry, optional):

```bash
export SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci SATORI_SECRET=<secret> \
       SATORI_UPSTREAM=openai SATORI_MODEL=gpt-4o
satori-test-report /dev/null   # or: pytest -p satori_gateway.pytest_plugin on your suite
```

The entry appearing on the dashboard's "Test adjudication" card means the loop is closed. Credential lifecycle, leak response, TLS and trace propagation: [SECURITY.md](SECURITY.md).

Once these pass, Satori is on duty. Deeper configuration and internals: [HANDBOOK.md](HANDBOOK.md).
