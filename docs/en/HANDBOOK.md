# Operations Handbook · HANDBOOK

> The full reference: configuration, internals, ops, and extension development. New here? Start with [QUICKSTART.md](QUICKSTART.md); common questions live in the [Chinese Q&A](../zh/QA.md).

[中文版](../zh/HANDBOOK.md)

## 1. Architecture

```
client ──▶ [protocol adapters adapters/] ──▶ canonical form (OpenAI Chat) ──▶ [detection core] ──▶ upstream (OpenAI-compatible)
          /v1/chat/completions (passthrough)     ├─ rule engine rules.py (every response)
          /v1/messages                           ├─ side-channels watch.py (tokenizer/billing/latency/hit-rate)
          /v1/responses                          ├─ periodic checkers checkers/ (voiceprint/answers/identity/canary)
                                                 ├─ suspicion ledger + 3 alert levels + circuit breaker app.py
                                                 ├─ recording record.py (daily JSONL)
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
| `[[upstreams]]` | `name` / `base_url` / `api_key` / `models` | — | upstreams; `api_key` supports `env:VAR` |
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
| `[logging]` | `level` / `file` | INFO / logs/satori.log | loguru (10MB rotation) |

## 3. Detection channels in depth

### 3.1 logprob voiceprint (fingerprint)

- **Principle**: the first-token top-logprobs distribution is a physical fingerprint of weights + tokenizer; compared against an official reference via JS divergence
- **Requires**: `satori collect` reference; upstream logprobs support
- **Catches**: substitution, silent quantization. **Misses**: effort reduction on identical weights (that's the canary's job)
- **Notes**: distributions drift slightly across snapshots/quantization — re-collect periodically; `--prompt` collects per-identity-question references

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

## 4. Alerts, decay & breaker ops

**Three levels**: `SAFETY` (<25) → `WATCH` (25–49, pay attention) → `DEGRADED` (≥50, alert + breaker).

**Decay**: each scoring event first decays the old score by the half-life, then adds. Meaning:

- Isolated small faults (a real model's occasional mannerism) → fade to zero, no wrongful convictions
- Sustained anomalies → scoring outpaces decay, levels climb anyway
- Auditing calls this the "materiality threshold": don't prosecute isolated slips, prosecute abnormal frequency

**Breaker SOP**:

1. `breaker open` on dashboard/logs → that upstream×model is now 503-ing
2. Forensics: hit details in `logs/satori.log`; re-audit recordings with `satori replay`
3. Confirmed false positive (e.g., the vendor legitimately changed something): `POST /satori/breaker/reset {"upstream":"...","model":"..."}`
4. Confirmed fraud: switch upstreams, take the recordings to the refund dispute

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
- **Replay is the upgrade weapon**: new rules or thresholds today can re-audit all of yesterday's traffic — settle accounts later
- `satori ingest` converts markdown transcripts to record format (heuristic pairing; role attribution is approximate)
- Recordings contain raw conversation text: **guard the records/ directory** in sensitive settings (already .gitignored)

## 7. Threat model (Kerckhoffs's principle)

**Public algorithm, private key**: the detection code and built-in rules are fully open; security rests not on algorithm secrecy but on your private references, answer sets, custom exams, and self-derived baselines.

| Adversary move | Satori's answer |
|---|---|
| Full substitution | voiceprint + answer fingerprint + all side-channels |
| Dilution (90% real) | hit-rate channel + cumulative ledger + identity contradiction |
| Distilled persona | voiceprint (physical distribution) + answer fingerprint (answering habits) |
| Effort-reduction degradation | canary + answer-fingerprint drift |
| Cutting logprobs | graceful degradation to black-box channels, never blindness |
| Forging usage | text-layer channels as backstop; forgery cost exceeds profit |
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

**Adapters** (protocol plugins): drop a module into `adapters/` with `@register_adapter` and implement `to_canonical` / `from_canonical` / `translate_sse` / `finish_sse` — see `anthropic.py`.

**Event protocol** (`ws /satori/live`): `snapshot` / `check` / `request` / `suspicion` / `level` / `alert` / `breaker`, all JSON with `ts`. Subscribe to build your own dashboard or wire up alert bots.

Both registries auto-discover via package scan: **no existing file needs editing**.
