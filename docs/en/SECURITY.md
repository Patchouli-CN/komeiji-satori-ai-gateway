# KomeijiSatori · Security Handbook

Operational documentation for the control-plane trust chain. Every control-plane
endpoint requires a credential; this handbook covers **a credential's life**,
**what to do when one leaks**, and **how to pipe test traffic in**.

## 1. Credential model: two orthogonal axes

| Axis | Governs | Values |
| :--- | :--- | :--- |
| **Trust** | whose PASS/FAIL carries weight | `TRUSTED` (PASS may decay, FAIL may trip breaker) / `NORMAL` (recorded, unweighted) / `UNVERIFIED` (recorded only) |
| **Role** | which actions may be taken | `reporter` (submit reports & feedback suggestions) → `operator` (confirm retirement, breaker reset, TTL lock, identity unfreeze) → `admin` (issue/revoke credentials) |

Roles are a **hierarchy**, not peer labels: operator implies reporter, admin
implies everything. Trust level grants no operational rights — even a `TRUSTED`
reporter cannot call `breaker/reset`.

## 2. Credential lifecycle

```
admin_secret (env:SATORI_ADMIN_SECRET, or the loopback bootstrap token)
    │
    ▼  POST /satori/admin/credentials
┌─────────┐  issue: {reporter_id, trust_level, roles, method, public_key?}
│ ISSUE   │  the plaintext credential is returned ONCE
│         │  (response carries Cache-Control: no-store)
└────┬────┘
     ▼
┌─────────┐  use: X-Satori-Reporter / Timestamp / Signature (/ Nonce)
│ VERIFY  │  hmac: HMAC-SHA256(secret, "<ts>.<raw body>"), 5-min window
│         │  ed25519: sign(canonical_json{reporter_id, ts, nonce, body_hash}),
│         │           nonce is single-use
└────┬────┘
      ▼
┌─────────┐  revoke: DELETE /satori/admin/credentials/{id}
│ REVOKE  │  record is RETAINED (status=revoked) for audit, effective immediately
└─────────┘
```

### Choosing a mode

- **Single user / localhost (default `hmac`)**: shared secret. The server must
  store the secret verbatim in SQLite (HMAC verification needs the key) — guard
  `state/credentials.db` permissions and keep it out of git.
- **Multi-team / cross-network (`ed25519`)**: asymmetric. The server stores only
  public keys; private keys never leave the reporter side. Revocation removes
  the registry entry with no symmetric material to rotate. Pair with
  `require_tls = true`.

Both modes coexist in one instance: `verify` dispatches per credential method.

## 3. Leak response

| Leaked | Action | Details |
| :--- | :--- | :--- |
| **reporter private key (ed25519)** | revoke + re-issue | `DELETE /satori/admin/credentials/{id}`, then issue with the new keypair. Accept a brief gap |
| **shared secret (hmac)** | revoke **all** reporters, re-issue each | No surgical option in hmac mode |
| **admin_secret** | rotate immediately | stop → change `env:SATORI_ADMIN_SECRET` → restart. Bootstrap tokens die with the process |
| **bootstrap token** | harmless | in-memory only; a restart invalidates it |

**General rule**: revoke first to stop the bleeding, re-issue to restore.
`state/feedback.jsonl` and `state/tool_traces.jsonl` keep their receipts —
malicious reports from the leak window remain auditable. If suspicion was
already injected, clear the ledger via `POST /satori/baseline/feedback`
(`false_alarm` + operator confirm).

## 4. TLS and reverse proxies

- `require_tls = null` (default): host-linked — **loopback exempt**,
  **non-loopback enforced**.
- `require_tls = true/false`: explicit. Control plane (`/satori/*`) only;
  proxy paths (`/v1/*`) are untouched — client-transparency is a design given.
- Reverse proxy: fill `trusted_proxies` (CIDR); only those sources have their
  `X-Forwarded-Proto` honored. Anyone else claiming https gets a 403.
- Credential issuance responses carry `Cache-Control: no-store`.

## 5. Trace ID propagation

Slop chain forensics accumulates suspicion per **session**. Make your tool
chains belong to one:

```
business client ── X-Satori-Session: <session id> ──▶ Satori ──▶ upstream
                                                   │
                             no header → per-request granularity (trace_id)
```

- **trace_id**: generated per forwarded request (`uuid4().hex[:16]`); lands in
  `state/tool_traces.jsonl`.
- **session_id**: declared by the client via the `X-Satori-Session` header;
  tool calls across turns share one session, so Slop accumulation spans requests.
- **OpenTelemetry compatible**: if you already run a trace system, pass the W3C
  `traceparent` trace-id as `X-Satori-Session` — Satori borrows its uniqueness
  without parsing OTel semantics. For the business → Tool Call → test chain,
  carry the same trace_id explicitly (`satori-test-report --suite <name>` with
  the `trace_id` field).

## 6. Wiring test reports

Environment variables (plugin / CLI / custom reporter alike):

| Variable | Purpose |
| :--- | :--- |
| `SATORI_URL` | gateway address e.g. `http://127.0.0.1:8400` (unset = silent skip) |
| `SATORI_REPORTER` | reporter_id (must match an issued credential) |
| `SATORI_SECRET` | hmac shared secret |
| `SATORI_METHOD=ed25519` + `SATORI_PRIVATE_KEY` / `SATORI_PRIVATE_KEY_FILE` | asymmetric mode |
| `SATORI_UPSTREAM` / `SATORI_MODEL` | report attribution (must exist in third_eye.toml) |
| `SATORI_QUEUE` | retry queue file (default `.satori-queue.jsonl`) |
| `SATORI_LEVEL` | pytest plugin default level (default L1) |

**Fault-tolerance semantics**: gateway unreachable or 5xx → local queue,
**tests never block, CI never goes red**; the next run flushes the queue and
delivered items leave it.

```bash
# pytest ecosystem
SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot SATORI_SECRET=… \
  pytest -p satori_gateway.pytest_plugin

# JUnit / TAP / JSON report files
satori-test-report results.xml [--format auto] [--suite core] [--level L0]

# local mock (validate reporting without a real gateway)
satori-test-mock --port 8401 [--secret …] [--reject]
```

Test levels: `L0` deterministic (N=3, PASS decay 10) / `L1` semantic (N=5,
decay 5) / `L2` complex reasoning (N=7, decay 2) / `L3` open-ended, unscored.
Marking: `@satori_test(level="L0")`.
