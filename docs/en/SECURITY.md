# KomeijiSatori · Security Handbook

> How the control plane decides who may speak, who may act, and what happens when a key leaks. [中文版](../zh/SECURITY.md)

覚「心の扉にも鍵を」 — even the door to the heart deserves a lock.

The detection side of Satori is public by design (Kerckhoffs: the algorithm is open; your references, exams and baselines are the keys). The control plane is the opposite problem: every `/satori/*` endpoint that isn't read-only can change an audit conclusion. A forged test FAIL fakes DEGRADED (denial of service). PASS spam washes suspicion (audit bypass). A forged `official_update` confirm makes the system close its own eyes — the most lethal of the three. So the watchdog requires credentials, and this document is the trust chain, end to end.

## 1. Two orthogonal axes

| Axis | Governs | Values |
| :--- | :--- | :--- |
| **TrustLevel** | whose PASS/FAIL carries weight in adjudication | `trusted` (PASS may decay suspicion, FAIL may trip the breaker) / `normal` (adjudicated in its own bucket, never decays, never trips) / `unverified` (persisted only, never adjudicated) |
| **Role** | which control-plane actions may be taken | `reporter` (submit test reports and feedback suggestions) ⊂ `operator` (confirm retirement, breaker reset, TTL lock, identity unfreeze) ⊂ `admin` (issue/revoke/list credentials) |

The axes are orthogonal on purpose. Trust answers "do we believe your testimony"; role answers "may you touch the controls". A `trusted` reporter still cannot reset a breaker, and an operator's FAIL carries no adjudication weight unless their trust level says so. Roles are a hierarchy, not peer labels: operator implies reporter, admin implies everything — otherwise the person allowed to retire a baseline couldn't submit the feedback that triggers retirement, which would be absurd.

Endpoint authorization matrix:

| Endpoint | Requirement |
| :--- | :--- |
| `POST /satori/test/report` | reporter + trust gating inside |
| `POST /satori/baseline/feedback` | reporter (operator confirms retire immediately) |
| `POST /satori/breaker/reset` | operator |
| `POST /satori/baseline/identity-cleared` | operator |
| `POST /satori/baseline/ttl/override` | operator |
| `GET/POST/DELETE /satori/admin/credentials[/{id}]` | `X-Satori-Admin-Secret` |

## 2. Signature byte layouts

Both modes share three headers — `X-Satori-Reporter`, `X-Satori-Timestamp`, `X-Satori-Signature` — plus a 300-second (configurable) timestamp window. Signatures cover the **raw request body bytes**, never a re-serialized form; JSON canonicalization differences are exactly where verification bugs live.

**HMAC mode** (default):

```
signature = hex(HMAC-SHA256(secret, "{ts}.{body}"))             # legacy form
signature = hex(HMAC-SHA256(secret, "{ts}.{nonce}.{body}"))     # with X-Satori-Nonce
```

The nonce form is optional; the server accepts both so existing clients keep working. With a nonce, in-window byte-for-byte replay is dead on arrival. Without one, high-risk endpoints get a fallback: feedback confirms are deduped on `(reporter_id, sha256(body))` within the window — a replayed confirm is logged as `confirm-deduped`, kept for the audit trail, but doesn't count toward retirement. Replay protection is layered precisely where replay would be profitable.

**Ed25519 mode** (`method = "ed25519"` at issuance, public key in PEM):

```
message   = canonical_json({reporter_id, timestamp, nonce, body_hash})
            # json.dumps(..., sort_keys=True, separators=(",",":")), UTF-8
            # body_hash = hex(SHA-256(body))
signature = hex(Ed25519_sign(private_key, message))
headers   = X-Satori-Reporter / Timestamp / Signature / Nonce (nonce mandatory, one-time)
```

Nonces (both modes share one 10,000-entry cache) are consumed **only after a signature verifies** — checking freshness without consuming first, so an attacker spraying garbage signatures can't fill the cache and DoS the protection. A full cache evicts oldest-first rather than flushing, because flushing would re-admit every old nonce still inside its window.

## 3. Credential lifecycle

```
admin_secret (env:SATORI_ADMIN_SECRET)  or  one-time bootstrap token
    │
    ▼  POST /satori/admin/credentials
┌─────────┐  {reporter_id, trust_level, roles, method, public_key?}
│ ISSUE   │  → 201 + plaintext secret, shown ONCE
│         │  (Cache-Control: no-store, X-Satori-Credential-Notice header)
└────┬────┘
     ▼
┌─────────┐  three headers (+ nonce), 5-minute window
│ VERIFY  │  hmac: shared secret  ·  ed25519: public key
└────┬────┘  last_seen_at / last_seen_ip recorded per use
     ▼
┌─────────┐  DELETE /satori/admin/credentials/{id}
│ REVOKE  │  record retained (status=revoked) for audit, effective now
└─────────┘
```

Storage truths worth knowing:

- **HMAC mode stores secrets verbatim** in `state/credentials.db`. HMAC verification needs the key; there is no hashing your way out of that. The store chmods the file 0600 at open and secrets never appear in list/get responses — the rest (backups, git, directory permissions) is on you.
- **Ed25519 mode stores only public keys.** Private keys never leave the reporter side; revocation just removes the registry entry, with no symmetric material to rotate. This is the mode for multi-team or cross-network deployments — pair it with `require_tls = true`.
- Re-issuing an existing active `reporter_id` is a 409: revoke first, then re-issue. Lost plaintext = revoke + re-issue; there is no recovery path by design.

**Bootstrap.** Loopback + no `admin_secret` configured → first start prints a one-time bootstrap token to **stderr** (never the log file, in memory only, dies with the process). Use it to issue your first operator credential. Non-loopback + no `admin_secret` → the gateway **refuses to start**. `security.enabled = false` disables auth entirely, starts with a loud warning, and grants everyone full admin — insecurity must be an explicit choice.

## 4. Leak response

| Leaked | Action |
| :--- | :--- |
| Ed25519 private key | revoke the credential, issue with the new keypair; accept a brief gap |
| HMAC shared secret | revoke and re-issue **all** reporters sharing exposure — no surgical option |
| `admin_secret` | stop → rotate `env:SATORI_ADMIN_SECRET` → restart |
| bootstrap token | nothing — in-memory only, restart kills it |

Revoke first to stop the bleeding, re-issue to restore. `state/feedback.jsonl` and `state/test_reports.jsonl` keep their receipts, so malicious submissions from the leak window remain auditable. If bad suspicion was already injected, clear it via signed feedback (`false_alarm` + operator confirm) or `breaker/reset`.

## 5. TLS posture

- `require_tls` unset: host-linked — **loopback exempt, non-loopback enforced**. Set `true`/`false` to override.
- Applies to `/satori/*` only. Proxy paths (`/v1/*`) are untouched; client transparency is a design given, and your clients' TLS to the gateway is your own deployment choice.
- Behind a reverse proxy, list its CIDRs in `trusted_proxies`; only those sources get their `X-Forwarded-Proto` honored. Anyone else claiming https gets a 403.
- Credential issuance responses carry `Cache-Control: no-store`.

## 6. Wiring test reporters

| Variable | Purpose |
| :--- | :--- |
| `SATORI_URL` | gateway address; unset = reporter silently skips |
| `SATORI_REPORTER` | reporter_id of an issued credential |
| `SATORI_SECRET` | HMAC shared secret |
| `SATORI_METHOD=ed25519` + `SATORI_PRIVATE_KEY` / `SATORI_PRIVATE_KEY_FILE` | asymmetric mode |
| `SATORI_UPSTREAM` / `SATORI_MODEL` | attribution (must exist in third_eye.toml) |
| `SATORI_QUEUE` | retry queue file (default `.satori-queue.jsonl`) |
| `SATORI_LEVEL` | pytest plugin default level (default L1) |

Fault tolerance is deliberate: gateway unreachable or 5xx → reports queue locally and the next run flushes; 4xx rejections are dropped as poison (retrying won't help). **Tests never block and CI never goes red because the observer is down** — `satori-test-report` exits 2 only when `SATORI_URL` is unset, 0 otherwise. `satori-test-mock --port 8401 [--secret …] [--reject]` gives you a fake gateway for developing reporters without a real one.

For slop session tracking, send `X-Satori-Session: <id>` from your client; without it, traces aggregate per upstream×model. If you already run OpenTelemetry, the W3C `traceparent` trace-id works fine as the session value — Satori borrows its uniqueness without parsing OTel semantics.

## 7. Fail-closed defaults

| Situation | Behavior |
| :--- | :--- |
| non-loopback, no `admin_secret` | refuses to start |
| loopback, no `admin_secret` | one-time bootstrap token, stderr only |
| `security.enabled = false` | starts with a loud warning; everyone is admin |
| missing/invalid signature headers | 401 |
| right credential, wrong role | 403 |
| TLS required but plain HTTP | 403 |
| `X-Forwarded-Proto` from an untrusted proxy | ignored (403 if that breaks TLS) |
| unknown `mode` / `trust_level` / role | config or request rejected |
| replayed nonce | rejected; garbage signatures don't consume nonce slots |
| replayed confirm body (no nonce) | logged as `confirm-deduped`, doesn't count |
| `[testing] enabled = false` | `/satori/test/report` returns 503 |

## 8. Threat-model boundaries — read this before exposing the port

The **read side is unauthenticated, deliberately**: `GET /satori/status`, `GET /v1/models`, the `ws://…/satori/live` event stream, and the dashboard are open to anyone who can reach the port. So are the proxy paths — any api_key works because Satori holds the real ones. This is the local-dashboard trade-off: the intended deployment is loopback (or behind your own reverse proxy with its own auth), where readability matters more than perimeter theater.

Concretely, anyone who can reach the port can:

- read the full audit state (ledger, baselines, checker details) and watch traffic events live;
- spend your upstream quota by proxying requests through the gateway.

What they **cannot** do without credentials: reset a breaker, inject or decay suspicion via test reports, retire a baseline, clear identity suspicion, lock a TTL, or touch credentials. The write side is the sealed one, because the write side is where audit conclusions change.

If that trade-off doesn't fit your network, put the gateway behind an authenticating reverse proxy and keep `trusted_proxies` tight. Don't just bind to 0.0.0.0 and hope.
