# 安全手册 · SECURITY

> 控制面信任链的全部细节：两轴模型、两种签名模式的精确格式、凭据的一生、重放防护、TLS、以及威胁模型的边界——包括我们**故意不防**什么。
> 操作入门见 [QUICKSTART.md](QUICKSTART.md) 第 7 节，机制背景见 [HANDBOOK.md](HANDBOOK.md)。

## 1. 为什么控制面要上锁

代理面（`/v1/*`）不需要网关认识客户端——真 key 由网关代持，客户端那个 `api_key` 字段网关不看。但控制面端点全是"能改变审计结论"的操作：

- 伪造 test report 打 DEGRADED → 对你的上游拒绝服务；
- 刷测试 PASS → 洗掉嫌疑分，审计绕过；
- 伪造 `official_update` 确认 → 让系统主动退役基线、闭眼——最致命的一种；
- 复位熔断器 → 把刚拉下的闸推回去。

看门狗不能谁都能关。所以 `/satori/*` 里除了读面（status / live），全部要凭据。

## 2. 两轴模型，以及为什么必须正交

| 轴 | 管什么 | 取值 |
|---|---|---|
| 信任轴 TrustLevel | 谁的 PASS/FAIL 有分量 | `trusted`（PASS 可衰减嫌疑、FAIL 可熔断）/ `normal`（FAIL 只加等级分值，PASS 不衰减）/ `unverified`（仅落盘留痕，不进裁决） |
| 角色轴 Role | 能动哪些操作 | `reporter`（提交 report / feedback）→ `operator`（确认退役、熔断复位、TTL 锁定、身份解冻）→ `admin`（凭据签发/吊销/查询） |

两轴分开是刻意的。信任分级解决"信不过的 reporter 不许说话"；维度隔离解决"信得过的 reporter 也不许拿质量维度的证词给身份维度作证"。合并成一个轴，总有一头能被绕过。

角色是**等级**不是平行标签：operator 天然是 reporter，admin 天然是一切（`admin ⊃ operator ⊃ reporter`）。信任等级不赋予任何操作权限——`trusted` 凭据照样碰不了 `breaker/reset`。

裁决状态按信任**分桶隔离**：TRUSTED 的滑窗 / flaky 判定只被 TRUSTED 报告影响，NORMAL 报告稀释不了真窗口，UNVERIFIED 根本不进裁决引擎。幂等键带 reporter_id——可预测的 trace_id 不会被抢先上报封杀。

## 3. 签名模式：精确到字节

两种模式可以在同一实例共存：不同 reporter 用不同 method，验签按凭据记录分派。

### 3.1 HMAC（默认，单人本地）

请求头三枚，可选第四枚：

```
X-Satori-Reporter:  <reporter_id>
X-Satori-Timestamp: <unix 秒，字符串>
X-Satori-Signature: HMAC-SHA256(secret, signed_bytes) 的 hex（小写）
X-Satori-Nonce:     <可选；带上则 nonce 参与签名且一次性>
```

被签名的字节布局：

```
不带 nonce:  "{ts}." + 原始请求体字节
带 nonce:    "{ts}.{nonce}." + 原始请求体字节
```

注意签的是**原始请求体字节**，不是反序列化后再序列化的 JSON——后者会因 canonical 化差异埋雷。客户端签名时直接用你要发的那串 bytes，服务端用收到的同一串 bytes 验。

时间窗默认 300 秒（`timestamp_window_seconds`），超窗直接拒。窗口内的逐字节重放靠两层：

1. **nonce**（推荐）：带 nonce 的请求，nonce 一次性——验签**通过后**才消费（验签前就占坑的话，攻击者用废签名灌满缓存就能把防护 DoS 掉）；缓存上限 10000 条，满了淘汰最旧的。重放直接 401。不带 nonce 的旧客户端照样接受，兼容存量。
2. **confirm 去重**（服务端兜底）：`baseline/feedback` 的 confirm 分支按 `(reporter, 请求体 sha256)` 在时间窗内去重——重放合法 confirm 刷不了计数、退役不了基线，响应 `action: confirm-deduped`，留痕但不计数。网关自带的 `satori feedback` CLI 不带 nonce，靠的就是这层。

签名不用自己写：`from satori_gateway.security import sign_request`（CLI、pytest 插件、面板共用这一个函数）。

**存储代价要说清**：HMAC 验证需要 key 原文，所以 `state/credentials.db` 里存的是 secret 本身。文件创建时自动 `chmod 0600`（Windows 上设置失败只告警，请手动管好权限）。secret 只在签发响应里出现一次，list / get 接口永不返回。

### 3.2 Ed25519（多团队跨网推荐）

签发时提交 reporter 侧的**公钥**（PEM），服务端只存公钥；私钥从不离开 reporter。请求头：

```
X-Satori-Reporter / X-Satori-Timestamp / X-Satori-Signature / X-Satori-Nonce（必填）
```

被签名的消息是 canonical JSON（`sort_keys`，分隔符 `,` 和 `:`，UTF-8）：

```json
{"body_hash":"<请求体 sha256 hex>","nonce":"<nonce>","reporter_id":"<id>","timestamp":"<ts>"}
```

签名是 Ed25519 原始签名的 hex。nonce 一次性（与 HMAC 共用同一个 nonce 缓存，跨模式重放同一 nonce 也拦），时间窗同样 300 秒。reporter_id、ts、nonce、signature 缺一即拒。

私钥泄露是 reporter 侧的事：吊销即从注册表移除，不牵动服务端存储的对称材料——这也是跨网场景推荐它的原因。配 `require_tls = true` 使用。签名辅助：`sign_request_ed25519(private_pem, reporter_id, body)`；生成密钥对：`generate_keypair()`。面板只走 HMAC，Ed25519 凭据请用 CLI / 插件。

## 4. 凭据的一生

```
admin_secret（env:SATORI_ADMIN_SECRET 或回环引导 token）
    │  X-Satori-Admin-Secret 头
    ▼  POST /satori/admin/credentials
  签发  {reporter_id, trust_level, roles, method?, public_key?}
    │  201 + 明文 secret——只出现这一次（Cache-Control: no-store）
    │  同名已有有效凭据 → 409，先吊销再重签
    ▼
  使用  三/四枚签名头；每次验签通过更新 last_seen_at / last_seen_ip
    ▼
  吊销  DELETE /satori/admin/credentials/{id} → 204
        status=revoked，立即失效；记录保留供审计追溯
```

Admin 接口由 `X-Satori-Admin-Secret` 把关，不走签名——它是签发凭据的凭据。

**引导 token**：host 是回环（127.0.0.1 / localhost / ::1）且没配 `admin_secret` 时，首次启动生成一次性 token，只打印到 stderr，不落日志文件，重启即失效。它是明文，所以它不进持久化通道。非回环 + 没配 `admin_secret` → **拒绝启动**，没有商量。

查询：`GET /satori/admin/credentials`（可按 `?status=active&trust_level=trusted` 过滤）、`GET /satori/admin/credentials/{id}`。返回的元数据永远不含 secret。

**凭据忘了 / secret 丢了**：没有找回接口，也不可能找回（list/get 不返回 secret）。吊销重签：`DELETE` 旧的，`POST` 签新的。admin_secret 忘了就去改环境变量重启。

## 5. TLS 前置

只作用于 `/satori/*` 控制面，代理路径 `/v1/*` 不受影响——客户端无感是设计前提。

- `require_tls` 不配（默认 null）：按 host 联动——回环免 TLS，非回环强制。
- 显式 `true` / `false` 覆盖。强制时非 HTTPS 一律 403。
- 反代场景：把代理 IP 段填进 `trusted_proxies`（CIDR，支持精确匹配），只有这些来源的 `X-Forwarded-Proto` 才采信；其余来源自称 https 一律 403。
- 凭据签发响应带 `Cache-Control: no-store`——明文凭据不许被任何中间层缓存。

## 6. fail-closed 默认值表

| 场景 | 默认行为 |
|---|---|
| 未配 `admin_secret` + 非回环 host | 拒绝启动 |
| 未配 `admin_secret` + 回环 | 打印一次性引导 token，Admin 接口仍有锁 |
| 缺签名头 / 验签失败 | 401 |
| 角色不足 | 403 |
| 时间戳超窗 / nonce 重放 / 凭据吊销 | 401 |
| 非 HTTPS（require_tls 生效时） | 403 |
| `security.enabled = false` | 控制面完全不鉴权，启动打醒目警告——不安全必须显式选择 |
| 验签失败的 nonce | 不消费，不占缓存坑位 |
| 重放的 confirm | `confirm-deduped`，留痕不计数 |
| testing 上报格式非法 / 目标不存在 | 400，毒丸不裁决 |
| testing 关闭（`enabled=false`） | 503 拒收——不开没有裁决的质量锚点 |

## 7. 泄露应急

| 泄露物 | 处置 |
|---|---|
| reporter 私钥（ed25519） | 吊销该 reporter，用新密钥对重签。接受短暂空窗 |
| 共享 secret（hmac） | 吊销**所有**受影响 reporter 逐个重签。hmac 没有"只废一个"的说法——服务端存的是原文 |
| admin_secret | 停服 → 轮换 `SATORI_ADMIN_SECRET` → 重启。引导 token 随之失效 |
| 引导 token | 重启即无害化（只在进程生命周期内有效） |

通用原则：先吊销止损，再补签恢复。`state/feedback.jsonl` 与 `state/test_reports.jsonl` 留着他帐——泄露窗口期内的恶意上报可事后审计。恶意 report 若已注入嫌疑分，用 operator 凭据 `POST /satori/baseline/feedback`（`false_alarm` + confirm）清零账本。

## 8. 测试上报接入（reporter 侧）

环境变量（pytest 插件 / `satori-test-report` / 自研 reporter 通用）：

| 变量 | 用途 |
|---|---|
| `SATORI_URL` | 网关地址，如 `http://127.0.0.1:8400`（未配置 = 静默跳过，退出码 2） |
| `SATORI_REPORTER` | reporter_id，须与签发的凭据一致 |
| `SATORI_SECRET` | HMAC 共享 secret |
| `SATORI_METHOD=ed25519` + `SATORI_PRIVATE_KEY` / `SATORI_PRIVATE_KEY_FILE` | 非对称模式 |
| `SATORI_UPSTREAM` / `SATORI_MODEL` | 上报归属的 上游×模型（须在 third_eye.toml 有配置） |
| `SATORI_QUEUE` | 失败补发队列文件（默认 `.satori-queue.jsonl`） |
| `SATORI_LEVEL` | pytest 插件默认级别（默认 L1） |

容错语义：5xx / 网络不可达 / 缺签名材料 → 入本地队列，**测试不中断，CI 不红**；重试时重新签名（时间戳会变）。**4xx 是客户端问题**（校验失败 / 凭据吊销）——丢弃并警告，永不入队，毒丸重试也不会成功。下次运行自动补发，交付即出队。

```bash
# pytest 生态
SATORI_URL=http://127.0.0.1:8400 SATORI_REPORTER=ci-bot SATORI_SECRET=… \
  pytest -p satori_gateway.pytest_plugin

# JUnit / TAP / JSON 报告文件（格式自动探测）
satori-test-report results.xml --suite core --level L0

# 本地 mock，开发/CI 验证上报逻辑，不接真网关
satori-test-mock --port 8401 [--secret …] [--reject] [--store reports.jsonl]
```

## 9. Trace / session 传播

Slop 链级审计按 session 累计"怀疑 → 实锤"。客户端用请求头 `X-Satori-Session: <会话 ID>` 声明归属；不声明则按 `上游×模型` 归并兜底。已有 OpenTelemetry 体系的话，把 W3C `traceparent` 的 trace-id 塞进来即可——Satori 不解析 OTel 语义，只借用其唯一性。

## 10. 威胁模型边界：我们防什么，不防什么

**防**：伪造控制面请求（改审计结论）、窗口外重放、窗口内逐字节重放（nonce）、confirm 刷计数（去重）、凭据泄露后的扩散（吊销 + 分桶）、明文凭据被中间层缓存（no-store）、带伤监听（非回环无 admin_secret 拒启动）。

**明示不防 / 取舍**：

- **读面无鉴权**。`GET /satori/status` 和 `WS /satori/live` 谁都能看——里面有账本分数、基线状态、每条请求的状态码与 token 计数（不含对话原文）。这是本地面板定位的取舍：单人回环场景里给读面上锁，只会逼用户把凭据贴进浏览器。要暴露到非回环，自己拿反代加一层鉴权。
- **HMAC 存 secret 原文**。这是 HMAC 的数学性质，不是实现偷懒。介意就用 Ed25519。
- **代理面不认识客户端**。谁连上 `/v1/*` 都能用你代持的上游 key 发请求。回环部署这是特性；要绑非回环，反代加鉴权，别把代理面裸奔到公网。
- **面板密钥只存内存**。`web/index.html` 里 admin 口令和 operator secret 只活在 JS 变量里，刷新即消失——防的是持久化泄露，代价是每次刷新重输。网关地址反而存 localStorage（它不是秘密）。
- **它不防上游本身**。Satori 能告诉你货不对，不能替你拿到真货。取证、对线、换家，那是你的活。
