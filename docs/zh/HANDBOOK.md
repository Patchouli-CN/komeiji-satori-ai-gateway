# 运维手册 · HANDBOOK

> 机制参考：每条通道怎么判、每个配置键干什么、账怎么记、文件删了亏什么。
> 跑起来看 [QUICKSTART.md](QUICKSTART.md)，控制面凭据看 [SECURITY.md](SECURITY.md)，遇到问题看 [QA.md](QA.md)。

## 1. 架构

```
客户端 ──▶ [adapters/] ──▶ 内部规范 ──▶ [检测核心] ──▶ [pipelines/] ──▶ 上游
          /v1/chat/completions(透传)  (OpenAI Chat)    ├─ 规则引擎 rules.py        每条响应
          /v1/messages                                ├─ 侧信道 watch/tokenwatch  每条响应
          /v1/responses                               ├─ 周期 checker checkers/   每 check_interval 一轮
                                                      ├─ 双账本 + 三级等级 + 熔断 app.py
                                                      ├─ 基线生命周期 baseline.py + 动态 TTL ttl.py
                                                      ├─ 测试裁决 testing.py / Slop 审计 slop.py
                                                      ├─ 控制面 security.py（HMAC/Ed25519/角色/TLS）
                                                      ├─ 持久化 state.py / 录制 record.py
                                                      └─ 事件流 → ws /satori/live → web/index.html
```

两条设计决定了一切：

1. **所有检测工作在规范格式上**。客户端说 Anthropic Messages 或 OpenAI Responses，适配器先翻译成 OpenAI Chat 再进检测核心；响应回来再翻回去。规则、侧信道、录制对协议零感知。所以上游是 `protocol = "anthropic"` 时检测照常——只是声纹通道因为拿不到 logprobs 自动跳过。
2. **转发与检测互不挡路**。客户端协议和上游协议都是规范本身时走透传快车道（逐字节转发）；要翻译才攒全量。检测挂在收尾阶段（`_finalize`），不增加首字节延迟。

周期 checker 按注册表装配：`@register_checker` 装饰器 + 包扫描，新增 checker 把模块放进 `checkers/` 就自动上岗。适配器（`adapters/`）和上游管线（`pipelines/`）同款套路。

## 2. 配置参考（third_eye.toml）

只列键和默认值，语义在后面的通道章节。样例文件里每个键都有注释，那是第一手资料。

| 段 | 键 | 默认 | 说明 |
|---|---|---|---|
| `[gateway]` | `host` / `port` | `127.0.0.1` / `8400` | 监听地址 |
| | `check_interval_seconds` | `300` | 周期核验间隔（真实 API 调用，烧钱节奏） |
| | `cors_origins` | `["*"]` | 面板跨域 |
| | `inject_usage` | `true` | 流式请求自动补 `stream_options.include_usage`，usage 侧信道的数据源 |
| `[[upstreams]]` | `name` / `base_url` / `api_key` / `models` | — | `api_key` 支持 `env:VAR`。路由按 `model` 匹配 `models`，都没匹配上时兜底用第一个上游 |
| | `protocol` | `"openai"` | 上游协议插件；`"anthropic"` 直连 Anthropic 原生 API。**声纹通道仅 openai 协议可用** |
| `[fingerprint]` | `reference_dir` | `fingerprints` | 参考指纹目录 |
| | `probe_prompt` | `"The capital of France is"` | 声纹探针 prompt |
| | `top_logprobs` | `10` | 分布取前 N 个 token |
| | `js_threshold` | `0.15` | JS 散度超过即判"不是同一个模型" |
| `[answerprint]` | `enabled` | `false`（样例里开了） | 答案指纹开关 |
| | `similarity_threshold` / `max_tokens` | `0.6` / `128` | 平均相似度低于阈值判异常 |
| `[identity]` | `enabled` | `false`（样例里开了） | 身份探针开关。**会产生真实 API 费用** |
| | `sample_size` / `max_tokens` | `5` / `64` | 每轮从 8 题里随机抽几题 |
| `[[canary.cases]]` | `prompt` / `expect` | 样例两道 | 金丝雀考题，配了才启用。**建议换成你自己的私有题** |
| `[rules]` | `path` | `rules.toml` | 用户规则文件 |
| | `suspicion_threshold` | `50` | DEGRADED / 熔断线 |
| | `watch_threshold` | `25` | WATCH 线 |
| | `decay_half_life_seconds` | `3600` | 账本半衰期 |
| | `hit_rate_threshold` / `hit_rate_min_samples` | `0.05` / `50` | 命中率通道 |
| `[breaker]` | `enabled` | `false`（样例里开了） | 熔断开关 |
| `[record]` | `enabled` / `directory` | `false` / `records` | 流量录制（对话原文落盘，涉敏慎用） |
| `[security]` | — | — | 控制面整段见 [SECURITY.md](SECURITY.md) |
| `[state]` | `directory` | `state` | 审计状态目录，别进 git |
| | `debounce_seconds` | `1.0` | 账本防抖落盘间隔 |
| | `archive_dir` | `archive/baselines` | 退役基线归档 |
| `[testing]` | `enabled` / `score_floor` | `true` / `5.0` | 测试裁决开关 / PASS 衰减的负分地板 |
| `[logging]` | `level` / `file` | `INFO` / 空（只控制台） | 样例里写了 `logs/satori.log`；文件 10 MB 轮转。完整堆栈：`LOGURU_FULL_TRACEBACK=1` |

## 3. 检测通道

先厘清一件旧文档含混带过的事：**不是所有通道都往账本记分**。

- 每条真实响应实时打分的：规则引擎、三条自基线侧信道、命中率通道、Slop——这些直接进质量账本。
- 周期 checker 里，只有声纹和答案指纹的失败进账本（进**身份账本**，权重随三档折损）。
- 身份探针和金丝雀的结果只上事件流和面板，不记分。它们是人看的哨兵，不是自动扣扳机的裁判——供词和单题通过率都不配单独定罪。

### 3.1 声纹指纹 fingerprint

对固定 prompt 取首 token 的 top-logprobs 分布（temp=0，max_tokens=1），与参考算 JS 散度，超 `js_threshold` 判"不是同一个模型"。分布是权重 + 分词器的物理属性，蒸馏能偷人设，偷不走这个——所以代码里那条告警叫 覚「心の中の弱者」。

- 前提：`satori collect` 采参考，上游支持 logprobs（`protocol = "openai"`）。对不支持的上游，`collect` 直接报错拒绝，运行时通道自动跳过——这条通道对 Claude 没用，别配。
- 抓：掉包、静默换量化版本。抓不到：同权重砍 effort（那是金丝雀和测试套件的活）。
- `--prompt` 可以给身份题组的某一题单独采一份参考（文件名带 prompt 哈希后缀），身份探针的 logprob 通道会对照它。
- 参考会随官方快照更新轻微漂移，靠 TTL 和重采管理（第 5 节）。
- 记账：失败注入身份账本，STRICT 权重 20 / STANDARD 10 / BASIC 不发探针。

### 3.2 答案指纹 answerprint

8 道稳定型题组（事实、格式、短推理，没有开放题——开放题同一个模型自己也会漂移），temp=0 作答，与参考算 SequenceMatcher 平均相似度，低于 `similarity_threshold` 判异常。思路来自 LLMmap：偷得走人设，偷不走作答习惯。

- 前提：`satori answers` 采参考。纯黑盒，不需要 logprobs、不需要 usage，模型还在产出文本就能用——Anthropic 系的主力。
- 记账：失败注入身份账本，STRICT 权重 10 / STANDARD 5 / BASIC 不发。

### 3.3 身份探针 identity

多语言多格式的 8 道身份题（"你是什么模型"、JSON 自报、"忽略你的身份指令"……），每轮随机抽 `sample_size` 题。三重判据：厂商自报自相矛盾、自报厂商与模型名暗示不符（请求 claude-* 却自称 GPT）、知识截止年漂移。有对应参考指纹的题还会对首 token 分布算 JS——嘴上说一套，分布是另一套，那就是 覚「想起うさぎは警戒を」的时刻。

- 结果**不记账本**，只广播事件。它是给你看的矛盾指示器，定罪交给声纹/答案指纹。
- 会产生真实 API 费用（每轮 sample_size 次调用）。

### 3.4 金丝雀 canary

有标准答案的题周期性抽查，通过率即质量漂移。结果同样只上事件流不记分。默认两道演示题务必换成你自己的——考题私有是柯克霍夫原则的一部分，公开的考题上游可以针对性优化。

### 3.5 规则引擎 rules

每条真实响应的正文 / CoT / 请求文本过一遍规则集（内置 17 条 + `rules.toml` 用户规则，同名覆盖内置），命中按分值进质量账本。内置规则四档：

| 档位 | 例子 | 分值 |
|---|---|---|
| 伪装痕迹 | 系统提示词泄漏（"You are Claude…"被复述） | 50 |
| 厂商自报 | OpenAI / Anthropic / Google / DeepSeek / 通义 / 文心 / Kimi / Llama 八家 | 40 |
| 代理自曝 | 自述处于中转/代理之后 | 25 |
| 弱信号 | CoT 大段中文穿帮 15、GPT 口癖 10/5、R1 式自我纠偏 3、截止日自述 10 | 3–15 |
| 豁免 | `roleplay-excuse`：用户主动要求假扮，请求侧命中 | -40 |

分值设计是故意的：单条伪装泄漏直接越界（50 ≥ 熔断线），单条厂商自报需要第二条信号叠加——单信号不封神。

规则写法：

```toml
[[rules]]
name = "my-rule"          # 与内置同名即覆盖
field = "reasoning"       # content | reasoning | any（默认 any）
target = "response"       # response（默认）| request（请求侧通常配负分豁免）
match = "regex"           # contains（默认）| regex
pattern = "你的模式"
score = 20                # 可为负
description = "命中说明"
```

工程建议：多条弱信号好过一条强信号；新规则先拿 `satori replay` 在历史录制上验证再上生产；想进命中率统计的规则保持 ≥25 分。

### 3.6 三条自基线侧信道

同一个套路（EMA 自基线 + 最少 20 样本 + 连续越限才报，宁可慢不误报），三个度量：

| 通道 | 度量 | 容差 / 连续次数 | 注入分 | 抓什么 |
|---|---|---|---|---|
| 分词 tokenwatch | chars / prompt_tokens | 25% / 3 | 30 | 分词器换人（模型被偷偷路由） |
| 延迟 latencywatch | 首字节延迟 | 50% / 5 | 15 | 基础设施签名漂移，换链路或换模型 |
| 计费 billingwatch | chars / completion_tokens | 30% / 3 | 30 | token 虚报、计费克扣 |

零额外请求，白嫖工作流量；基线只在正常样本上更新，防止被污染。低比例掺水对它们不敏感——防误报的有意取舍，掺水交给下一条。

### 3.7 命中率通道 hitrate

统计严重规则（≥25 分：伪装泄漏、厂商自报、代理自曝）的命中率 EMA。孤立误报被 EMA 稀释；持续掺水（比如 90% 真 10% 假）会让命中率稳定越过 `hit_rate_threshold`（默认 5%，最少 50 样本），掺多少抓多少。告警注入 20 分后重置重新武装。

### 3.8 业务测试裁决 testing

质量锚点：把你自己的测试套件通过率变成证据。供应商能伪造文本，伪造不了"能不能把活干对"。

- 上报：`POST /satori/test/report`（reporter 角色）。必填 `trace_id / test_suite / test_name / level / status / model_claimed / upstream`。幂等键 = reporter_id + trace_id + test_name + attempt，重复上报不重复计分，重启后从 `state/test_reports.jsonl` 重建。
- 分级：L0 确定性断言（滑窗 N=3，触发注入 15，PASS 衰减 10）/ L1 语义等价（N=5，10，5）/ L2 复杂推理（N=7，8，2）/ L3 开放式不计分（标了也只是 recorded）。
- 滑窗：最近 N×3 次里失败 ≥ N 次才触发，抗偶发 flaky。历史失败率 >5% 且样本 ≥20 的测试自动标 unreliable，此后只记录不裁决。
- 信任分桶：滑窗 / lifetime / flaky 状态按 TRUSTED / NORMAL 分桶隔离，低信任凭据的报告稀释不了 TRUSTED 的窗口；UNVERIFIED 仅落盘，根本不进裁决。
- 非对称权力：**只有 TRUSTED 凭据**的 PASS 才衰减质量类嫌疑（受 `score_floor` 地板约束，洗不穿），FAIL 才享"跨线优先"——注入分保底够跨过熔断线，可立即熔断。NORMAL 的 FAIL 只加等级分值，PASS 不衰减。
- 接入：pytest 插件 `-p satori_gateway.pytest_plugin` + `@satori_test(level="L0")`；非 pytest 生态 `satori-test-report results.xml --level L0`（JUnit XML / TAP / JSON 自动探测）。凭据走环境变量，Satori 挂了入本地队列，CI 不红。细节见 [SECURITY.md](SECURITY.md) 第 8 节。

### 3.9 工具链审计 slop

Tool Call 场景下，模型被降级的危害不是"回答变笨"，是工具参数错了、逻辑链断了——静默污染下游系统。每个带工具调用的响应落一条 trace 到 `state/tool_traces.jsonl`（干净的也留痕），按结构化启发式打分：

| 形态 | 分值 |
|---|---|
| 参数不是合法 JSON（断链） | +30 |
| 调了请求里没声明的工具（幻觉） | +20 |
| 同一响应内重复相同调用（复读） | +15 |
| 参数膨胀 > 8 KB | +10 |

三阶段：怀疑（单步 score>0，广播 `slop` 事件）→ 实锤（同 session 累计 2 步可疑，注入 DEGRADED 级**质量类**嫌疑 40 分，附 覚「偽りの魂に、真の力は宿らない」）→ 回溯（实锤报告标出第一个突变步，面板高亮"第一个被污染的念头"）。实锤后该 session 重新武装，不刷屏。

- session 由客户端请求头 `X-Satori-Session` 声明；没声明时按 `上游×模型` 归并兜底。早期版本按每请求的 trace_id 归并——每请求一个新 uuid，实锤永远凑不满两步，那是 bug 不是设计。
- 未实锤 session 跟踪上限 1000 个，最久不活动的先淘汰——自定义 session 头的流量不能把内存撑爆。
- 诚实的边界：这是结构化启发式不是语义判定。"参数选错但 JSON 合法"认不出，那是测试套件的活。语义回放基线在路线图上。
- 前置：Anthropic Messages 适配器已双向翻译 tools / tool_use / tool_result（含流式 `input_json_delta` 重组），工具链因此能被检测核心看到。OpenAI Responses 适配器目前只翻文本 input 与 instructions，function calling item 未翻译。

### 3.10 录制与回放 record / replay

`[record] enabled = true` 后，每条请求的请求文本 / 正文 / CoT / usage 按天追加到 `records/YYYY-MM-DD.jsonl`（单字段超 64 KB 截断）。回放是它的核心价值：覚 的 想起「テリブルスーヴェニール」——用今天的规则审昨天的流量，秋后算账。

```bash
satori replay records/2026-09-24.jsonl                              # 规则 + 侧信道重审
satori replay records/x.jsonl --tool-traces state/tool_traces.jsonl # 附带工具链回放
satori ingest 会话记录.md --out records/demo.jsonl                   # markdown 转录制格式
```

`ingest` 是启发式配对（角色判别是估算），导入的录制默认挂在 `import/transcript` 名下。注意录制的是对话原文：涉敏场景谨慎开启，管好 `records/` 权限（已在 .gitignore 排除）。

## 4. 双账本与三级报警

所有记分汇入每个 `上游×模型` 一本账，但分两个口袋：

- **质量类**：规则命中、侧信道、命中率、Slop 实锤、测试 FAIL。可被 TRUSTED 的测试 PASS 衰减（洗不穿 `score_floor` 地板）。
- **身份类**：声纹、答案指纹失败。**任何测试结果都洗不掉**——测试能证明"活干得对"，证明不了"你是你"。解冻只有两条路：重新 `collect` / `answers`，或 operator 显式裁决 `POST /satori/baseline/identity-cleared`（只清身份口袋）。

总分 = 两口袋之和，按阈值分三级，跃迁实时广播：

| 等级 | 条件（默认） | 动作 |
|---|---|---|
| SAFETY | < 25 | 无 |
| WATCH | 25–49 | 事件提醒，面板橙色 |
| DEGRADED | ≥ 50 | 告警；`[breaker] enabled` 时熔断，该 上游×模型 后续请求一律 503 |

记账方式：每次先把旧分按半衰期（默认 1 小时）衰减，再加新分。孤立小错随时间归零，持续掺水的加分速度远超衰减——审计学叫"重要性水平"，不追究孤立小错，只追频率异常。等级只在**升级**时广播，降级静默（复位走 `breaker/reset` 的 closed 事件）。

熔断 SOP：

1. 面板/日志出现 `breaker open`，该 上游×模型 已 503 停工，其他上游不受影响。
2. 取证：`logs/satori.log` 看命中详情；开了录制就 `satori replay` 重审。
3. 误报 → operator 凭据 `POST /satori/breaker/reset {"upstream":"...","model":"..."}`（清熔断 + 清账本）。
4. 官方真的更新了 → `satori feedback --upstream U --model M --reason official_update --confirm` 走退役流程（第 5 节）。
5. 实锤掉包 → 换上游，拿着录制文件去对线退款。

## 5. 基线生命周期与 TTL

**可信度**：

```
trust(t) = W_source × W_pressure × max(0, 1 − age/TTL)^γ        γ = 1.5
```

- `W_source`：来源权重，official 1.0 / secondhand 0.5 / community 0.3。采集时写进参考旁的 `.meta.json` sidecar；没有 sidecar 的存量参考一律按 secondhand。**来源先于时机**：二手端点采的"官方参考"只是一份漂亮的二手真相。
- `W_pressure`：采集时刻的压力权重，LOW/MID 1.0 / HIGH 0.6 / EXTR 0.0。压力按**厂商本地时区**的流量模式算（夜间 LOW，工作日高峰 HIGH）。`--force-pressure` 可以硬采，但 0.6 的折扣自己签收。
- `age/TTL`：时间衰减，γ=1.5 让曲线前期平缓、末期陡降。

**三档动作**（档次是油门不是仪表）：

| 档次 | 条件 | 身份通道 |
|---|---|---|
| STRICT | trust ≥ 0.8 | 满额记账（声纹 20 / 答案 10） |
| STANDARD | 0.4 ≤ trust < 0.8 | 半额（10 / 5）。老参考不配满贯信任 |
| BASIC | 无参考 / 已退役 / trust < 0.4 | 连探针都不发。省 token，也不拿过期参考诬告 |

trust 跌破 0.1 额外打 `expired` 标记，行为等同 BASIC。

**TTL（保质期）**：`ttl.py` 从 `state/baseline_events.jsonl` 的真实事件学习——你每次确认 `official_update` 就是一次实锤的过期事件，这才是学习原料，不空等刷新历史。

- 间隔样本 < 3 段（约等于事件 < 4 条）：固定 30 天，不学。
- 3–4 段：相邻事件间隔的中位数 × 0.7（首次启用，保守）。
- ≥ 5 段：中位数 × 0.9，3σ 剔异常，钳制在 [7, 180] 天。
- 消耗 70% 黄灯（建议重采）/ 90% 橙 / 100% 红——**只点灯，永不自动退役**。预测不配执行地面真值的动作：自动退役等于用预测杀人，误报形态会从"狼来了"变成"真狼来了却闭嘴"。
- 学习、锁定、预警都按 `上游×模型` 各自记账，官方与中转跑同名模型互不串味。

**注意有两套"冷启动"定义，别搞混**：TTL 学习的冷启动看的是事件间隔数（< 3 段不学）；而 feedback 退役阈值的冷启动看的是基线事件数（< 3 条时 1 票即可退役，常态 3 票）。前者管"保质期学不学"，后者管"几票能退役"。

**退役闭环**：`POST /satori/baseline/feedback`（reporter 角色）提交 `official_update` + confirm——累积到阈值（冷启动 1 票 / 常态 3 票，自上次退役起计）或 operator 一票 → 参考文件连 sidecar 归档进 `archive/baselines/<上游>--<模型>/`（含 meta.json 留痕：时间、原因、reporter、退役前最后一次 JS）→ 该 上游×模型 降 BASIC，账本清零。`network_jitter` / `false_alarm` 的 confirm 只清账本，不动基线。退役后重新 `collect` / `answers`，退役标记自动失效（参考文件比退役时间新即忽略），档次按可信度重新算。

**手动锁定**：operator 可以 `POST /satori/baseline/ttl/override` 给一个正的 `ttl_days`（可选 `expires_at` 绝对截止时间）临时锁定保质期。锁定 ≠ 退役：`expires_at` 到期自动解锁回归学习值；没有 HTTP 端的解锁接口，删掉 `state/ttl_overrides.json` 里对应条目也行。操作员能拖时间，不能改判生死。

## 6. state/ 目录：哪个能删，删了亏什么

| 文件 | 内容 | 删了亏什么 |
|---|---|---|
| `ledger.json` | 双账本 + 熔断快照（防抖落盘、原子替换） | 账本清零重启。不推荐 |
| `feedback.jsonl` | 误报反馈流水 | 退役计数与 TTL 学习各少一份原料 |
| `baseline_events.jsonl` | 基线事件流水（refreshed / retired） | TTL 退回冷启动 30 天；feedback 退役阈值也回冷启动票制 |
| `test_reports.jsonl` | 测试上报流水（重启重建裁决状态用） | 幂等集、滑窗、flaky 判定全部重来 |
| `tool_traces.jsonl` | 工具链 trace（不含原始参数文本） | 丢回放取证原料 |
| `ttl_overrides.json` | 手动锁定的保质期 | 锁定解除，回归学习值 |
| `credentials.db` | 控制面凭据库（SQLite，0600） | **别删，别进 git**。HMAC 模式存 secret 原文 |

退役归档在 `archive/baselines/`（配置 `state.archive_dir`），含 meta.json 留痕，`satori replay` 翻旧账用。`fingerprints/` 是参考指纹的家，不算 state，但同样别乱删——删了就是亲手把觉大人的眼睛捂上（退回 BASIC）。

## 7. 端点清单

代理面（无鉴权，客户端用）：

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat，规范本身，透传快车道 |
| `POST /v1/messages` | Anthropic Messages 入口 |
| `POST /v1/responses` | OpenAI Responses 入口（文本级翻译） |
| `GET /v1/models` | 配置里全部 上游×模型 的列表 |

读面（无鉴权，本地面板定位的明示取舍，见 [SECURITY.md](SECURITY.md)）：

| 端点 | 说明 |
|---|---|
| `GET /satori/status` | 全量状态：security / baselines（含 TTL 与锁定）/ checks / suspicion（双口袋分）/ breakers / tests / slop |
| `WS /satori/live` | 连接即收 snapshot，之后持续推事件 |

控制面（凭据把关，签名格式见 [SECURITY.md](SECURITY.md)）：

| 端点 | 角色 | 说明 |
|---|---|---|
| `POST /satori/breaker/reset` | operator | 复位熔断 + 清账本 |
| `POST /satori/baseline/feedback` | reporter | 误报反馈 / official_update 确认（攒票退役） |
| `POST /satori/test/report` | reporter | 业务测试上报（信任轴 + 滑窗裁决） |
| `POST /satori/baseline/identity-cleared` | operator | 身份类嫌疑裁决解冻（只清身份口袋） |
| `POST /satori/baseline/ttl/override` | operator | 锁定保质期 |
| `GET/POST/DELETE /satori/admin/credentials[/{id}]` | admin_secret | 凭据签发（201，secret 只出现一次）/ 查询（可按 `status`、`trust_level` 过滤）/ 吊销（204，记录保留） |

**角色是等级不是平行标签**：operator 天然是 reporter，admin 天然是一切。否则"能退役基线的人反而不能提交触发退役的 feedback"，荒谬。

## 8. 事件类型（ws /satori/live）

全部 JSON，带 `ts`：

| type | 何时发 |
|---|---|
| `snapshot` | WS 连接即收，全量状态 |
| `restored` | 重启后从 ledger.json 恢复账本（有事才发） |
| `check` | 每轮周期核验每条结果 |
| `request` | 每条转发请求（状态码、首字节延迟、token 计数） |
| `suspicion` | 每次记账（gained / total / hits） |
| `level` | 报警等级**升级**（降级静默） |
| `alert` | 越界进入 DEGRADED |
| `breaker` | 熔断跳闸（open）/ 人工复位（closed） |
| `baseline` | 基线退役（retired）/ 身份解冻（identity-cleared） |
| `ttl` | TTL 老化状态翻转 / 手动锁定 |
| `feedback` | 每条反馈（含 confirm 计数、deduped） |
| `test` | 每条测试上报的裁决结果 |
| `slop` | 工具链怀疑（suspicious）/ 实锤（confirmed） |

做自己的告警机器人直接订阅这个频道即可，不用轮询 status。

## 9. 故障排查

- **声纹通道一直"跳过"**：上游协议不是 openai（没有 logprobs 能力），或者参考没采。前者改走 answerprint，后者 `satori collect`。
- **启动日志说 `Running in BASIC mode`**：没采参考或基线已退役。不是错误，是告诉你那只眼闭着。想睁开就去采集。
- **`checker 自身报错`**：周期核验里某个 checker 抛异常被兜住了（该轮记失败但不记分）。看 `logs/satori.log` 堆栈；开 `LOGURU_FULL_TRACEBACK=1` 拿完整栈。
- **面板事件流不动**：WS 没建立。确认装了 `uvicorn[standard]`；反代场景确认 WS 升级头被转发。
- **客户端报 503 `breaker open`**：熔断中，不是网关坏了。按第 4 节 SOP 走。
- **控制面 401**：三枚签名头缺一、时间戳超窗（本机时钟飘了 5 分钟以上）、nonce 重放、或凭据已吊销。403 是角色不够。
- **可疑度莫名很高**：`GET /satori/status` 看 `suspicion` 段的 hits 构成（quality / identity 分开列），再对 `logs/satori.log` 的 `[suspicion]` 行定位具体命中。确认误报走 feedback 清账。
- **Windows 下 curl 中文 400**：控制台把 `-d` 里的中文转 GBK 了，JSON 写文件用 `--data-binary @req.json`（详见 [QA.md](QA.md)）。

## 10. 二次开发

三类插件全是"放模块 + 装饰器"，包扫描自动发现，不需要改任何现有文件：

- **checker**（周期核验）：`checkers/` 放模块，`@register_checker`，实现 `from_config(cfg)`（返回 None 即禁用）和 `check(client, upstream, model)` → `CheckResult`。
- **adapter**（客户端协议）：`adapters/` 放模块，`@register_adapter`，实现 `to_canonical` / `from_canonical` / `translate_sse` / `finish_sse`。参考 `anthropic.py`（含 tools 双向翻译与流式重组）。
- **pipeline**（上游协议）：`pipelines/` 放模块，`@TransferPipeline.register("upstream"/"convert", name)`，声明 `format` 与 `capabilities`（有 `{"logprobs"}` 声纹通道才开工）。

跑测试：`pytest -q`（221 个）。改完代码记得让测试保持绿。
