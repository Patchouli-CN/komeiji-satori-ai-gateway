# 觉之瞳 · 安全手册（Security Handbook）

控制面信任链的操作文档。所有端点需要凭据；本手册讲清**凭据的一生**、
**泄露了怎么办**、以及**测试流量怎么接上来**。

## 1. 凭据模型：两轴正交

| 轴 | 管什么 | 取值 |
| :--- | :--- | :--- |
| **信任轴** | 谁的 PASS/FAIL 有分量 | `TRUSTED`（PASS 可衰减、FAIL 可熔断）/ `NORMAL`（仅记录不计权）/ `UNVERIFIED`（仅记录） |
| **角色轴** | 能动哪些操作 | `reporter`（提交上报与反馈建议）→ `operator`（确认退役、熔断复位、TTL 锁定、身份解冻）→ `admin`（凭据签发/吊销） |

角色是**等级**不是平行标签：operator 天然是 reporter，admin 天然是一切。
信任等级不赋予任何操作权限——`TRUSTED` 也一样不能碰 `breaker/reset`。

## 2. 凭据生命周期

```
admin_secret（env:SATORI_ADMIN_SECRET，或回环首次启动的引导 token）
    │
    ▼  POST /satori/admin/credentials
┌─────────┐  签发：{reporter_id, trust_level, roles, method, public_key?}
│ 签发     │  明文凭据**只返回一次**（响应带 Cache-Control: no-store）
└────┬────┘
     ▼
┌─────────┐  使用：X-Satori-Reporter / Timestamp / Signature(/ Nonce)
 │ 验签    │  hmac：HMAC-SHA256(secret, "<ts>.<原始请求体>")，5 分钟窗
 │         │  ed25519：sign(canonical_json{reporter_id, ts, nonce, body_hash})，nonce 一次性
 └────┬────┘
      ▼
┌─────────┐  吊销：DELETE /satori/admin/credentials/{id}
 │ 吊销    │  记录**保留**（status=revoked）供审计追溯，立即失效
 └─────────┘
```

### 两种模式怎么选

- **单人本地（默认 `hmac`）**：共享 secret。服务端 SQLite 必须存 secret 原文
  （HMAC 验证需要 key）——管好 `state/credentials.db` 的文件权限，别进 git。
- **多团队跨网络（`ed25519`）**：非对称。服务端只存公钥，私钥从不离开 reporter；
  吊销即从注册表移除，不牵动服务端的对称材料。配 `require_tls = true` 使用。

同一实例可以两种模式共存：不同 reporter 用不同 method，`verify` 按凭据分派。

## 3. 泄露应急

| 泄露物 | 处置 | 命令/动作 |
| :--- | :--- | :--- |
| **reporter 私钥（ed25519）** | 吊销该 reporter 重签 | `DELETE /satori/admin/credentials/{id}` → 用新密钥对重新签发。接受短暂空窗期 |
| **共享 secret（hmac）** | 吊销**所有** reporter 逐个重签 | 逐个 `DELETE` + 重新签发。hmac 模式下没有"只废一个"的办法 |
| **admin_secret** | 立即轮换 | 停服 → 改 `env:SATORI_ADMIN_SECRET` → 重启。引导 token 一次性失效 |
| **引导 token 泄露** | 无害化 | 重启即失效（只在进程生命周期内有效） |

**通用原则**：先吊销止损，再补签恢复；`state/feedback.jsonl` 与
`state/tool_traces.jsonl` 留着他帐——泄露窗口期内的恶意上报可事后审计。
恶意 report 若已注入嫌疑分，用 `POST /satori/baseline/feedback`
（`false_alarm` + operator 确认）清零账本。

## 4. TLS 与反向代理

- `require_tls = null`（默认）：按 host 联动——**回环免 TLS**，**非回环强制**。
- `require_tls = true/false`：显式指定。只影响 `/satori/*` 控制面，代理路径
  （`/v1/*`）不受影响——客户端无感知是设计前提。
- 反向代理：把代理 IP 段填进 `trusted_proxies`（CIDR），只有这些来源的
  `X-Forwarded-Proto` 才采信；其余来源自称 https 一律 403。
- 凭据签发响应带 `Cache-Control: no-store`——明文不许被任何中间层缓存。

## 5. Trace ID 传播规范

Slop 链级审计按 **session** 累计怀疑→实锤。让工具链归属于同一个 session：

```
业务客户端 ── X-Satori-Session: <会话 ID> ──▶ Satori ──▶ 上游
                                                │
                              session 无声明时按请求粒度（trace_id）算
```

- **trace_id**：网关为每个转发的请求生成（`uuid4().hex[:16]`），进入
  `state/tool_traces.jsonl`。
- **session_id**：客户端请求头 `X-Satori-Session` 声明；同一会话的多轮
  工具调用共享一个 session，Slop 累计跨请求生效。
- **OpenTelemetry 兼容**：若你已有 trace 体系，把 W3C `traceparent` 的
  trace-id 作为 `X-Satori-Session` 传入即可——Satori 不解析 OTel 语义，
  只借用其唯一性。测试执行链的打通：业务请求 → Tool Call → 测试上报共用
  同一 trace_id（`satori-test-report --suite <name>` 时以 `trace_id` 字段显式
  携带）。

## 6. 测试上报接入

环境变量（插件 / CLI / 自研 reporter 通用）：

| 变量 | 用途 |
| :--- | :--- |
| `SATORI_URL` | 网关地址，如 `http://127.0.0.1:8400`（未配置 = 静默跳过） |
| `SATORI_REPORTER` | reporter_id（须与签发的凭据一致） |
| `SATORI_SECRET` | hmac 共享 secret |
| `SATORI_METHOD=ed25519` + `SATORI_PRIVATE_KEY` / `SATORI_PRIVATE_KEY_FILE` | 非对称模式 |
| `SATORI_UPSTREAM` / `SATORI_MODEL` | 上报归属的上游×模型（须在 third_eye.toml 有配置） |
| `SATORI_QUEUE` | 失败补发队列文件（默认 `.satori-queue.jsonl`） |
| `SATORI_LEVEL` | pytest 插件默认测试级别（默认 L1） |

**容错语义**：Satori 不可达或报 5xx → 入本地队列，**测试不中断、CI 不红**；
下次运行自动补发，交付即出队。

```bash
# pytest 生态
SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot SATORI_SECRET=… \
  pytest -p satori_gateway.pytest_plugin

# JUnit / TAP / JSON 报告文件
satori-test-report results.xml [--format auto] [--suite core] [--level L0]

# 本地 mock（开发/CI 验证上报逻辑，不接真网关）
satori-test-mock --port 8401 [--secret …] [--reject]
```

测试级别：`L0` 确定性断言（N=3，PASS 衰减 10）/ `L1` 语义等价（N=5，衰减 5）/
`L2` 复杂推理（N=7，衰减 2）/ `L3` 开放式不计分。标记：
`@satori_test(level="L0")`。
