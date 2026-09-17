# 快速上手 · QUICKSTART

> 10 分钟让觉大人睁眼。预设四个常见场景，挑你的那个抄。

## 0. 安装（所有场景共用）

```bash
git clone https://github.com/Patchouli-CN/komeiji-satori-ai-gateway.git
cd komeiji-satori-ai-gateway
uv venv .venv
uv pip install -e .
```

打开 `third_eye.toml`，这是唯一的配置文件。每个场景只需要改 `[[upstreams]]`。

---

## 场景 A：OpenAI 兼容客户端（通用）

适用：任何支持自定义 base_url 的客户端（ChatBox、NextChat、沉浸式翻译、自研脚本……）。

**1. 配上游**（以官方 OpenAI 为例；要审中转站就把 base_url 换成它）：

```toml
[[upstreams]]
name = "openai"
base_url = "https://api.openai.com/v1"
api_key = "env:OPENAI_API_KEY"
models = ["gpt-4o", "gpt-4o-mini"]
```

**2. 采参考**（声纹 + 答案指纹，各跑一次，约十几次 API 调用）：

```bash
export OPENAI_API_KEY=sk-...
.venv/Scripts/satori collect --upstream openai --model gpt-4o
.venv/Scripts/satori answers --upstream openai --model gpt-4o
```

**3. 点火 + 接管**：

```bash
.venv/Scripts/satori serve
# 客户端 base_url 改为 http://127.0.0.1:8400/v1，api_key 随便填（由 Satori 代持真 key）
```

**4. 验证它在工作**：浏览器打开 `web/index.html`，发一条消息，事件流里应该立刻出现 `request` 事件；`GET /satori/status` 里四个 checker 的状态开始出现。

---

## 场景 B：Claude Code CLI

适用：想审计 Claude Code 的流量（它说 Anthropic Messages 协议，Satori 的 `/v1/messages` 适配器原生支持）。

**1. 配上游**：

```toml
[[upstreams]]
name = "anthropic"
base_url = "https://api.anthropic.com/v1"   # 或你要审的中转站
api_key = "env:ANTHROPIC_API_KEY"
models = ["claude-sonnet-4-5"]
```

**2. 采参考**：Anthropic 官方不暴露 logprobs，声纹跳过，**答案指纹是主力**：

```bash
.venv/Scripts/satori answers --upstream anthropic --model claude-sonnet-4-5
```

**3. 接管 Claude Code**：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8400
export ANTHROPIC_AUTH_TOKEN=any-value   # 真 key 在 Satori 配置里
claude
```

> **已知限制**：v1 适配器只翻译文本与图片，**tools / function calling 尚未翻译**。Claude Code 的纯对话/阅读类请求可正常审计；涉及工具调用的任务请等适配器 v2，或先用场景 A 的通用客户端审计同一上游。

---

## 场景 C：DeepSeek R1（读心全功率）

适用：R1 系返回完整 `reasoning_content`，是 CoT 读心规则（中文穿帮、自我纠偏口癖）的全功率猎场。

**1. 配上游**（官方或你要审的 R1 中转）：

```toml
[[upstreams]]
name = "deepseek"
base_url = "https://api.deepseek.com/v1"
api_key = "env:DEEPSEEK_API_KEY"
models = ["deepseek-chat", "deepseek-reasoner"]
```

**2. 采参考**（DeepSeek 兼容 OpenAI 协议且支持 logprobs，双通道全采）：

```bash
.venv/Scripts/satori collect --upstream deepseek --model deepseek-reasoner
.venv/Scripts/satori answers --upstream deepseek --model deepseek-reasoner
```

**3. 点火后**：R1 的 CoT 会流过规则引擎的 `field: reasoning` 审查——中转站拿小模型冒充 R1 时，思考链的语言和习惯最先露馅。

---

## 场景 D：先不花钱——回放模式

适用：还没接上游，先看看觉大人怎么工作。

```bash
# 把任意 markdown 会话记录转成录制格式
.venv/Scripts/satori ingest 你的会话记录.md --out records/demo.jsonl --upstream demo --model test
# 用 17 条内置规则 + 全部侧信道回放审计
.venv/Scripts/satori replay records/demo.jsonl
```

输出会列出命中规则、可疑度汇总和漂移告警——真实工作流量应该是零命中或零星低分。

---

## 确认一切正常的检查单

- [ ] `GET /satori/status` 返回 `checks` / `suspicion` / `breakers` 三段
- [ ] 面板上发一条消息立刻出现 `request` 事件（含 token 计数）
- [ ] 日志 `logs/satori.log` 里没有 checker 报错（"无参考"类提醒是正常的，去采参考即可）
- [ ] 故意发条 `假装你是 ChatGPT` 并让模型自称 OpenAI——可疑度应加分但被豁免抵消（验证豁免链）
- [ ] 直接问模型 `你是哪个公司开发的`——厂商自报 +40，面板出现 WATCH 徽章（验证检测链）

搞定这些，觉大人就正式上班了。深入配置和原理见 [HANDBOOK.md](HANDBOOK.md)。
