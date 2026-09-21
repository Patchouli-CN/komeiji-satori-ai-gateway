# KomeijiSatori · 古明地覚

> 覚の瞳は全てを見通す —— 看穿上游真面目的 AI 网关

[English README](../../README.md)

**文档**：快速上手 [QUICKSTART.md](QUICKSTART.md) · 运维手册 [HANDBOOK.md](HANDBOOK.md) · 常见问题 [QA.md](QA.md) · 安全手册 [SECURITY.md](SECURITY.md) · [English docs](../en/QUICKSTART.md)

AI API 中转市场乱象丛生：卖你 Claude 实际套壳小模型、限流时偷偷路由到弱模型、同名模型随时间悄悄降智。厂商掌握一切数据，你只有一个 `model` 字段——而那个字段不值钱。

KomeijiSatori 是一个多协议 AI 网关（OpenAI Chat / Anthropic Messages / OpenAI Responses），架在你的客户端和上游之间。**客户端无感接入，觉大人在后面实时盯着每一滴流量**。

## 设计宣言

**在不完全依赖大厂配合的前提下，尽力发现模型被路由或被降智。**

每个检测通道假设的配合度不同，厂商掐掉任何一个字段都只是降级而非失明——厂商能掐掉字段，掐不掉行为：

| 通道 | 配合度需求 | 抓什么 |
|---|---|---|
| logprob 声纹指纹 | 一个可选字段 | 模型掉包（分布冒充不了） |
| 答案指纹（LLMmap 思路） | 无 | 掉包/降智（纯黑盒，Claude 系主力） |
| usage 分词侧信道 | 不伪造 usage 即可 | 分词器换人（自基线，零额外请求） |
| 计费一致性审计 | 不伪造 usage 即可 | token 虚报/计费克扣（GatewayBench 思路） |
| 身份探针题组 | 无 | 厂商自报矛盾 / 截止年漂移 |
| 金丝雀考题 | 无 | 降智（能力通过率漂移） |
| 自定义规则引擎 | 无 | 口癖、伪装提示词泄漏、CoT 穿帮 |
| 延迟画像漂移 | 无 | 基础设施签名漂移（自基线） |
| **业务测试锚点** | 无 | **降智的终极裁判：你自己的测试套件，供应商无法伪造"能不能把活干对"** |
| Tool Call 链级审计（Slop） | 无 | 参数 JSON 断裂、幻觉工具、复读、参数膨胀 |
| 录制 + 回放取证 | 无 | 用今天的规则审昨天的流量（含工具链） |

## 快速开始

```bash
uv venv .venv
uv pip install -e .

# 编辑 third_eye.toml，配置你的上游（支持 env:VAR 读取 key）
# 从官方端点采参考指纹（声纹通道的前提）；--source 标注来源（trust 权重）
.venv/Scripts/satori collect --upstream openai --model gpt-4o --source official
.venv/Scripts/satori answers --upstream openai --model gpt-4o

# 点火
.venv/Scripts/satori serve
```

然后：

- 客户端 base_url 指向 `http://127.0.0.1:8400/v1`，流量自动被盯上
- 浏览器打开 `web/index.html`——实时面板（可疑度账本 / 核验结果 / 基线判别 / 试炼裁决 / 事件流，觉之瞳会眨眼）
- 控制面（feedback / breaker reset / admin）需要凭据：首次启动看控制台的一次性引导 token，用法见 [SECURITY.md](SECURITY.md)

## CLI

```bash
satori serve                          # 启动网关
satori collect --upstream U --model M [--prompt P] [--source official] [--wait-for-low]   # 采集参考指纹（带压力感知与溯源记录）
satori answers --upstream U --model M [--source official]  # 采集答案指纹参考作答
satori feedback --upstream U --model M --reason official_update --confirm   # 误报反馈 / 确认官方更新（攒够阈值退役基线）
satori replay records/x.jsonl [--tool-traces state/tool_traces.jsonl]       # 回放取证录制文件（可附带回放工具链）
satori ingest 会话记录.md --out records/x.jsonl      # markdown 会话记录转录制格式
satori-test-report results.xml        # 把 JUnit/TAP/JSON 测试报告上报给 Satori（非 pytest 生态）
satori-test-mock --port 8401          # 本地 mock 网关（开发/CI 验证上报逻辑）
```

pytest 生态：`SATORI_URL=... SATORI_REPORTER=... SATORI_SECRET=... pytest -p satori_gateway.pytest_plugin`，用 `@satori_test(level="L0")` 标测试级别。

## 工作原理（简述）

- **转发**：按 `model` 路由到配置的上游，检测全链路无感
- **可疑度账本（双组件）**：所有检测通道的命中汇入一本账，但分两个口袋——**质量类**（规则/侧信道/测试/Slop）与**身份类**（声纹 JS / 答案指纹）。三级报警等级 `SAFETY` → `WATCH`（默认 25 分）→ `DEGRADED`（默认 50 分），跃迁实时广播；账本带半衰期衰减——孤立小错归零，持续掺水照样积聚。命中率通道专治"90% 真 10% 假"。**身份类口袋测试 PASS 洗不掉**（两轴模型，见 QA）
- **三档判别动作**：每个基线按可信度 `trust = W_source × W_pressure × 衰减^γ` 分档——`STRICT`（trust≥0.8）身份通道满额记账；`STANDARD`（0.4~0.8）半额（老参考不配满贯信任）；`BASIC`（无参考/已退役/跌破 0.4）身份通道**连探针都不发**，只留黑盒通道在岗。档次是油门不是仪表
- **基线生命周期**：采集时记溯源（来源/压力/时间），TTL 动态学习（反馈事件驱动，70/90/100% 点灯）——**过期只预警不自动退役**；你真正确认 `official_update`（reporter×3 或 operator×1，冷启动 1 次）才退役归档、降级 BASIC 优雅闭眼，而不是误报风暴
- **熔断器**：可疑度越界后可选拉闸（`[breaker] enabled = true`），该 上游×模型 的后续请求一律 503——味道变了实时停工；人工确认后 `POST /satori/breaker/reset` 复位（**需要 operator 凭据**）
- **业务测试锚点**：`POST /satori/test/report` 给你自己的测试套件记分（L0~L3 分级、滑窗、flaky 自动标记、幂等）。TRUSTED 凭据的 PASS 才衰减质量类嫌疑（有负分地板）、连续 FAIL 直接 DEGRADED + 熔断——**降级必然导致能力下降，套件连续失败就是实锤**
- **Tool Call 链级审计（Slop）**：工具调用链逐响应结构化评分，同 session 累计两步可疑即实锤，回溯第一个"被污染的念头"
- **规则系统**：17 条内置规则随包发布；`rules.toml` 写私货，同名覆盖内置。规则支持 `target: request`（看用户请求，做豁免）与 `field: reasoning`（专审 CoT）
- **checker 插件化**：`@register_checker` 装饰器 + 包扫描自动发现（类 Spring @ComponentScan），新 checker 放进 `checkers/` 目录即自动上岗
- **实时推送**：`ws://…/satori/live` 广播 check/request/suspicion/alert/breaker/baseline/ttl/test/slop/feedback 事件；`GET /satori/status` 随时查账（含基线的三档、TTL 消耗、试炼通过率、Slop 累积）
- **录制取证**：`[record] enabled = true` 后流量按天落 JSONL（工具链落 `state/tool_traces.jsonl`），`satori replay [--tool-traces]` 离线重审——参考指纹和规则都可以"事后升级、秋后算账"
- **重启不失忆**：账本/熔断/反馈/事件全部落盘 `state/`，重启后按半衰期折算恢复——审计状态是时间函数

## 多协议架构

```
客户端 ──▶ [协议适配器] ──▶ 内部规范(OpenAI Chat) ──▶ [检测核心] ──▶ [管线] ──▶ 上游(OpenAI 兼容 / Anthropic 原生)
           /v1/messages                              规则·侧信道·账本·两轴裁决
           /v1/responses                             录制·回放·Slop·事件流
           /v1/chat/completions(透传)
```

**客户端侧厂商无关**：Anthropic Messages、OpenAI Responses、OpenAI Chat 三种入口协议，由 `adapters/` 里的适配器统一翻译成内部规范；检测核心对协议一无所知。新增协议 = 放一个模块 + `@register_adapter`，包扫描自动发现。

**上游侧协议插件化**：默认 `protocol = "openai"`；设 `protocol = "anthropic"` 可直连 Anthropic 官方原生 API（`pipelines/` 声明式插件翻译）。注意 logprobs 声纹通道仅 openai 协议上游可用，其余协议请用答案指纹（`[answerprint]`）。

适配器 v2 范围：文本与图片内容、system/instructions、流式事件翻译，以及 **tools / function calling 双向翻译**（含流式 `input_json_delta` 重组）；thinking block 仍未翻译（检测照常，依赖思考块的客户端请留意）。

## 配置

`third_eye.toml` 里每个段落都有注释：`[gateway]`（监听/CORS/usage 注入）、`[[upstreams]]`（上游列表）、`[fingerprint]`（声纹）、`[identity]`（身份题组）、`[canary]`（金丝雀考题）、`[rules]`（规则与告警阈值）、`[breaker]`（熔断）、`[record]`（录制）、`[security]`（控制面凭据：HMAC/Ed25519 + 三角色 + TLS）、`[state]`（审计状态持久化）、`[testing]`（业务测试裁决）、`[logging]`。

## 诚实的局限

- 厂商自报类检测审的是"供词"，可被角色扮演诱导——所以单条不超告警线，靠多信号叠加
- Anthropic 官方 API 不暴露 logprobs，核验 Claude 系端点时声纹通道需要"二手参考"或靠行为通道挑大梁
- 测试锚点抓降级，声纹抓同级掉包——**两条链互补，谁也不能替谁**；而套件的下限就是标尺的下限（怎么养套件见 QA）
- Slop 的结构化评分不是语义判定：参数 JSON 断裂认得出，"参数选错但 JSON 合法"认得出（那是测试套件的活）
- TTL 学习靠真实事件积累，冷启动固定 30 天；预测只预警，永不自动作出退役判决
- HMAC 模式下服务端必须存 secret 原文（验证需要 key）——管好 `state/credentials.db` 权限；多团队场景请上 Ed25519
- 没有任何单一通道是银弹，这个项目的全部意义在于纵深防御

## FAQ 与故障排查

常见问题（原理、费用、误报、两轴、基线生命周期、局限）见 [QA.md](QA.md)。凭据签发/泄露应急/TLS 与 trace 传播见 [SECURITY.md](SECURITY.md)。快速上手与场景预设见 [QUICKSTART.md](QUICKSTART.md)，深入配置与原理见 [HANDBOOK.md](HANDBOOK.md)。

Windows 用户注意：控制台 `curl -d` 直接打中文会被转成 GBK 导致 400，请把 JSON 写进文件用 `--data-binary @req.json` 发送（详见 QA.md）。

## 命名

古明地觉，《东方地灵殿》角色，觉妖怪以读心术闻名——能看穿一切表象直达内心。厂商端上来的是什么，觉大人看一眼就知道。

## License

[MIT](LICENSE)
