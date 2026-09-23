# 运维手册 · HANDBOOK

> 从配置到原理到二次开发的全量参考。快速上手先看 [QUICKSTART.md](QUICKSTART.md)，常见问题看 [QA.md](QA.md)，控制面安全看 [SECURITY.md](SECURITY.md)。

## 1. 架构总览

```
客户端 ──▶ [协议适配器 adapters/] ──▶ 内部规范(OpenAI Chat) ──▶ [检测核心] ──▶ [管线 pipelines/] ──▶ 上游(OpenAI 兼容 / Anthropic 原生)
           /v1/chat/completions(透传)        ├─ 规则引擎 rules.py（每条响应）
           /v1/messages                      ├─ 侧信道 watch.py（分词/计费/延迟/命中率）
           /v1/responses                     ├─ 周期 checker checkers/（声纹/答案/身份/金丝雀）
                                             ├─ 可疑度账本（质量+身份双组件）+ 三级等级 + 熔断 app.py
                                             ├─ 基线生命周期 baseline.py（trust/三档判别/退役归档）
                                             ├─ 动态 TTL ttl.py（学习中位数/老化预警）
                                             ├─ 测试裁决 testing.py（滑窗/flaky/两轴门控）
                                             ├─ Slop 链级审计 slop.py（change_trace/怀疑→实锤）
                                             ├─ 控制面安全 security.py（HMAC/Ed25519/角色/TLS）
                                             ├─ 状态持久化 state.py（账本/事件流水）
                                             ├─ 录制 record.py（JSONL 落盘 + 工具链回放）
                                             └─ 事件流 publish() → ws /satori/live → web/index.html
```

数据流要点：**所有检测都工作在规范格式上**。客户端说什么协议，适配器都先翻译成 OpenAI Chat 再进检测核心；响应同理。因此规则、侧信道、录制对协议零感知。

## 2. 配置参考（third_eye.toml）

| 段 | 键 | 默认 | 说明 |
|---|---|---|---|
| `[gateway]` | `host` / `port` | 127.0.0.1 / 8400 | 监听地址 |
| | `check_interval_seconds` | 300 | 周期 checker 间隔 |
| | `cors_origins` | `["*"]` | 面板跨域（本地场景） |
| | `inject_usage` | true | 流式请求自动补 `include_usage`（侧信道数据源） |
| `[[upstreams]]` | `name` / `base_url` / `api_key` / `models` / `protocol` | — | 上游；`api_key` 支持 `env:VAR`；`protocol` 默认 `openai`，可设 `anthropic` 直连原生 API（声纹通道仅 openai 协议可用） |
| `[fingerprint]` | `reference_dir` | fingerprints | 参考指纹目录 |
| | `probe_prompt` | "The capital of France is" | 声纹探针 |
| | `top_logprobs` | 10 | 分布候选数（≤20） |
| | `js_threshold` | 0.15 | JS 散度告警阈值 |
| `[answerprint]` | `enabled` / `similarity_threshold` / `max_tokens` | true / 0.6 / 128 | 答案指纹 |
| `[identity]` | `enabled` / `sample_size` / `max_tokens` | true / 5 / 64 | 身份题组（8 题随机抽 5） |
| `[[canary.cases]]` | `prompt` / `expect` | — | 金丝雀考题（**建议自定义**） |
| `[rules]` | `path` | rules.toml | 用户规则文件 |
| | `suspicion_threshold` | 50 | DEGRADED/熔断线 |
| | `watch_threshold` | 25 | WATCH 线 |
| | `decay_half_life_seconds` | 3600 | 账本衰减半衰期 |
| | `hit_rate_threshold` / `hit_rate_min_samples` | 0.05 / 50 | 命中率通道 |
| `[breaker]` | `enabled` | true | 熔断开关 |
| `[record]` | `enabled` / `directory` | false / records | 流量录制 |
| `[security]` | `enabled` / `mode` | true / hmac | 控制面鉴权；`mode` 可设 `ed25519`（多团队跨网） |
| | `timestamp_window_seconds` | 300 | HMAC 时间窗（防重放） |
| | `db` | state/credentials.db | 凭据库（**HMAC 存 secret 原文，管好权限**） |
| | `admin_secret` | env:SATORI_ADMIN_SECRET | Admin 口令；回环留空则首次启动生成一次性引导 token |
| | `require_tls` | null（按 host 联动） | 回环免 TLS、非回环强制；显式 true/false 覆盖 |
| | `trusted_proxies` | [] | 信任的反代 IP 段（CIDR），只采信其 `X-Forwarded-Proto` |
| `[state]` | `directory` | state | 审计状态目录 |
| | `debounce_seconds` | 1.0 | ledger 防抖落盘间隔 |
| | `archive_dir` | archive/baselines | 退役基线归档目录 |
| `[testing]` | `enabled` / `score_floor` | true / 5.0 | 业务测试裁决开关 / 负分地板（PASS 衰减的下限） |
| `[logging]` | `level` / `file` | INFO / logs/satori.log | loguru（10MB 轮转） |

### state/ 目录说明（重启不失忆的家当）

| 文件 | 内容 | 能删吗 |
|---|---|---|
| `ledger.json` | 账本/熔断快照（防抖落盘 + 原子替换） | 删了 = 账本清零重启（不推荐） |
| `feedback.jsonl` | 误报反馈流水（replay 可关联） | 删了 = TTL 少一份学习原料 |
| `baseline_events.jsonl` | 基线事件流水（retired/refreshed，TTL 数据源） | 删了 = TTL 回冷启动 30 天 |
| `test_reports.jsonl` | 测试上报流水（裁决引擎重启重建） | 删了 = 幂等集与滑窗重来 |
| `tool_traces.jsonl` | 工具链 trace 流水（`satori replay --tool-traces` 的原料） | 删了 = 丢回放取证 |
| `credentials.db` | 控制面凭据库（SQLite） | **绝对别删也别进 git** |
| `ttl_overrides.json` | 手动锁定的保质期 | 删了 = 解锁回归学习值 |

## 3. 检测通道详解

### 3.1 logprob 声纹（fingerprint）

- **原理**：探针首 token 的 top-logprobs 分布是权重+分词器的物理指纹；与官方参考算 JS 散度
- **前提**：`satori collect` 采参考；上游支持 logprobs
- **抓**：掉包、静默量化。**抓不到**：同权重砍 effort（那是金丝雀的活）
- **注意**：分布随快照/量化轻微漂移，参考定期重采；`--prompt` 可给身份题单独采
- **记账**：失败进**身份账本**，权重随三档判别折损（见 3.8）

### 3.2 答案指纹（answerprint）

- **原理**：8 题稳定型题组 temp=0 作答，与参考算 SequenceMatcher 相似度（LLMmap 思路）
- **前提**：`satori answers` 采参考。**纯黑盒**，Claude 系主力
- **抓**：掉包 + 降智。开放题会漂移，所以题组全是事实/格式/短推理题

### 3.3 身份题组（identity）

- **原理**：多语言多格式身份题随机抽样，聚合三重判据——厂商自报矛盾、自报 vs 模型名暗示不符、截止年漂移
- **抓**：套壳蒸馏的细节马脚、多源混合路由（矛盾检测天然抗掺水）
- **抓不到**：人设统一的蒸馏（交给声纹/答案指纹）

### 3.4 金丝雀（canary）

- **原理**：有标准答案的题，通过率即质量。**抗降智主力**
- **强烈建议**：把默认考题换成你自己的——考题私有是柯克霍夫原则的一部分

### 3.5 规则引擎（rules）

- **原理**：每条真实响应的正文/CoT 过 17 条内置 + 用户规则，命中记分
- 内置规则四档：伪装泄漏（50）、厂商自报（40）、代理自曝（25）、弱信号（3~15）+ 豁免（-40）
- **作用域**：`field: content/reasoning/any`；`target: response/request`

### 3.6 三条自基线侧信道（tokenwatch / billing / latency）

- **原理**：EMA 基线 + 连续越限才报。分词比例（chars/prompt_tokens）、计费比例（chars/completion_tokens）、首字节延迟
- **特点**：零额外请求、无公开特征可规避；对低比例掺水不敏感（防误报的有意取舍）

### 3.7 命中率通道（hitrate）

- **原理**：严重规则（≥25 分）命中率 EMA 超 5% 告警——"90% 真 10% 假"掺多少抓多少
- **实测**：10% 掺水稳定告警（7.2%），诚实上游 1% 偶发沉默

### 3.8 基线生命周期与三档判别（baseline.py + ttl.py）

**可信度公式**：`trust(t) = W_source × W_pressure × max(0, 1 − age/TTL)^γ`

- `W_source`：溯权权重——official 1.0 / secondhand 0.5 / community 0.3（缺失 sidecar 按 secondhand）。**来源先于时机**：二手端点采的"官方参考"只是一份漂亮的二手真相
- `W_pressure`：采集时压力——LOW/MID 1.0 / HIGH 0.6 / EXTR 0.0（按厂商本地时区的流量模式）
- `age/TTL`：TTL 由 `ttl.py` 从真实事件学习（中位数 × 0.9/0.7，钳制 [7d, 180d]）；冷启动固定 30 天；学习/锁定/预警都按 **上游×模型** 各自记账（官方与中转同名模型不串味）

**三档判别动作**（档次是油门，不是仪表读数）：

| 档次 | 条件 | 身份通道（声纹/答案指纹） | 说明 |
|---|---|---|---|
| `STRICT` | trust ≥ 0.8 | 满额记账（20/10） | 参考新鲜可信，对不上就是大罪 |
| `STANDARD` | 0.4 ≤ trust < 0.8 | **半额**记账（10/5） | 基线老化/来源降权，不按满贯信任老参考 |
| `BASIC` | 无参考 / 已退役 / trust < 0.4 | **闭嘴**——连探针都不发 | 只留黑盒通道（身份探针/金丝雀/规则/侧信道）；省 token 也不诬告 |

**退役闭环**：`POST /satori/baseline/feedback`（reporter 角色）提交 `official_update` 确认——reporter 累积 N=3（冷启动 1 次）或 operator ×1 → 参考归档到 `archive/baselines/`（含 meta.json 留痕）→ 该 上游×模型 降级 BASIC。`network_jitter`/`false_alarm` 只清账本不退役。**TTL 到期只点灯（70% 黄 / 90% 橙 / 100% 红），永不自动退役**——预测不误杀，退役必经地面真值。退役后重新 `collect`/`answers` 采集，退役标记自动失效（参考文件比退役时间新即忽略），恢复按可信度判别。

### 3.9 业务测试裁决（testing.py）——质量锚点

- **上报**：`POST /satori/test/report`（reporter 角色）；字段 `trace_id/test_suite/test_name/level/status/attempt/failure_diff/model_claimed/upstream`；幂等键 = reporter_id+trace_id+test_name+attempt（重启后从流水重建）——可预测的 trace_id 不会被抢先上报封杀
- **分级**：L0 确定性断言（N=3，注入 15，PASS 衰减 10）/ L1 语义等价（N=5，10，5）/ L2 复杂推理（N=7，8，2）/ L3 开放式不计分
- **滑窗**：最近 M=N×3 次里失败 ≥ N 次才触发；**flaky 自动标记**（失败率 >5% 且样本 ≥20 → unreliable 只记录不裁决）。滑窗/lifetime/flaky 全部按信任**分桶隔离**：TRUSTED 的窗口只被 TRUSTED 报告影响，低信任凭据投毒不了真判定
- **两轴门控**：信任轴（UNVERIFIED 仅落盘不进裁决；TRUSTED 的 PASS 才衰减、FAIL 才享"跨线优先"注入 DEGRADED 级嫌疑并可立即熔断；NORMAL 只加等级分值）× 信号轴（PASS 只洗质量类、洗不穿负分地板）
- **接入**：pytest 插件 `-p satori_gateway.pytest_plugin`（`@satori_test(level=)`）；非 pytest 生态 `satori-test-report results.xml`；凭据走环境变量（SATORI_URL/REPORTER/SECRET，见 SECURITY.md）

### 3.10 Tool Call 链级审计（slop.py）

- **change_trace**：每个带工具调用的响应落一条 trace（step/tool/args_valid/args_bytes/flags/score）到 `state/tool_traces.jsonl`——干净的链也留痕供回放
- **结构化评分**：断链 JSON（+30）/ 幻觉工具——调了请求里没声明的（+20）/ 同响应复读（+15）/ 参数膨胀 >8KB（+10）。**诚实的边界：不是语义判定**——"参数选错但 JSON 合法"要交给测试套件
- **三阶段**：怀疑（单步 score>0，广播事件）→ 实锤（同 session 累计 2 步 → 注入 DEGRADED 级**质量类**嫌疑 40 分，权重高于 Logprob，可被 L0/L1 PASS 部分衰减但不穿地板）→ 回溯（origin_trace + origin_step 高亮第一个"被污染的念头"）
- **前置**：adapter v2 已双向翻译 tools（含流式 input_json_delta 重组）；session 由客户端 `X-Satori-Session` 头声明（缺省按 `upstream/model` 归并，未实锤 session 跟踪上限 1000、最久不活动先淘汰）

## 4. 报警、衰减、三档与熔断运维

**三级警告（账本）**：`SAFETY`（<25）→ `WATCH`（25~49，关注）→ `DEGRADED`（≥50，告警+熔断）。
**三档动作（基线）**：`STRICT` → `STANDARD` → `BASIC`，见 3.8——警告决定**要不要罚**，档次决定**盯多紧**。

**衰减**：每次记账先把旧分按半衰期衰减再加新分。含义：

- 孤立小错（真模型偶发口癖）→ 自动归零，不冤案
- 持续异常 → 加分速度远超衰减，照样升级
- 审计学叫"重要性水平"：不追究孤立小错，只追频率异常

**双组件账本**：质量类（规则/计费/延迟/Slop/测试 FAIL）可被 TRUSTED 的测试 PASS 衰减（有地板）；身份类（声纹/答案指纹）**不可被任何测试结果洗白**——解冻只有两条路：重新 `satori collect`/`answers`，或 operator 显式裁决 `POST /satori/baseline/identity-cleared`。

**熔断 SOP**：

1. 面板/日志出现 `breaker open` → 该 上游×模型 已 503 停工
2. 取证：`logs/satori.log` 看命中详情；`satori replay records/x.jsonl [--tool-traces state/tool_traces.jsonl]` 重审录制与工具链
3. 确认是误报（比如上游官方真的更新了什么）：先签 operator 凭据（见 [SECURITY.md](SECURITY.md)），再 `POST /satori/breaker/reset {"upstream":"...","model":"..."}`
4. 确认是官方换模型：`satori feedback --upstream U --model M --reason official_update --confirm` 走退役流程；确认是实锤掉包：换上游，拿着录制文件去对线退款

## 5. 规则编写指南

```toml
[[rules]]
name = "my-rule"          # 与内置同名即覆盖（调分专用）
field = "reasoning"       # content | reasoning | any
target = "response"       # response | request（请求侧通常配负分豁免）
match = "regex"           # contains | regex
pattern = "你的模式"
score = 20                # 可为负
description = "命中说明"
```

工程建议：

- 多条弱信号 > 单条强信号（抗误报）
- 写正则先拿样本在 `satori replay` 上验证，别直接上生产
- 身份类强信号规则保持 ≥25 分，才能进命中率统计
- 用户诱导的身份表述用 `target = "request"` 负分豁免，参考内置 `roleplay-excuse`

## 6. 录制与回放取证

- 录制格式（JSONL）：`{ts, upstream, model, status, request_text, content, reasoning, usage}`
- 工具链格式（`state/tool_traces.jsonl`）：`{ts, upstream, model, session, trace_id, slop_score, first_suspicious, steps[{index, tool, args_valid, args_bytes, flags, score}]}`——回放打印每条链与第一个突变步；原始 arguments 不落盘（隐私与体量），可重导出的规则以捕获时 flags 呈现
- **回放是升级武器**：新规则/新阈值出来后 `satori replay` 重审全部历史，秋后算账
- `satori ingest` 可把 markdown 会话记录转录制格式（启发式配对，角色判别是估算）
- 录制含对话原文：**涉敏场景管好 records/ 目录权限**（已在 .gitignore 排除）

## 7. 威胁模型（柯克霍夫原则）

**算法公开，密钥保密**：检测代码与内置规则全部开源，安全性不依赖算法保密，而依赖你私有的参考指纹、参考作答、自定义考题、自基线，以及控制面凭据。

| 对手招式 | Satori 的应对 |
|---|---|
| 全面掉包 | 声纹 + 答案指纹 + 全部侧信道 |
| 掺水（90% 真） | 命中率通道 + 累积账本 + 身份矛盾检测 |
| 蒸馏人设 | 声纹（物理分布）+ 答案指纹（作答习惯） |
| 砍 effort 降智 | 金丝雀 + 你的测试套件（降级 → 能力下降 → 套件连败 → 熔断） |
| 同级狸猫换太子（质量不掉） | 身份账本：声纹/答案指纹测试 PASS 洗不掉 |
| 掐 logprobs | 降级到黑盒通道，不失明（BASIC 档显式闭嘴而非诬告） |
| 伪造 usage | 文本层通道兜底；伪造成本高于收益 |
| 伪造控制面请求（打 DEGRADED / 复位熔断 / 退役基线） | HMAC/Ed25519 凭据 + 三角色矩阵 + TLS 前置——看门狗关不掉了 |
| 读源码免杀内置规则 | 自定义考题/规则 + 社区规则迭代 + 无特征自基线 |

已知局限见 [QA.md](QA.md) 局限节——丑话都写在那了。

## 8. 二次开发

**checker**（周期核验插件）：

```python
# checkers/mychecker.py
from ..registry import register_checker
from . import CheckResult

@register_checker
class MyChecker:
    name = "mine"

    @classmethod
    def from_config(cls, cfg):
        return cls() if cfg.identity.enabled else None  # None = 禁用

    async def check(self, client, upstream, model):
        return CheckResult(self.name, upstream.name, model, True, 0.0, "ok")
```

**adapter**（协议适配插件）：`adapters/` 放模块，`@register_adapter` + 实现 `to_canonical` / `from_canonical` / `translate_sse` / `finish_sse`，参考 `anthropic.py`（v2 含 tools 双向翻译与流式重组）。

**事件协议**（`ws /satori/live`）：`snapshot` / `check` / `request` / `suspicion` / `level` / `alert` / `breaker` / `baseline`（退役·解冻）/ `ttl`（老化预警）/ `feedback` / `test` / `slop`（怀疑·实锤）/ `restored`（重启恢复），全部 JSON，带 `ts`。做自己的面板或接入告警机器人直接订阅即可。

两个注册表都是包扫描自动发现：**不需要改任何现有文件**。
