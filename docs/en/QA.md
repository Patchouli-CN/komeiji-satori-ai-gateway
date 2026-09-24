# Q&A · Real questions

> Questions people actually ask, in roughly the order they ask them. Principles and mechanism details live in [HANDBOOK.md](HANDBOOK.md); credential operations in [SECURITY.md](SECURITY.md). [中文版](../zh/QA.md)

## Detection

### Q: Why does the fingerprint channel skip on my Anthropic upstream?

Because Anthropic's API has never exposed logprobs, and the voiceprint is a logprob distribution. No distribution, nothing to compare — the channel isn't broken, it's absent by design. `satori collect` will tell you the same thing and suggest the alternative: `satori answers`, the answer fingerprint, which is pure black-box (eight fixed questions at temp=0, similarity against your reference) and the primary weapon for Claude-family endpoints. The identity battery, rules, canary and latency profile all work too. If you specifically want a voiceprint for a Claude relay, collect one from an endpoint you trust and mark it `--source secondhand` — half trust weight, provenance recorded.

### Q: I'm in BASIC mode. What should I actually do?

First, don't panic — BASIC means the identity channels are deliberately silent, not that something failed. The startup log tells you which kind of BASIC it is: "No baseline collected" (you never collected references for that model — go run `collect`/`answers` from an endpoint you trust) or "Baseline expired … retired" (a human confirmed an official update; re-collect from the official endpoint to re-arm). While in BASIC, the black-box channels — rules, side-channels, hit-rate, canary, slop, tests — are all still on duty, and you stop paying for identity probes. It's a legitimate steady state for an upstream you only half-care about, but understand what you're flying without: equal-tier swaps become much harder to catch.

### Q: The TTL expired. Does the baseline auto-retire?

No, and that's a load-bearing decision. TTL consumption lights lamps — yellow at 70%, orange at 90%, red at 100% — and the red lamp suggests re-collecting or confirming an update. Nothing else happens. Retirement requires human ground truth through the feedback channel (`satori feedback --reason official_update --confirm`, or operator single-vote). The reasoning: a prediction model that can kill baselines turns the failure mode from "cried wolf" into "the real wolf arrived and we stayed silent". Lamps are cheap; silence is not.

### Q: Can flaky tests trip the breaker?

Structurally, no. Three layers stand between your flaky suite and the breaker: the sliding window needs ≥N fails within the last 3N runs before anything injects; a test whose lifetime failure rate exceeds 5% (with ≥20 samples) gets marked `unreliable` and is recorded but never adjudicated again; and only TRUSTED credentials can trip the breaker at all — NORMAL reports just add their level score and let the general threshold decide. If a genuinely flaky test still somehow fires, the breaker reset is one signed POST away. But if your L0 "deterministic" test is flaky, the test is the bug.

### Q: I suspect watering. In what order do I investigate?

1. **Look at the ledger first**: `GET /satori/status` — which pocket grew (quality or identity), and which rules fed it. Identity-pocket growth is the serious one; no test result can wash it.
2. **Read the hits** in `logs/satori.log` — each suspicion line names the rules and snippets.
3. **Replay the evidence**: `satori replay records/<date>.jsonl --tool-traces state/tool_traces.jsonl` re-audits recorded traffic with current rules and walks tool chains step by step.
4. **Check the baselines card**: tier, trust, TTL consumption. An aged STANDARD baseline crying mismatch is a weaker accusation than a fresh STRICT one.
5. **Decide the ground truth yourself**: send your own probe, compare against the vendor's official endpoint.
6. **Then act** — false alarm: `--reason false_alarm --confirm` clears the ledger. Real official update: `--reason official_update --confirm` retires the baseline gracefully. Real fraud: take the recordings to the refund dispute and switch upstreams.

Don't skip to step 6. The ledger accuses; only you convict.

### Q: The dashboard loads but the event stream is dead.

Nine times out of ten: `uvicorn` was installed without the `standard` extra, and the `websockets` dependency that `/satori/live` needs is missing. `uv pip install "uvicorn[standard]"` fixes it (it's declared in the project dependencies, so this mainly bites hand-rolled installs). The other suspect is a reverse proxy that doesn't upgrade WebSocket connections — configure it to, or check that the gateway address field in the dashboard matches where you actually bound the server.

### Q: Can a vendor fool Satori?

They can cut individual fields — stop returning logprobs, for instance — but they can't cut behavior. As long as the model still produces text, that text is evidence. The sharper move is swapping in an equal-tier model that passes your test suite: the quality dimension waves it through, but the identity pocket (voiceprint / answer fingerprint) keeps its own account, and no PASS can wash it. Evading every channel at once costs more than the fraud pays. That's the entire design philosophy: not one perfect sensor, but a stack whose combined evasion cost exceeds the profit.

### Q: Does it cost extra API calls?

Yes. Voiceprint, answer fingerprint, identity battery and canary are real calls every `check_interval_seconds` (default 300). With many upstreams × models, watch the bill: widen the interval, shrink `identity.sample_size`, or disable checkers. Rules, the three side-channels, hit-rate and slop ride on live traffic and cost nothing extra. In BASIC mode the identity channels aren't even probed — while the eye is closed, that spend stops too.

### Q: Will role-play cause false alarms?

No — this is handled, not hoped away. The builtin `roleplay-excuse` rule recognizes impersonation directives ("pretend you are", "act as", 扮演, 假设你是……) on the request side and grants a −40 exemption, exactly cancelling one vendor self-report. Write your own exemptions in `rules.toml` with `target = "request"`.

## Operations

### Q: I lost a credential / my admin secret. Now what?

Credential plaintext: no recovery by design — revoke (`DELETE /satori/admin/credentials/{id}`) and re-issue. Admin secret: stop, rotate `env:SATORI_ADMIN_SECRET`, restart; existing credentials are unaffected. Bootstrap token: restart the gateway, it dies with the process. And if you lost `state/credentials.db` entirely, every credential is gone — revoke-by-rebuild: re-issue everything from your admin secret. This is why the file is 0600 and why you back it up somewhere that isn't git.

### Q: What are the three alert levels, and how do I tune them?

SAFETY (<25) → WATCH (25–49, "possible degradation, pay attention") → DEGRADED (≥50, alert + breaker if enabled). All three knobs live in `[rules]`: `watch_threshold`, `suspicion_threshold`, `decay_half_life_seconds` (default 3600). The ledger decays with a half-life: each new hit first decays the old score, then adds — an honest upstream's occasional mannerism fades to zero, sustained watering outpaces decay and climbs. Builtin weights are tuned so one high-risk hit (disguise leak, 50) crosses immediately while a medium one (self-report, 40) needs a second stacked signal. Single signals never convict.

### Q: What are STRICT / STANDARD / BASIC again?

Baseline trust (provenance × collection pressure × time decay, γ=1.5) sets monitoring intensity — a throttle, not a gauge:

| Tier | Condition | Identity channels |
|---|---|---|
| STRICT | trust ≥ 0.8 | full weight (fingerprint 20 / answerprint 10) — a mismatch is a serious charge |
| STANDARD | 0.4–0.8 | half weight — no full-trust accounting on an old reference |
| BASIC | no reference / retired / < 0.4 | silent, not even probed — black-box channels only |

The author's own stance sits at STANDARD: if the dish is good I respect your supply chain — but I'm still counting the ingredients.

### Q: What is the circuit breaker, and will it hurt normal requests?

With `[breaker] enabled = true` (the shipped config), a DEGRADED crossing 503s that upstream×model until a human with an operator credential resets it. The positioning is "when the flavor changes, work stops in real time" — better an interruption than low-quality output flowing into your project. Blocking is scoped to the offending upstream×model; everything else flows. Crossing requires stacked signals, so friendly fire is unlikely — but if it happens, `POST /satori/breaker/reset` (signed) clears the breaker and both ledger pockets. Keep `enabled = false` if you want alert-only.

### Q: Why does the control plane need credentials at all? It's localhost.

Because every control endpoint is "an operation that changes an audit conclusion": forged FAILs fake DEGRADED (denial of service), PASS spam washes suspicion (audit bypass), a forged `official_update` makes the system close its own eyes. Localhost is not a trust boundary — it's where your CI, your browser, and every other process live. Loopback + HMAC + bootstrap token is the minimal loop for solo use; multi-team goes Ed25519 + `require_tls`. The read side (status, event stream, dashboard) is deliberately open — that trade-off is spelled out in [SECURITY.md](SECURITY.md) §8.

### Q: How do I wire up my test suite?

```bash
# pytest
SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot SATORI_SECRET=… \
  pytest -p satori_gateway.pytest_plugin
# JUnit XML / TAP / JSON (non-pytest ecosystems)
satori-test-report results.xml --suite core --level L0
```

Levels: `@satori_test(level="L0")` deterministic (N=3, PASS decay 10) / `L1` semantic (5, 5) / `L2` complex reasoning (7, 2) / `L3` open-ended, unscored. Reporting never blocks tests and never turns CI red — failures queue locally and flush next run. One honest warning: **your suite's floor is the ruler's floor**. Write L0 assertions that only the tier you paid for can stably pass; a suite of "at least it parsed" tests measures nothing.

### Q: Which protocols does it speak?

Client side: OpenAI Chat (`/v1/chat/completions`, passthrough), Anthropic Messages (`/v1/messages`), OpenAI Responses (`/v1/responses`) — adapters translate everything into a canonical form the detection core understands, tools/function calling included, both ways. Thinking blocks aren't translated (detection unaffected). Upstream side: OpenAI-compatible by default; `protocol = "anthropic"` talks to the native Anthropic API via `pipelines/`. Voiceprint works only on openai-protocol upstreams.

### Q: Is recording safe?

`[record] enabled = true` writes raw conversation text to `records/` in daily JSONL. It's .gitignored, but guard the directory permissions in sensitive settings — this is the one place Satori keeps your actual conversations. The payoff is replay forensics: upgrade rules today, re-audit yesterday. Tool chains land separately in `state/tool_traces.jsonl` without raw argument text.

### Q: Why does curl with Chinese fail with 400 on Windows?

The Windows console transcodes `-d` content to GBK and the JSON breaks. Not the gateway's fault. Write the body to a file:

```bash
printf '%s' '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}' > req.json
curl -X POST http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" --data-binary @req.json
```

## Extension

### Q: Can I write my own rules / checkers / protocol adapters?

Rules: edit `rules.toml` — `name / field (content|reasoning|any) / target (response|request) / match (contains|regex) / pattern / score (negative allowed) / description`; same name as a builtin overrides it. Validate against samples with `satori replay` before production, and prefer several weak signals over one strong one. Checkers and adapters: drop a module into `checkers/` or `adapters/` with the register decorator — package-scan auto-discovery, no existing file needs editing. Details: [HANDBOOK.md](HANDBOOK.md) §9.

### Q: Can reference fingerprints be shared?

Yes, and it's encouraged — the JSON in `fingerprints/` is plain distribution data, shareable like an antivirus signature database; nobody begs the vendor. It does contain endpoint info (in filenames and sidecars), so look before sharing. Mark `--source` and `--notes` when collecting so downstream users know how much trust weight to give it.

## The ugly truths

- Every single channel can be circumvented by a targeted effort; the point is the stacked cost of circumventing all of them.
- A distilled model with a fully consistent persona beats the content channels. Voiceprint or answerprint is the last line — collect references before you need them.
- Slop scoring is structural: broken JSON is caught, "valid JSON, wrong parameter choice" is your suite's job.
- TTL learning needs real event history; cold start is a fixed 30 days. Parameter-level or time-local degradation is only caught where your suite covers that pattern.
- HMAC mode forces the server to store secrets verbatim — guard `state/credentials.db`; multi-team deployments should use Ed25519.
- Satori never proves "this is the real model". It proves "this is no longer the model it used to be". Behavioral fingerprints are probabilistic, and the defense is the stack, not the layer.
