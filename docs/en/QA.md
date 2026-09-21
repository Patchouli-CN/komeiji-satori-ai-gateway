# Q&A · Common questions

> The Chinese original ([../zh/QA.md](../zh/QA.md)) is the living document; this English edition covers the same ground for non-Chinese readers.

## Positioning & principles

### Q: What is Satori?

A multi-protocol gateway (OpenAI Chat / Anthropic Messages / OpenAI Responses) sitting between you and your AI upstreams. Clients plug in without noticing; it audits every drop of traffic in the background: model substitution, silent routing, quiet degradation — all recorded into a suspicion ledger with live alerts; on threshold it can trip the circuit breaker and stop low-quality output dead.

### Q: Why is it named Komeiji Satori?

A satori youkai from *Touhou Chireiden* famed for mind-reading — seeing through every facade straight to the heart. Whatever the vendor serves up, one glance (at the token distribution) tells the truth.

### Q: How does it detect model substitution?

Defense in depth — no single silver bullet:

- **logprob voiceprint**: the token distribution is a physical property of the model — distillation steals persona but not distributions
- **Answer fingerprint**: fixed battery at temp=0, answers compared to reference via similarity (LLMmap-style); pure black-box — the primary channel for Claude-family endpoints (no logprobs)
- **usage tokenizer side-channel**: token counts for the same text expose the tokenizer — a swap drifts the ratio (self-baselining, zero extra requests)
- **Billing consistency**: completion_tokens vs received text ratio drift catches token inflation / billing skim
- **Identity battery**: multi-phrasing cross-checks; vendor self-report contradictions and knowledge-cutoff drift are the tells
- **Canary exams**: questions with known answers; degradation shows in pass rate immediately
- **Business test anchor**: your own test suite's outcomes — degradation implies a capability drop; consecutive suite failures are hard evidence, and the vendor cannot forge "can it do the job right"
- **Rule engine**: mannerisms, disguise-prompt leaks, CoT language slips — 17 built-in plus your own
- **Latency profile**: first-byte latency as infrastructure signature, self-baselined drift detection
- **Hit-rate channel**: serious-rule hit rate above threshold — built for the "90% real, 10% fake" dilution tactic
- **Slop chain forensics**: broken argument JSON / undeclared tools / repeats — the typical shape of degraded models polluting downstream systems

All hits land in one two-pocket ledger (quality-class + identity-class), escalating across `SAFETY → WATCH → DEGRADED`.

### Q: Can a vendor fool Satori in return?

They can cut individual fields (e.g., stop returning logprobs), but **they can't cut behavior** — as long as the model still produces text, that output is evidence. A sharper move is swapping in an "equal-tier model that passes your test suite": the quality dimension waves it through, but the **identity pocket** (voiceprint / answer fingerprint) keeps that account — and no test PASS can wash it. Each layer assumes a different level of cooperation, and cutting everything costs more than it pays. That is the "without depending entirely on the vendor" design philosophy.

### Q: What about a distilled model that genuinely believes it's Claude?

Content channels miss it (it isn't "lying" — it's "amnesiac"), but the logprob voiceprint catches it — the mouth can impersonate, the token distribution can't; endpoints without logprobs fall back to the **answer fingerprint**: answering habits are equally unstealable. So **collect references with an official key before going live** (`satori collect` for voiceprint, `satori answers` for answers), or both channels idle.

### Q: What's the two-axis model? Why can't test PASSes wash identity suspicion?

Adjudication stands on two orthogonal axes — miss one and you're routed around:

| Axis | Governs | Implementation |
|---|---|---|
| Trust | whose PASS/FAIL carries weight | TrustLevel: TRUSTED (PASS may decay, FAIL may trip breaker) / NORMAL (recorded only) / UNVERIFIED (recorded only) |
| Signal | what a PASS can wash | PASSes only decay **quality-class** hits (rules/billing/latency/Slop/test) and never below the negative floor; **identity-class** hits (voiceprint JS / answer fingerprint) can't be washed by any test result |

Translation: even with a TRUSTED credential spamming test PASSes, the "is this still the original model" account loses nothing. Unfreezing identity suspicion takes exactly two roads — re-collect references, or an explicit operator ruling (`POST /satori/baseline/identity-cleared`).

## Usage

### Q: Does it cost extra API calls?

Yes. Voiceprint, answer fingerprint, identity battery and canary are real API calls on `check_interval_seconds`. Watch the bill with many upstreams × models; widen the interval, shrink `identity.sample_size`, or disable checkers per config. Rules, the three side-channels and the hit-rate channel cost zero extra requests — they ride on live traffic. Under the `BASIC` tier identity channels aren't even probed — while the eye is closed, that spend stops too.

### Q: Anthropic's official API has no logprobs — how do I verify Claude endpoints?

Primary: **answer fingerprint** (`satori answers` collects references from the official endpoint, pure black-box); supported by the identity battery, rules and latency profile; for voiceprint you can also collect a "second-hand reference" from a trusted third party (`--source secondhand`, half trust weight, provenance recorded in the sidecar).

### Q: Is recording safe?

`[record] enabled = true` writes conversation text to `records/`. **Be careful in sensitive settings**, guard directory permissions; the directory is .gitignored and never enters the repo. Replay forensics (`satori replay`) is its core value: re-audit history after rule upgrades, settle accounts later. Tool chains land separately in `state/tool_traces.jsonl` (no raw argument text); `satori replay --tool-traces` walks them step by step.

### Q: Will role-play induce false alarms?

No. The built-in `roleplay-excuse` rule recognizes Chinese/English impersonation directives (pretend / act as / 扮演 / 假设你是……) and grants a −40 exemption, exactly cancelling one vendor self-report. You can write your own exemptions in `rules.toml`.

### Q: What are the three alert levels?

The ledger divides scores into three levels; transitions broadcast live to the event stream and dashboard:

| Level | Default score | Meaning | Action |
|---|---|---|---|
| `SAFETY` | < 25 | safe | none |
| `WATCH` | 25 ~ 49 | possible degradation, pay attention | event alert, orange dashboard badge |
| `DEGRADED` | ≥ 50 | quality degraded | alert + breaker (if enabled) |

The ledger has **half-life decay** (default 1 hour, `decay_half_life_seconds` tunable): isolated small faults fade to zero — an honest upstream's occasional mannerism never accumulates into a wrongful conviction; only sustained anomalies climb past WATCH.

### Q: How do I tune alert thresholds?

`[rules]` in `third_eye.toml`: `suspicion_threshold` (DEGRADED/breaker line, default 50), `watch_threshold` (WATCH line, default 25), `decay_half_life_seconds` (half-life, default 3600). Built-in rule weights are designed so a single high-risk hit (disguise leak, 50) crosses immediately while a single medium hit (vendor self-report, 40) needs a second stacked signal — single signals never convict.

### Q: What are the three baseline tiers (STRICT/STANDARD/BASIC)?

Baseline trust `trust = W_source × W_pressure × decay^γ` sets the monitoring intensity — **tier is a throttle, not a gauge**:

| Tier | Condition | Identity-channel action |
|---|---|---|
| `STRICT` | trust ≥ 0.8 | full scoring (fingerprint 20 / answer 10) |
| `STANDARD` | 0.4 ~ 0.8 | half scoring — aged/low-trust sources get no full-trust accounting |
| `BASIC` | no reference / retired / below 0.4 | silenced — not even probed; black-box channels only |

The dashboard's "Baseline tiers" card shows each upstream×model's tier, trust, TTL consumption, source and retirement time live.

### Q: What happens when a baseline expires? Do I re-collect manually?

No world-ending event. TTL (shelf life) is learned from your real events (confirmed `official_update`s and intervals); consumption lights yellow at 70%, orange at 90%, red at 100% — **but only lamps, never auto-retirement**. Do one of two things:

- Confirm the vendor really updated the model: `satori feedback --upstream U --model M --reason official_update --confirm` (reporter ×3 or operator ×1; ×1 during cold start) → the old reference archives into `archive/baselines/`, the tier degrades to BASIC, then re-`collect`/`answers`
- Just a false positive / network jitter: same command with `--reason network_jitter` (or `false_alarm`) clears the ledger only, leaving the baseline intact

**Why doesn't TTL auto-retire**: killing by prediction turns the failure mode from "crying wolf" into "the real wolf arrives and we stay silent" — predictive models have no authority over ground-truth actions.

### Q: Can dilution (90% real, 10% fake) be caught?

Yes — that's the hit-rate channel's design scenario. Dilution means one in ten trades leaves fingerprints: the rule engine scores each response (accumulation outpaces decay), the identity battery catches "two answers contradicting each other", and the **hit-rate channel** tracks the serious-rule (≥25 pts) hit ratio, alerting above a sustained 5% (`hit_rate_threshold` / `hit_rate_min_samples` tunable). Measured: 10% dilution alerts reliably, honest 1% noise stays silent.

### Q: What is the circuit breaker? Will it hurt my normal requests?

With `[breaker] enabled = true`, any upstream×model crossing the threshold trips: subsequent requests 503 until you confirm and reset. The positioning is "when the flavor changes, work stops in real time" — better to interrupt than let low-quality output flow into the project. Blocking targets only the offending upstream×model; other upstreams are unaffected; crossing requires multiple stacked signals, so friendly fire is unlikely. Keep `enabled = false` for alert-only. **Reset now requires an operator credential** (see SECURITY.md) — the watchdog can't be switched off by anyone.

### Q: Why credentials for the control plane? Even a local dashboard?

Because every control-plane endpoint is "an operation that can change audit conclusions": forged test reports to fake DEGRADED (denial of service), PASS spam to wash suspicion (audit bypass), `official_update` confirmations to make the system close its own eyes (the most lethal). Loopback + HMAC + a one-time bootstrap token is the minimal loop for solo use; multi-team cross-network goes Ed25519 + `require_tls`. Issuance/revocation/leak response: [SECURITY.md](SECURITY.md).

### Q: How do I wire up my own test suite?

Three entries, one semantics:

```bash
# pytest ecosystem (zero blocking during execution; failures queue locally for retry)
SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot SATORI_SECRET=... \
  pytest -p satori_gateway.pytest_plugin
# non-pytest: JUnit XML / TAP / JSON
satori-test-report results.xml --suite core --level L0
# custom: POST /satori/test/report — fields in HANDBOOK 3.9
```

Levels: `@satori_test(level="L0")` deterministic (N=3, PASS decay 10) / `L1` semantic (N=5, 5) / `L2` complex reasoning (N=7, 2) / `L3` open-ended, unscored. **Your suite's floor is the ruler's floor** — write more L0 assertions that only the tier you paid for can stably pass, fewer "at least it parsed" L1s.

### Q: Which client protocols are supported?

Three entries: OpenAI Chat (`/v1/chat/completions`, passthrough), Anthropic Messages (`/v1/messages`), OpenAI Responses (`/v1/responses`). **Adapter v2 translates tools / function calling both ways** (including streaming `input_json_delta` reassembly; tool chains feed Slop forensics); thinking blocks remain untranslated (detection unaffected). Upstream side defaults to OpenAI-compatible protocol; set `protocol = "anthropic"` for the native Anthropic API (voiceprint channel is openai-protocol only).

### Q: Why does curl with Chinese fail with 400 on Windows?

The Windows console transcodes `-d` content to GBK and the JSON breaks. Not the gateway's fault. Use a file:

```bash
printf '%s' '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}' > req.json
curl -X POST http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" --data-binary @req.json
```

## Extension

### Q: How do I write my own rules?

Edit `rules.toml` (project root):

```toml
[[rules]]
name = "my-rule"
field = "reasoning"   # content | reasoning | any
match = "regex"       # contains | regex
pattern = "your pattern"
score = 20            # may be negative (exemption)
description = "hit description"
```

Same name as a built-in overrides it (retuning). `target = "request"` rules inspect the user request, not model output.

### Q: How do I write my own checker / protocol adapter?

Both are decorator + package-scan auto-discovery (Spring `@ComponentScan` style):

- checker: drop a module into `checkers/`, `@register_checker` + implement the `from_config(cfg)` factory and `check()`; returning `None` means config-disabled
- adapter: drop a module into `adapters/`, `@register_adapter` + implement `to_canonical` / `from_canonical` / `translate_sse`

No existing file needs editing.

### Q: Can reference fingerprints be shared?

Yes, and it's encouraged. The JSON in `fingerprints/` is plain distribution data — share it like an antivirus signature database; nobody begs the vendor. Note it contains endpoint info (in filenames), so look before sharing. When collecting/sharing, mark `--source` and `--notes` so downstream users know how much trust weight to give it.

## Limitations (the ugly truths)

- Every single channel can be circumvented by targeted means; the point of this project is the stacked cost of circumventing all of them
- Identity self-reports can be induced by role-play — hence exemptions and threshold design
- Fingerprints drift slightly with quantization/snapshots — re-collect periodically
- The test anchor catches degradation, the voiceprint catches equal-tier swaps — complementary chains, but **your suite's floor is the ruler's floor**; don't mistake toy tests for a real ruler
- Slop's structural scoring is not semantic: broken JSON is caught, "valid JSON, wrong parameter choice" is the test suite's job
- TTL learning needs real event accumulation — cold start is fixed 30 days; parameter/time-local degradation is only caught where your suite covers that pattern
- HMAC mode forces the server to store the secret verbatim (verification needs the key) — guard `state/credentials.db`; multi-team deployments should go Ed25519
- We don't prove "this is the real model", we discover "this isn't the model it used to be" — behavioral fingerprints are probabilistic
