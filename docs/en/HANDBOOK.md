# Operations Handbook · HANDBOOK

> The full reference: configuration, internals, ops, and extension development. New here? Start with [QUICKSTART.md](QUICKSTART.md); common questions live in the [Chinese Q&A](../zh/QA.md); control-plane security lives in [SECURITY.md](SECURITY.md).

[中文版](../zh/HANDBOOK.md)

## 1. Architecture

```
client ──▶ [protocol adapters adapters/] ──▶ canonical form (OpenAI Chat) ──▶ [detection core] ──▶ [pipelines pipelines/] ──▶ upstream (OpenAI-compatible / native Anthropic)
           /v1/chat/completions (passthrough)     ├─ rule engine rules.py (every response)
           /v1/messages                           ├─ side-channels watch.py (tokenizer/billing/latency/hit-rate)
           /v1/responses                          ├─ periodic checkers checkers/ (voiceprint/answers/identity/canary)
                                                  ├─ suspicion ledger (quality + identity pockets) + 3 alert levels + circuit breaker app.py
                                                  ├─ baseline lifecycle baseline.py (trust / three tiers / retirement & archive)
                                                  ├─ dynamic TTL ttl.py (learned median / aging warnings)
                                                  ├─ test adjudication testing.py (sliding window / flaky / two-axis gating)
                                                  ├─ Slop chain forensics slop.py (change_trace / suspicion → confirmed)
                                                  ├─ control-plane security security.py (HMAC/Ed25519 / roles / TLS)
                                                  ├─ state persistence state.py (ledger / event streams)
                                                  ├─ recording record.py (daily JSONL + tool-chain replay)
                                                  └─ event stream publish() → ws /satori/live → web/index.html
```

Key point: **all detection works on the canonical form**. Whatever protocol the client speaks, the adapter translates to OpenAI Chat before the detection core sees it, and back for the response. Rules, side-channels and recording are protocol-blind.

## 2. Configuration reference (third_eye.toml)

| Section | Key | Default | Notes |
|---|---|---|---|
| `[gateway]` | `host` / `port` | 127.0.0.1 / 8400 | listen address |
| | `check_interval_seconds` | 300 | periodic checker interval |
| | `cors_origins` | `["*"]` | dashboard CORS (local use) |
| | `inject_usage` | true | auto-add `include_usage` to streaming requests (side-channel data source) |
| `[[upstreams]]` | `name` / `base_url` / `api_key` / `models` / `protocol` | — | upstreams; `api_key` supports `env:VAR`; `protocol` defaults to `openai`, set `anthropic` for the native API (voiceprint channel is openai-protocol only) |
| `[fingerprint]` | `reference_dir` | fingerprints | reference fingerprint directory |
| | `probe_prompt` | "The capital of France is" | voiceprint probe |
| | `top_logprobs` | 10 | distribution candidates (≤20) |
| | `js_threshold` | 0.15 | JS-divergence alert threshold |
| `[answerprint]` | `enabled` / `similarity_threshold` / `max_tokens` | true / 0.6 / 128 | answer fingerprint |
| `[identity]` | `enabled` / `sample_size` / `max_tokens` | true / 5 / 64 | identity battery (8 questions, 5 sampled) |
| `[[canary.cases]]` | `prompt` / `expect` | — | canary exams (**customize these**) |
| `[rules]` | `path` | rules.toml | user rule file |
| | `suspicion_threshold` | 50 | DEGRADED / breaker line |
| | `watch_threshold` | 25 | WATCH line |
| | `decay_half_life_seconds` | 3600 | ledger decay half-life |
| | `hit_rate_threshold` / `hit_rate_min_samples` | 0.05 / 50 | hit-rate channel |
| `[breaker]` | `enabled` | true | circuit breaker |
| `[record]` | `enabled` / `directory` | false / records | traffic recording |
| `[security]` | `enabled` / `mode` | true / hmac | control-plane auth; `mode` may be `ed25519` (multi-team) |
| | `timestamp_window_seconds` | 300 | HMAC timestamp window (replay protection) |
| | `db` | state/credentials.db | credential store (**HMAC stores secrets verbatim — guard this file**) |
| | `admin_secret` | env:SATORI_ADMIN_SECRET | admin password; leave empty on loopback for a one-time bootstrap token |
| | `require_tls` | null (host-linked) | loopback exempt, non-loopback enforced; true/false overrides |
| | `trusted_proxies` | [] | trusted reverse-proxy CIDRs whose `X-Forwarded-Proto` is honored |
| `[state]` | `directory` | state | audit-state directory |
| | `debounce_seconds` | 1.0 | ledger flush debounce |
| | `archive_dir` | archive/baselines | retired-baseline archive |
| `[testing]` | `enabled` / `score_floor` | true / 5.0 | business-test adjudication / negative-score floor |
| `[logging]` | `level` / `file` | INFO / logs/satori.log | loguru (10MB rotation) |

### The state/ directory (restart-proof memory)

| File | Contents | Safe to delete? |
|---|---|---|
| `ledger.json` | ledger/breaker snapshot (debounced + atomic replace) | deleting = ledger wipe on next start (not recommended) |
| `feedback.jsonl` | false-positive feedback stream (replay-correlatable) | deleting = less TTL learning material |
| `baseline_events.jsonl` | baseline events (retired/refreshed — TTL data source) | deleting = TTL falls back to cold-start 30d |
| `test_reports.jsonl` | test-report stream (adjudicator rebuilds on restart) | deleting = idempotency set & windows reset |
| `tool_traces.jsonl` | tool-chain traces (input for `satori replay --tool-traces`) | deleting = lose chain replay forensics |
| `credentials.db` | control-plane credential store (SQLite) | **never delete, never commit** |
| `ttl_overrides.json` | manually locked shelf-lives | deleting = unlock, back to learned values |

## 3. Detection channels in depth

### 3.1 logprob voiceprint (fingerprint)

- **Principle**: the first-token top-logprobs distribution is a physical fingerprint of weights + tokenizer; compared against an official reference via JS divergence
- **Requires**: `satori collect` reference; upstream logprobs support
- **Catches**: substitution, silent quantization. **Misses**: effort reduction on identical weights (that's the canary's job)
- **Notes**: distributions drift slightly across snapshots/quantization — re-collect periodically; `--prompt` collects per-identity-question references
- **Scoring**: failures feed the **identity pocket**, weighted by the baseline tier (see 3.8)

### 3.2 Answer fingerprint (answerprint)

- **Principle**: 8 stability-oriented questions at temp=0, answers compared to reference via SequenceMatcher similarity (LLMmap-style)
- **Requires**: `satori answers` reference. **Pure black-box** — the primary channel for Claude-family endpoints
- **Catches**: substitution + degradation. Open-ended questions drift, so the battery is all facts/formats/short reasoning

### 3.3 Identity battery (identity)

- **Principle**: multi-language, multi-format identity questions sampled randomly, aggregated over three verdicts — vendor self-report contradiction, self-report vs model-name mismatch, knowledge-cutoff drift
- **Catches**: distilled repacks' detail slips, multi-source mixed routing (contradiction detection is inherently anti-dilution)
- **Misses**: fully consistent distilled personas (voiceprint/answer channels cover that)

### 3.4 Canary exams (canary)

- **Principle**: questions with known answers; pass rate is quality. **The anti-degradation anchor**
- **Strongly recommended**: replace the default questions with your own — private exam questions are part of the Kerckhoffs design

### 3.5 Rule engine (rules)

- **Principle**: every real response's content/CoT passes 17 built-in + user rules; hits score into the ledger
- Built-in tiers: disguise leaks (50), vendor self-reports (40), proxy disclosures (25), weak signals (3~15), plus exemptions (−40)
- **Scopes**: `field: content/reasoning/any`; `target: response/request`

### 3.6 Self-baselining side-channels (tokenwatch / billing / latency)

- **Principle**: EMA baseline + consecutive-breach alerting. Tokenizer ratio (chars/prompt_tokens), billing ratio (chars/completion_tokens), first-byte latency
- **Properties**: zero extra requests, no public signatures to evade; intentionally insensitive to low-ratio dilution (the price of no false alarms)

### 3.7 Hit-rate channel (hitrate)

- **Principle**: EMA of serious-rule (≥25 pts) hit rate alerts above 5% — the "90% real, 10% fake" tactic gets caught proportional to its dilution
- **Measured**: 10% dilution alerts reliably (7.2%), honest 1% noise stays silent

### 3.8 Baseline lifecycle & three tiers (baseline.py + ttl.py)

**Trust formula**: `trust(t) = W_source × W_pressure × max(0, 1 − age/TTL)^γ`

- `W_source`: provenance weight — official 1.0 / secondhand 0.5 / community 0.3 (missing sidecar → secondhand). **Source before timing**: a "reference" scraped from a relay endpoint is just a pretty second-hand truth
- `W_pressure`: collection pressure — LOW/MID 1.0 / HIGH 0.6 / EXTR 0.0 (from vendor-local-time traffic patterns)
- `age/TTL`: TTL is learned by `ttl.py` from real events (median × 0.9/0.7, clamped [7d, 180d]); cold start is fixed 30d

**Three-tier actions** (tier is a throttle, not a gauge):

| Tier | Condition | Identity channels (voiceprint/answers) | Meaning |
|---|---|---|---|
| `STRICT` | trust ≥ 0.8 | full weight (20/10) | fresh trusted reference — a mismatch is a serious charge |
| `STANDARD` | 0.4 ≤ trust < 0.8 | **half** weight (10/5) | aged/low-trust source — no full-trust accounting on an old reference |
| `BASIC` | no reference / retired / trust < 0.4 | **silent** — not even probed | black-box channels only; saves tokens and never accuses unjustly |

**Retirement loop**: `POST /satori/baseline/feedback` (reporter role) with `official_update` confirm — reporter ×3 (×1 during cold start) or operator ×1 → references archived to `archive/baselines/` (with meta.json receipts) → that upstream×model degrades to BASIC. `network_jitter` / `false_alarm` only clears the ledger, never retires. **TTL expiry only lights lamps (70% yellow / 90% orange / 100% red) — never auto-retires.** Predictions don't kill; retirement needs human ground truth.

### 3.9 Business-test adjudication (testing.py) — the quality anchor

- **Reporting**: `POST /satori/test/report` (reporter role); fields `trace_id/test_suite/test_name/level/status/attempt/failure_diff/model_claimed/upstream`; idempotency key = trace_id+test_name+attempt (rebuilt from the stream on restart)
- **Levels**: L0 deterministic (N=3, inject 15, PASS decay 10) / L1 semantic (N=5, 10, 5) / L2 complex reasoning (N=7, 8, 2) / L3 open-ended, unscored
- **Sliding window**: fail ≥ N within the last M=N×3 runs triggers; **flaky auto-marking** (failure rate >5% with ≥20 samples → unreliable, recorded but unadjudicated)
- **Two-axis gating**: trust axis (only TRUSTED PASSes decay; only TRUSTED FAILs enjoy "score-floor-first, line-crossing" injection to DEGRADED-class suspicion and can trip the breaker immediately; NORMAL only adds level scores; UNVERIFIED is recorded only) × signal axis (PASSes only wash quality-class hits, never below the negative floor)
- **Wiring**: pytest plugin `-p satori_gateway.pytest_plugin` (`@satori_test(level=)`); non-pytest ecosystems `satori-test-report results.xml`; credentials via env vars (SATORI_URL/REPORTER/SECRET — see SECURITY.md)

### 3.10 Tool-call chain forensics (slop.py)

- **change_trace**: every tool-call-bearing response emits a trace (step/tool/args_valid/args_bytes/flags/score) to `state/tool_traces.jsonl` — clean chains are recorded too, for replay
- **Structural scoring**: broken JSON arguments (+30) / undeclared tool (called but not in the request's tools — +20) / identical repeat within one response (+15) / argument bloat >8KB (+10). **Honest boundary: not semantic adjudication** — "plausible JSON, wrong parameter choice" is the test suite's job
- **Three phases**: suspicion (any step scoring >0, broadcast) → confirmed (two suspicious steps in one session → inject DEGRADED-class **quality-class** suspicion at 40, above Logprob weight, washable by L0/L1 PASSes only down to the floor) → backtrack (origin_trace + origin_step highlight the first "polluted thought")
- **Prerequisite**: adapter v2 translates tools both ways (including streaming `input_json_delta` reassembly); session is declared by the client's `X-Satori-Session` header (defaults to per-request granularity)

## 4. Alerts, decay, tiers & breaker ops

**Three alert levels (ledger)**: `SAFETY` (<25) → `WATCH` (25–49, pay attention) → `DEGRADED` (≥50, alert + breaker).
**Three action tiers (baseline)**: `STRICT` → `STANDARD` → `BASIC`, see 3.8 — alerts decide **whether to punish**, tiers decide **how hard to watch**.

**Decay**: each scoring event first decays the old score by the half-life, then adds. Meaning:

- Isolated small faults (a real model's occasional mannerism) → fade to zero, no wrongful convictions
- Sustained anomalies → scoring outpaces decay, levels climb anyway
- Auditing calls this the "materiality threshold": don't prosecute isolated slips, prosecute abnormal frequency

**Two-pocket ledger**: quality-class (rules/billing/latency/Slop/test FAILs) can be decayed by TRUSTED test PASSes (floor-protected); identity-class (voiceprint JS / answer fingerprint) **cannot be laundered by any test result** — unfreezing takes either re-collecting references or an explicit operator ruling (`POST /satori/baseline/identity-cleared`).

**Breaker SOP**:

1. `breaker open` on dashboard/logs → that upstream×model is now 503-ing
2. Forensics: hit details in `logs/satori.log`; re-audit recordings and tool chains with `satori replay records/x.jsonl [--tool-traces state/tool_traces.jsonl]`
3. Confirmed false positive (e.g., the vendor legitimately changed something): issue an operator credential first (see [SECURITY.md](SECURITY.md)), then `POST /satori/breaker/reset {"upstream":"...","model":"..."}`
4. Confirmed official model swap: `satori feedback --upstream U --model M --reason official_update --confirm` to run retirement; confirmed fraud: switch upstreams, take the recordings to the refund dispute

## 5. Rule authoring guide

```toml
[[rules]]
name = "my-rule"          # same name as a built-in overrides it (retuning)
field = "reasoning"       # content | reasoning | any
target = "response"       # response | request (request-side usually negative-score exemptions)
match = "regex"           # contains | regex
pattern = "your pattern"
score = 20                # may be negative
description = "hit description"
```

Engineering advice:

- Several weak signals > one strong signal (false-positive resistance)
- Validate new regexes against samples via `satori replay` before production
- Keep identity-class strong signals at ≥25 points so they feed the hit-rate channel
- Exempt user-induced identity statements with negative-score `target = "request"` rules — see the built-in `roleplay-excuse`

## 6. Recording & replay forensics

- Record format (JSONL): `{ts, upstream, model, status, request_text, content, reasoning, usage}`
- Tool-chain format (`state/tool_traces.jsonl`): `{ts, upstream, model, session, trace_id, slop_score, first_suspicious, steps[{index, tool, args_valid, args_bytes, flags, score}]}` — replay prints each chain and its first mutated step; raw arguments are not persisted (privacy/size), derivable rules render as captured flags
- **Replay is the upgrade weapon**: new rules or thresholds today can re-audit all of yesterday's traffic — settle accounts later
- `satori ingest` converts markdown transcripts to record format (heuristic pairing; role attribution is approximate)
- Recordings contain raw conversation text: **guard the records/ directory** in sensitive settings (already .gitignored)

## 7. Threat model (Kerckhoffs's principle)

**Public algorithm, private key**: the detection code and built-in rules are fully open; security rests not on algorithm secrecy but on your private references, answer sets, custom exams, self-derived baselines — and control-plane credentials.

| Adversary move | Satori's answer |
|---|---|
| Full substitution | voiceprint + answer fingerprint + all side-channels |
| Dilution (90% real) | hit-rate channel + cumulative ledger + identity contradiction |
| Distilled persona | voiceprint (physical distribution) + answer fingerprint (answering habits) |
| Effort-reduction degradation | canary + **your own test suite** (degradation → capability drop → suite failures → breaker) |
| Equal-quality swap (same tier) | identity pocket: voiceprint/answer hits — test PASSes can't wash them |
| Cutting logprobs | graceful degradation to black-box channels (BASIC tier explicitly silences rather than accuses) |
| Forging usage | text-layer channels as backstop; forgery cost exceeds profit |
| Forged control-plane requests (fake DEGRADED / breaker reset / baseline retirement) | HMAC/Ed25519 credentials + role matrix + TLS front — the watchdog can't be switched off by a stranger |
| Reading the source to evade built-in rules | custom exams/rules + community rule iteration + signature-free self-baselines |

Known limitations are listed in the [Chinese Q&A](../zh/QA.md) — the ugly truths all live there.

## 8. Extension development

**Checkers** (periodic verification plugins):

```python
# checkers/mychecker.py
from ..registry import register_checker
from . import CheckResult

@register_checker
class MyChecker:
    name = "mine"

    @classmethod
    def from_config(cls, cfg):
        return cls() if cfg.identity.enabled else None  # None = disabled

    async def check(self, client, upstream, model):
        return CheckResult(self.name, upstream.name, model, True, 0.0, "ok")
```

**Adapters** (protocol plugins): drop a module into `adapters/` with `@register_adapter` and implement `to_canonical` / `from_canonical` / `translate_sse` / `finish_sse` — see `anthropic.py` (v2 includes two-way tool translation and streaming reassembly).

**Event protocol** (`ws /satori/live`): `snapshot` / `check` / `request` / `suspicion` / `level` / `alert` / `breaker` / `baseline` (retirement · unfreeze) / `ttl` (aging warnings) / `feedback` / `test` / `slop` (suspicion · confirmed) / `restored` (restart recovery), all JSON with `ts`. Subscribe to build your own dashboard or wire up alert bots.

Both registries auto-discover via package scan: **no existing file needs editing**.
