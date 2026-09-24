# Operations Handbook · HANDBOOK

> The mechanism reference: how each channel works, what every knob does, and how to fix things when they look wrong. New here? Do [QUICKSTART.md](QUICKSTART.md) first. Control-plane security: [SECURITY.md](SECURITY.md). [中文版](../zh/HANDBOOK.md)

## 1. Architecture

```
client ──▶ [adapters/] ──▶ canonical (OpenAI Chat) ──▶ [detection core] ──▶ [pipelines/] ──▶ upstream
           /v1/chat/completions (passthrough)   ├─ rule engine rules.py            OpenAI-compatible
           /v1/messages                         ├─ side-channels watch.py          or native Anthropic
           /v1/responses                        │   (tokenizer / billing / latency / hit-rate)
                                                ├─ periodic checkers checkers/
                                                │   (voiceprint / answerprint / identity / canary)
                                                ├─ suspicion ledger, two pockets + breaker (app.py)
                                                ├─ baseline lifecycle baseline.py + dynamic TTL ttl.py
                                                ├─ test adjudication testing.py
                                                ├─ slop chain forensics slop.py
                                                ├─ control-plane auth security.py (HMAC / Ed25519)
                                                ├─ state persistence state.py · recording record.py
                                                └─ event stream → ws /satori/live → web/index.html
```

One fact explains most of the design: **all detection works on the canonical form**. Whatever the client speaks, the adapter translates to OpenAI Chat before the detection core sees it, and back on the way out. Rules, side-channels, slop and recording are protocol-blind. A new client protocol is one module in `adapters/` with `@register_adapter`; a new upstream protocol is one module in `pipelines/` (`TransferPipeline`). Both registries auto-discover by package scan — no existing file needs editing.

Adapter v2 scope: text and image content, system/instructions, streaming translation, and tools/function calling **both ways** (including streaming `input_json_delta` reassembly — tool chains feed slop forensics). Thinking blocks are not translated; detection is unaffected, but thinking-dependent clients should know.

## 2. Configuration reference (third_eye.toml)

| Section | Key | Ships as | Code default | Notes |
|---|---|---|---|---|
| `[gateway]` | `host` / `port` | 127.0.0.1 / 8400 | same | listen address |
| | `check_interval_seconds` | 300 | 300 | periodic checker interval — checkers are real API calls, watch the bill |
| | `cors_origins` | `["*"]` | `["*"]` | dashboard CORS, local use |
| | `inject_usage` | true | true | auto-add `stream_options.include_usage` to streaming requests (side-channel data source) |
| `[[upstreams]]` | `name` / `base_url` / `api_key` / `models` / `protocol` | — | — | `api_key` supports `env:VAR`; `protocol` defaults to `openai`, set `anthropic` for the native API |
| `[fingerprint]` | `reference_dir` | fingerprints | same | reference directory (voiceprints, answers, sidecars) |
| | `probe_prompt` | "The capital of France is" | same | voiceprint probe |
| | `top_logprobs` | 10 | 10 | distribution candidates (≤ 20) |
| | `js_threshold` | 0.15 | 0.15 | JS-divergence verdict line |
| `[answerprint]` | `enabled` / `similarity_threshold` / `max_tokens` | true / 0.6 / 128 | **false** / same | the shipped file turns it on |
| `[identity]` | `enabled` / `sample_size` / `max_tokens` | true / 5 / 64 | **false** / 5 / 64 | 8-question battery, 5 sampled per round |
| `[[canary.cases]]` | `prompt` / `expect` | two demo cases | — | **replace these with your own** |
| `[rules]` | `path` | rules.toml | same | user rules; same-name overrides a builtin |
| | `suspicion_threshold` | 50 | 50 | DEGRADED / breaker line |
| | `watch_threshold` | 25 | 25 | WATCH line |
| | `decay_half_life_seconds` | 3600 | 3600 | ledger half-life |
| | `hit_rate_threshold` / `hit_rate_min_samples` | 0.05 / 50 | same | hit-rate channel |
| `[breaker]` | `enabled` | true | **false** | the shipped file arms it |
| `[record]` | `enabled` / `directory` | false / records | same | raw conversation text — guard the directory |
| `[security]` | `enabled` / `mode` | true / hmac | same | `mode = "ed25519"` for multi-team |
| | `timestamp_window_seconds` | 300 | 300 | signature time window |
| | `db` | state/credentials.db | same | chmod 0600, never commit |
| | `admin_secret` | `env:SATORI_ADMIN_SECRET` | "" | empty on loopback → one-time bootstrap token; empty on non-loopback → refuses to start |
| | `require_tls` | unset | unset (host-linked) | loopback exempt, non-loopback enforced; true/false overrides |
| | `trusted_proxies` | unset | [] | CIDRs whose `X-Forwarded-Proto` is honored |
| `[state]` | `directory` / `debounce_seconds` / `archive_dir` | state / 1.0 / archive/baselines | same | restart-proof memory |
| `[testing]` | `enabled` / `score_floor` | true / 5.0 | same | PASS may never wash total suspicion below the floor |
| `[logging]` | `level` / `file` | INFO / logs/satori.log | same | loguru, 10 MB rotation; `LOGURU_FULL_TRACEBACK=1` for full stacks |

## 3. Detection channels

Scores below are the ledger weights each channel injects on a hit.

### 3.1 logprob voiceprint — `fingerprint` (20, identity pocket)

The first-token top-logprobs distribution for a fixed probe is a physical property of weights + tokenizer. Compared against the reference from `satori collect` via JS divergence; past `js_threshold` it's "not the same model". Catches substitution and silent quantization; misses effort-reduction on identical weights (the canary's job). Distributions drift slightly across snapshots — re-collect periodically. `--prompt` collects per-identity-question references. **OpenAI-protocol upstreams only** — the pipeline must report logprobs capability, and `collect` refuses otherwise. On a mismatch the checker detail ends with 覚「心の中の弱者」: what was served is not the model that was promised.

### 3.2 Answer fingerprint — `answerprint` (10, identity pocket)

Eight stability-oriented questions (facts, formats, short reasoning — no open-ended prompts, those drift even on one model) at temp=0, answers compared to the `satori answers` reference by SequenceMatcher similarity; below `similarity_threshold` (0.6) it fails. Pure black-box: no logprobs, no usage, just text. This is the primary channel for Claude-family endpoints.

### 3.3 Identity battery — `identity` (context, not a ledger weight by itself)

Eight identity questions in several languages and formats, five sampled and shuffled each round (probe traffic shouldn't be fingerprintable). Three verdicts get cross-checked: vendor self-report contradiction, self-report vs model-name mismatch, knowledge-cutoff drift. Its logprob sub-channel compares answer first tokens against per-question references. Catches distilled repacks' detail slips and mixed multi-source routing — contradiction detection is inherently dilution-resistant. Misses the fully consistent distilled persona; that's what 3.1 and 3.2 are for.

### 3.4 Canary exams — `canary`

Questions with known answers; pass rate is quality. The anti-degradation anchor. Replace the shipped demo cases with your own — private exam questions are part of the Kerckhoffs design (the algorithm is public; your references, exams and baselines are the keys).

### 3.5 Rule engine — `rules.py` (3 to 50, plus exemptions)

Every real response's content and CoT pass 17 builtin rules plus your `rules.toml`. Builtin tiers: disguise-system-prompt leak (50), vendor self-reports ×8 (40), proxy-relay disclosure (25), CoT language switch (15), mannerisms and phrasebook signals (3–10), and `roleplay-excuse` (−40, request-side exemption that cancels one induced self-report). Rule shape: `field = content|reasoning|any`, `target = response|request`, `match = contains|regex`, `score` may be negative. Same name as a builtin overrides it.

### 3.6 Self-baselining side-channels — tokenizer (30) / billing (30) / latency (15)

EMA baseline per upstream×model, then alert only on 3 consecutive breaches beyond 25% deviation after ≥20 samples — conservative by design. Tokenizer ratio (request chars / `usage.prompt_tokens`) exposes a tokenizer swap; billing ratio (response chars / `completion_tokens`) exposes token padding; first-byte latency is an infrastructure signature. Zero extra requests, no public signature to evade. Deliberately insensitive to low-ratio dilution — that's the price of no false alarms, and the hit-rate channel pays the other half of that bill.

### 3.7 Severe-rule hit-rate — `hitrate` (20)

EMA of the share of responses hitting a severe rule (score ≥ 25), alerting past 5% after 50 samples. The "90% real, 10% fake" dilution tactic leaves fingerprints proportional to its dilution: measured, 10% dilution alerts reliably while honest ~1% noise stays silent.

### 3.8 Slop chain forensics — `slop.py` (confirm: 40, quality pocket)

When a model is degraded under tool-calling, the damage isn't "answers get dumber" — it's "arguments arrive broken and silently pollute downstream systems". Every tool-call-bearing response emits a `change_trace` (per step: tool, args valid, args bytes, flags, score) to `state/tool_traces.jsonl` — clean chains are recorded too, for replay. Raw arguments are never persisted.

Structural scoring: broken JSON arguments +30, undeclared tool +20, identical repeat within one response +15, argument bloat >8 KB +10. **Honest boundary: not semantic adjudication** — "plausible JSON, wrong parameter choice" is your test suite's job.

Three phases: suspicion (any step scores >0, broadcast) → confirmed (2 suspicious steps in one session → +40 into the quality pocket, washable by L0/L1 PASSes only down to the floor) → backtrack (the first polluted step is highlighted; replay prints it with 覚「この呼び出し、少し違和感が…」). Sessions come from the client's `X-Satori-Session` header; without it, traffic falls back to per-upstream×model aggregation — per-request trace IDs would never accumulate to a verdict. Session tracking is bounded (1000 slots, least-recently-active evicted).

### 3.9 Business-test adjudication — `testing.py`

Your suite is the quality anchor: degradation implies capability drop, and "can it do the job" is the one thing a vendor cannot forge. `POST /satori/test/report` (reporter role) takes `trace_id / test_suite / test_name / level / status / attempt / max_attempts / failure_diff / model_claimed / upstream`; idempotent on reporter+trace_id+test_name+attempt, rebuilt from the stream on restart.

- **Levels**: L0 deterministic (trigger N=3, inject 15, PASS decay 10) / L1 semantic (5, 10, 5) / L2 complex reasoning (7, 8, 2) / L3 open-ended, recorded but unscored.
- **Sliding window**: ≥N fails within the last 3N runs triggers. Trigger re-arms the window.
- **Flaky auto-marking**: lifetime failure rate >5% with ≥20 samples → `unreliable`, recorded but never adjudicated again.
- **Trust buckets**: TRUSTED and NORMAL reports adjudicate in fully separate windows/lifetimes — low-trust reports can neither dilute nor poison the TRUSTED window. UNVERIFIED is persisted only.
- **Asymmetric gating**: only TRUSTED PASSes decay suspicion (quality pocket only, never below `score_floor`, identity untouched); only TRUSTED FAIL-triggers enjoy "floor-first, line-crossing" injection — the injected score is topped up to whatever crosses the DEGRADED line, and the breaker trips immediately. NORMAL triggers add just the level score; the general threshold decides.
- **Wiring**: `pytest -p satori_gateway.pytest_plugin` with `@satori_test(level=...)`, or `satori-test-report results.xml` for JUnit/TAP/JSON. Env vars and fault tolerance: [SECURITY.md](SECURITY.md) §6.

## 4. The ledger, tiers, and the breaker

**Two pockets.** Quality-class (rules, side-channels, hit-rate, slop, test FAILs) can be decayed by TRUSTED PASSes down to the floor. Identity-class (voiceprint, answerprint) cannot be laundered by any test result — unfreezing takes re-collecting references or an explicit operator ruling (`POST /satori/baseline/identity-cleared`, identity pocket only). A vendor who swaps in an equal-quality model that passes your whole suite still loses the identity account.

**Three alert levels.** SAFETY (<25) → WATCH (25–49) → DEGRADED (≥50), defaults tunable in `[rules]`. Each scoring event first decays the old score by the half-life, then adds. Isolated faults fade; sustained watering outpaces decay and climbs. Auditing calls this the materiality threshold: prosecute abnormal frequency, not isolated slips. Level-ups broadcast; crossing DEGRADED logs 覚「想起うさぎは警戒を」.

**Three baseline tiers** — a throttle, not a gauge. Alerts decide whether to punish; tiers decide how hard to watch. Trust `= W_source × W_pressure × max(0, 1 − age/TTL)^1.5`:

- `W_source`: official 1.0 / secondhand 0.5 / community 0.3, from the collection sidecar; a missing sidecar means secondhand. Source before timing — a "reference" scraped from a relay is just a pretty second-hand truth.
- `W_pressure`: LOW/MID 1.0 / HIGH 0.6 / EXTR 0.0, recorded at collection time.
- Tier actions: STRICT ≥0.8 full identity weight (20/10) · STANDARD ≥0.4 half (10/5) · BASIC below 0.4 or no reference — identity channels aren't even probed (their stale results are dropped from the panel too). Below 0.1 the baseline is additionally flagged `expired`.

**Breaker SOP.**

1. `breaker open` on dashboard/logs → that upstream×model 503s (other upstreams unaffected).
2. Forensics: hit details in `logs/satori.log`; re-audit with `satori replay records/x.jsonl --tool-traces state/tool_traces.jsonl`.
3. False positive: signed `POST /satori/breaker/reset {"upstream","model"}` (operator). Also clears both pockets.
4. Real official update: `satori feedback … --reason official_update --confirm` runs retirement. Real fraud: switch upstreams, take the recordings to the refund dispute.

## 5. Baseline lifecycle and TTL

**Retirement is a human act.** `POST /satori/baseline/feedback` with `confirm` and reason `official_update` counts confirmations: one operator confirm retires immediately; reporter confirms need 3 — except during cold start (<3 baseline events for that upstream×model), when one is enough. Retirement archives the references and sidecars to `archive/baselines/<upstream>--<model>/` with a meta.json receipt, records an event, clears the ledger, and the upstream×model degrades to BASIC instead of false-alarming. `network_jitter` / `false_alarm` confirms only clear the ledger. Re-running `collect`/`answers` clears the retired marker.

**TTL is a reminder, not a judge.** The shelf-life is learned from real events (`baseline_events.jsonl`: `retired` and `refreshed`): fewer than 3 events → fixed 30 days; 3–4 events → median interval × 0.7; ≥5 → median × 0.9 (3σ outliers rejected), clamped to [7, 180] days. Consumption lights a dashboard lamp at 70% (aging) / 90% (critical) / 100% (expired), broadcast once per state flip. **Expiry never retires anything by itself** — killing by prediction turns the failure mode from "crying wolf" into "the real wolf arrives and we stay silent". 覚「予言は灯をともす。刃は振るわない」 — prophecy lights the lamp; it never swings the blade.

Operators can lock a TTL manually: `POST /satori/baseline/ttl/override` with `ttl_days` (+ optional `expires_at`, `reason`), persisted to `state/ttl_overrides.json`. The lock expires or clears and the learned value returns — operators can buy time, not commute sentences.

## 6. State directory — what you can delete and what it costs

| File | Contents | Deleting costs |
|---|---|---|
| `ledger.json` | ledger/breaker snapshot (debounced, atomic) | ledger wipe on next start |
| `feedback.jsonl` | feedback stream | TTL learning material, audit trail |
| `baseline_events.jsonl` | retired/refreshed events | TTL falls back to cold-start 30d |
| `test_reports.jsonl` | test report stream | idempotency set and windows reset |
| `tool_traces.jsonl` | tool chains (no raw args) | chain replay forensics |
| `ttl_overrides.json` | manual TTL locks | locks release to learned values |
| `credentials.db` | control-plane credentials | **never delete, never commit** (0600) |
| `archive/baselines/` | retired references + receipts | rollback material, retirement history |

`records/` (when `[record]` is on) holds raw conversation text in daily JSONL — .gitignored, guard it like logs.

## 7. Event stream reference

`ws /satori/live` sends a `snapshot` on connect, then pushes JSON events (all with `ts`):

| Type | Meaning |
|---|---|
| `check` | a checker ran (`ok`, `score`, `detail`) |
| `request` | a proxied request finished (status, latency, tokens) |
| `suspicion` | ledger write (gained, total, hits) |
| `level` | alert-level transition (up only) |
| `alert` | DEGRADED crossing |
| `breaker` | `open` / `closed` |
| `baseline` | `retired` / `identity-cleared` |
| `ttl` | aging warning or manual override |
| `feedback` | feedback recorded (incl. `confirm-deduped`) |
| `test` | a test report's adjudication |
| `slop` | `suspicious` / `confirmed` |
| `restored` | state recovered after restart |

Subscribe to build your own dashboard or wire alert bots.

## 8. Troubleshooting

- **"No baseline collected … BASIC mode" at startup** — expected until you `collect`/`answers`. The startup report distinguishes "never opened the eye" from "deliberately closed" (retired).
- **Fingerprint skips on an anthropic upstream** — by design; no logprobs exist there. Use `satori answers`. See [QA.md](QA.md).
- **Checker errors in `logs/satori.log`** — "no reference" reminders are normal (go collect); HTTP errors mean the upstream or key, not detection.
- **Dashboard connects but nothing moves** — check `uvicorn[standard]` is installed; the WS channel depends on it.
- **401 on control endpoints** — missing/expired signature headers, clock skew beyond the 300s window, or a revoked credential. 403 means authenticated but wrong role (or TLS required).
- **Replay shows hits that live traffic didn't** — you upgraded rules between recording and replay. That's the feature working.

## 9. Extension development

**Checkers**: drop a module into `checkers/` with `@register_checker`, a `from_config(cls, cfg)` factory (return `None` when disabled) and `async check(client, upstream, model) -> CheckResult`. Auto-discovered.

**Adapters / pipelines**: one module + the register decorator; see `adapters/anthropic.py` (v2: two-way tool translation, streaming reassembly) and `pipelines/anthropic.py` (declarative `TransferPipeline`).

**Rules**: edit `rules.toml`, validate against samples with `satori replay` before production, keep identity-class strong signals at ≥25 points so they feed the hit-rate channel, and prefer several weak signals over one strong one — false-positive resistance beats elegance.
