# KomeijiSatori · 古明地覚

> The Third Eye sees through everything — an AI gateway that reads your upstream's true colors

[中文文档](docs/README.zh-CN.md) · [Q&A (中文)](docs/QA.md)

The AI API relay market is a mess: "Claude" endpoints secretly serving small open-source models, silent routing to weaker models under load, the same model name quietly getting dumber over time. The vendor holds all the data; you get a single `model` field — and that field is worthless.

KomeijiSatori is a multi-protocol AI gateway (OpenAI Chat, Anthropic Messages, OpenAI Responses) that sits between your clients and your upstreams. **Clients plug in without noticing anything; Satori watches every drop of traffic behind the scenes.**

## Design Manifesto

**Detect model routing and degradation without depending on vendor cooperation.**

Every detection channel assumes a different level of cooperation, so losing any single field is degradation, not blindness — vendors can cut off fields, but they can't cut off behavior:

| Channel | Cooperation needed | Catches |
|---|---|---|
| logprob voiceprint fingerprint | one optional field | model substitution (distributions can't be faked) |
| Answer fingerprint (LLMmap-style) | none | substitution / degradation (pure black-box; primary channel for Claude-family endpoints) |
| usage tokenizer side-channel | unforged `usage` | tokenizer swap (self-baselining, zero extra requests) |
| Billing consistency audit | unforged `usage` | token padding / billing skimming (GatewayBench-style) |
| Identity probe battery | none | vendor self-report contradictions / knowledge-cutoff drift |
| Canary exams | none | degradation (capability pass-rate drift) |
| Custom rule engine | none | mannerisms, disguise-prompt leaks, CoT language slips |
| Latency profile drift | none | infrastructure signature drift (self-baselining) |
| Record + replay forensics | none | audit yesterday's traffic with today's rules |

## Quick Start

```bash
uv venv .venv
uv pip install -e .

# Edit third_eye.toml with your upstreams (supports env:VAR for keys)
# Collect reference fingerprints from an official endpoint (required for the voiceprint channel)
.venv/Scripts/satori collect --upstream openai --model gpt-4o
# Collect reference answers for the black-box answer-fingerprint channel
.venv/Scripts/satori answers --upstream openai --model gpt-4o

# Ignite
.venv/Scripts/satori serve
```

Then:

- Point your clients' base_url at `http://127.0.0.1:8400/v1` — traffic is watched automatically
- Open `web/index.html` in a browser — the live dashboard (suspicion ledger / check results / event stream; the Third Eye blinks)

## CLI

```bash
satori serve                                   # start the gateway
satori collect --upstream U --model M [--prompt P]   # collect reference fingerprints
satori answers --upstream U --model M          # collect reference answers
satori replay records/2026-09-18.jsonl         # replay & audit recorded traffic
satori ingest transcript.md --out records/x.jsonl    # convert markdown transcripts to record format
```

## How It Works (brief)

- **Forwarding**: routes by `model` to the configured upstream; detection is fully transparent to the traffic
- **Suspicion ledger**: every channel's hits flow into one ledger per upstream×model; crossing the threshold raises real-time alerts — role-play requests from users earn negative-score exemptions, no friendly fire
- **Circuit breaker**: when enabled (`[breaker] enabled = true`), a tripped upstream×model gets 503'd on every subsequent request — when the flavor changes, work stops in real time so low-quality output never pollutes your project. Reset manually via `POST /satori/breaker/reset`
- **Rule system**: 17 built-in rules (vendor self-reports, disguise leaks, mannerisms, exemptions); `rules.toml` holds your own, same-name rules override built-ins. Rules support `target: request` (inspect the user request, for exemptions) and `field: reasoning` (audit CoT specifically)
- **Pluggable checkers**: `@register_checker` decorator + package-scan auto-discovery (Spring `@ComponentScan`-style) — drop a module into `checkers/` and it's on duty
- **Live push**: `ws://…/satori/live` broadcasts check/request/suspicion/alert/breaker events; `GET /satori/status` for the current ledger
- **Record & forensics**: `[record] enabled = true` writes traffic to daily JSONL; `satori replay` re-audits it offline — upgrade rules today, settle accounts with yesterday

## Multi-Protocol Architecture

```
client ──▶ [protocol adapter] ──▶ canonical form (OpenAI Chat) ──▶ [detection core] ──▶ upstream (OpenAI-compatible)
          /v1/messages                                    rules · side-channels · ledger
          /v1/responses                                   records · replay · event stream
          /v1/chat/completions (passthrough)
```

**Vendor-agnostic on the client side**: Anthropic Messages, OpenAI Responses, and OpenAI Chat entry protocols are translated into the canonical form by adapters in `adapters/`; the detection core knows nothing about protocols. New protocol = one module + `@register_adapter`, auto-discovered by package scan (same pattern as checkers).

**Deliberately OpenAI-compatible on the upstream side**: our investigation targets (relay resellers, repackers) all speak this protocol to stay client-compatible — speaking the same language is itself camouflage. Native Anthropic/Gemini upstreams join via compatibility layers.

Adapter v1 scope: text & image content, system/instructions, streaming event translation. Tools / function calling / thinking blocks are not yet translated (detection still works; tool-dependent clients take note).

## Configuration

Every section of `third_eye.toml` is commented: `[gateway]` (listen/CORS/usage injection), `[[upstreams]]`, `[fingerprint]` (voiceprint), `[answerprint]` (answer fingerprint), `[identity]` (identity battery), `[canary]` (exams), `[rules]` (rules & alert threshold), `[breaker]` (circuit breaker), `[record]` (recording), `[logging]`.

## Honest Limitations

- Vendor self-report channels audit "testimony" and can be induced by role-play — that's why no single hit crosses the alert line; signals must stack
- Anthropic's official API exposes no logprobs, so verifying Claude-family endpoints needs "second-hand references" or leans on behavioral channels (the answer fingerprint is now the primary weapon there)
- When a distilled model's persona is fully consistent, content channels miss it; the logprob channel is the last line of defense — collect reference fingerprints before going live
- No single channel is a silver bullet; the entire point of this project is defense in depth

## FAQ & Troubleshooting

See [docs/QA.md](docs/QA.md) (Chinese) for principles, costs, false positives, extension guides, and limitations.

Windows users: pasting Chinese into `curl -d` in the console gets mangled to GBK and returns 400 — write the JSON to a file and use `--data-binary @req.json`.

## Naming

Komeiji Satori, from *Touhou Chireiden* (Subterranean Animism) — a satori youkai famed for mind-reading, seeing through every facade straight to the heart. Whatever the vendor serves up, one glance from Satori tells the truth.

## License

[MIT](LICENSE)
