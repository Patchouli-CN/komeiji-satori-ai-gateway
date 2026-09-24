# KomeijiSatori · 古明地覚

> The third eye doesn't blink. An AI gateway that reads your upstream's mind.

[中文文档](docs/zh/README.md) · [快速上手](docs/zh/QUICKSTART.md) · [运维手册](docs/zh/HANDBOOK.md) · [安全手册](docs/zh/SECURITY.md) · [Q&A 中文](docs/zh/QA.md)

**English docs**: [Quickstart](docs/en/QUICKSTART.md) · [Handbook](docs/en/HANDBOOK.md) · [Security Handbook](docs/en/SECURITY.md) · [Q&A](docs/en/QA.md)

## Should you use this?

If you buy API access from a reseller, a relay, or any middleman — yes, that's exactly who this is for. That market is a mess: "Claude" endpoints quietly serving small open models, traffic rerouted to weaker models at peak hours, the same model name getting dumber month over month. The vendor holds all the data. You get a `model` field in the response, and that field is worth nothing.

KomeijiSatori sits between your clients and your upstreams as a multi-protocol gateway (OpenAI Chat, Anthropic Messages, OpenAI Responses). Clients point at it and notice nothing. Behind the proxy, every response is cross-examined: is this still the model it used to be, and is it still as good as it was?

覚「嘘は口に、本音はトークンに」 — lies live in the words; the truth lives in the tokens.

## How it works

Traffic flows through protocol adapters into a canonical form, past the detection core, and out through an upstream pipeline. Detection is transparent to the client until the circuit breaker decides it shouldn't be.

```
client ──▶ [adapter] ──▶ canonical ──▶ [detection core] ──▶ [pipeline] ──▶ upstream
/v1/chat/completions                   rules · side-channels · ledger      OpenAI-compatible
/v1/messages                           checkers · slop · tests             or native Anthropic
/v1/responses                          records · replay · event stream
```

No single channel is trusted, and no single channel is a silver bullet. Each one assumes a different level of vendor cooperation, so losing any one field (say, logprobs) is degradation, not blindness. Vendors can cut off fields; they can't cut off behavior.

| Channel | Needs from vendor | What it catches |
|---|---|---|
| logprob voiceprint (`collect`) | one optional field | model substitution — distributions can't be faked |
| answer fingerprint (`answers`) | nothing | substitution + degradation; pure black-box, primary weapon for Claude-family endpoints |
| identity probe battery | nothing | self-report contradictions, knowledge-cutoff drift |
| canary exams | nothing | capability pass-rate drift (degradation) |
| rule engine (17 builtin + `rules.toml`) | nothing | mannerisms, disguise-prompt leaks, CoT language slips |
| tokenizer / billing / latency side-channels | an unforged `usage` | tokenizer swap, token padding, infra signature drift — self-baselining, zero extra requests |
| severe-rule hit-rate | nothing | the "90% real, 10% fake" dilution tactic |
| slop chain forensics | nothing | tool calls with broken JSON args, hallucinated tools, repeats — how degraded models pollute downstream systems |
| your own test suite | nothing | "can it still do the job" — the one thing a vendor can't forge |

Everything lands in one suspicion ledger per upstream×model — actually two pockets: quality-class hits can be decayed by trusted test PASSes; identity-class hits (voiceprint, answer fingerprint) cannot be washed by any test result. The ledger decays with a half-life, so an honest upstream's occasional mannerism fades to zero while sustained watering keeps accumulating. Cross the line and the circuit breaker 503s that upstream×model until a human resets it.

When rules improve, yesterday gets re-audited: record traffic to daily JSONL, then `satori replay` it with today's rules. And on a DEGRADED crossing, the log line ends with 覚「想起うさぎは警戒を」 — the remembrance rabbit has been warned. Some traditions are worth keeping.

## The three tiers, and why

References age. An answer fingerprint collected from the official endpoint today is a strong accusation; the same file six months later is a rumor. So each baseline carries a trust score — provenance × collection pressure × time decay — and the score sets the monitoring intensity:

- **STRICT** (trust ≥ 0.8): fresh official reference. Identity channels at full weight — a mismatch is a serious charge.
- **STANDARD** (0.4–0.8): aged or second-hand reference. Identity channels at half weight — don't spend full-trust accounting on an old rumor. The author's own stance: if the dish is good, I respect your supply chain — but I'm still counting the ingredients.
- **BASIC** (no reference / retired / trust < 0.4): the third eye closes for identity — those channels aren't even probed. Black-box channels (rules, side-channels, canary, tests) stay on duty. It saves tokens and never accuses unjustly.

Baselines expire only through human ground truth (`satori feedback … --confirm`), never through a timer. Predictions light lamps; they don't swing blades.

## Quick start

```bash
uv venv .venv && uv pip install -e .

# collect references from an official endpoint
.venv/Scripts/satori collect --upstream openai --model gpt-4o --source official
.venv/Scripts/satori answers --upstream openai --model gpt-4o

.venv/Scripts/satori serve
```

Then point your client's base_url at `http://127.0.0.1:8400/v1` (any api_key — Satori holds the real one) and open `web/index.html` in a browser. The ten-minute version, with expected output at every step, is [QUICKSTART.md](docs/en/QUICKSTART.md).

## Honest limitations

- Identity self-reports can be induced by role-play. That's why single hits never convict — signals must stack, and role-play earns an exemption.
- Anthropic's official API exposes no logprobs. The voiceprint channel is dead there; answer fingerprinting is the primary channel, or collect a `--source secondhand` voiceprint from someone you trust.
- A distilled model with a fully consistent persona beats the content channels. The voiceprint is the last line of defense — collect references before you need them.
- Slop scoring is structural, not semantic: broken JSON is caught, "valid JSON, wrong parameter choice" is your test suite's job.
- Satori never proves "this is the real model." It proves "this is no longer the model it used to be." Behavioral fingerprints are probabilistic; the defense is the stack, not the layer.

## Docs

- [QUICKSTART.md](docs/en/QUICKSTART.md) — running in ten minutes, control plane included
- [HANDBOOK.md](docs/en/HANDBOOK.md) — mechanism reference: channels, ledger, baseline lifecycle, state files, extension points
- [SECURITY.md](docs/en/SECURITY.md) — control-plane trust model, credentials, signatures, TLS
- [QA.md](docs/en/QA.md) — real questions, including the ugly truths

## Naming

Komeiji Satori, from *Touhou Chireiden* (Subterranean Animism) — a satori youkai famed for mind-reading, seeing through every facade straight to the heart. Whatever the vendor serves up, one glance tells the truth.

## License

[MIT](LICENSE)
