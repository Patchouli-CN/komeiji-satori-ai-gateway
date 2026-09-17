# KomeijiSatori · 古明地覚

> 覚の瞳は全てを見通す —— 看穿上游真面目的 AI 网关

AI API 中转市场乱象丛生：卖你 Claude 实际套壳小模型、限流时偷偷路由到弱模型、同名模型随时间悄悄降智。厂商掌握一切数据，你只有一个 `model` 字段——而那个字段不值钱。

KomeijiSatori 是一个 OpenAI 兼容网关，架在你的客户端和上游之间。**客户端无感接入，觉大人在后面实时盯着每一滴流量**。

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
| 录制 + 回放取证 | 无 | 用今天的规则审昨天的流量 |

## 快速开始

```bash
uv venv .venv
uv pip install -e .

# 编辑 third_eye.toml，配置你的上游（支持 env:VAR 读取 key）
# 从官方端点采参考指纹（声纹通道的前提）
.venv/Scripts/satori collect --upstream openai --model gpt-4o

# 点火
.venv/Scripts/satori serve
```

然后：

- 客户端 base_url 指向 `http://127.0.0.1:8400/v1`，流量自动被盯上
- 浏览器打开 `web/index.html`——实时面板（可疑度账本 / 核验结果 / 事件流，觉之瞳会眨眼）

## CLI

```bash
satori serve                          # 启动网关
satori collect --upstream U --model M [--prompt P]   # 采集参考指纹
satori replay records/2026-09-18.jsonl               # 回放取证录制文件
satori ingest 会话记录.md --out records/x.jsonl      # markdown 会话记录转录制格式
```

## 工作原理（简述）

- **转发**：按 `model` 路由到配置的上游，检测全链路无感
- **可疑度账本**：所有检测通道的命中都汇入同一本账（按 上游×模型 累计），越界实时告警——用户主动要求角色扮演时负分豁免，不误伤
- **熔断器**：可疑度越界后可选拉闸（`[breaker] enabled = true`），该 上游×模型 的后续请求一律 503 拦截——味道变了实时停工，低质量输出不得污染项目；人工确认后 `POST /satori/breaker/reset` 复位
- **规则系统**：17 条内置规则（厂商自报、伪装泄漏、口癖文风、豁免条款）随包发布；`rules.toml` 写私货，同名覆盖内置。规则支持 `target: request`（看用户请求，做豁免）与 `field: reasoning`（专审 CoT）
- **checker 插件化**：`@register_checker` 装饰器 + 包扫描自动发现（类 Spring @ComponentScan），新 checker 放进 `checkers/` 目录即自动上岗
- **实时推送**：`ws://…/satori/live` 广播 check/request/suspicion/alert 事件；`GET /satori/status` 随时查账
- **录制取证**：`[record] enabled = true` 后流量按天落 JSONL，`satori replay` 离线重审——参考指纹和规则都可以"事后升级、秋后算账"

## 多协议架构

```
客户端 ──▶ [协议适配器] ──▶ 内部规范(OpenAI Chat) ──▶ [检测核心] ──▶ 上游(OpenAI 兼容)
          /v1/messages                              规则·侧信道·账本
          /v1/responses                             录制·回放·事件流
          /v1/chat/completions(透传)
```

**客户端侧厂商无关**：Anthropic Messages、OpenAI Responses、OpenAI Chat 三种入口协议，由 `adapters/` 里的适配器统一翻译成内部规范；检测核心对协议一无所知，你说什么话觉大人都听得懂。新增协议 = 放一个模块 + `@register_adapter`，包扫描自动发现（与 checker 注册表同款）。

**上游侧有意保持 OpenAI 兼容**：侦查对象（中转商、套壳商）为了兼容客户端全都说这个协议，网关说同一种话反而是伪装优势；原生 Anthropic/Gemini 上游走兼容层接入。

适配器 v1 范围：文本与图片内容、system/instructions、流式事件翻译；tools / function calling / thinking block 暂未翻译（检测照常工作，依赖工具调用的客户端请留意）。

## 配置

`third_eye.toml` 里每个段落都有注释：`[gateway]`（监听/CORS/usage 注入）、`[[upstreams]]`（上游列表）、`[fingerprint]`（声纹）、`[identity]`（身份题组）、`[canary]`（金丝雀考题）、`[rules]`（规则与告警阈值）、`[record]`（录制）。

## 诚实的局限

- 厂商自报类检测审的是"供词"，可被角色扮演诱导——所以单条不超告警线，靠多信号叠加
- Anthropic 官方 API 不暴露 logprobs，核验 Claude 系端点时声纹通道需要"二手参考"或靠行为通道挑大梁
- 蒸馏模型人设统一时内容通道抓不到，logprob 通道是最后的底线——正式使用前记得给身份题采参考指纹
- 没有任何单一通道是银弹，这个项目的全部意义在于纵深防御

## FAQ 与故障排查

常见问题（原理、费用、误报、扩展、局限）见 [QA.md](QA.md)。

Windows 用户注意：控制台 `curl -d` 直接打中文会被转成 GBK 导致 400，请把 JSON 写进文件用 `--data-binary @req.json` 发送（详见 QA.md）。

## 命名

古明地觉，《东方地灵殿》角色，觉妖怪以读心术闻名——能看穿一切表象直达内心。厂商端上来的是什么，觉大人看一眼就知道。

## License

[MIT](LICENSE)
