# Q&A · 常见问题

## 定位与原理

### Q: Satori 是什么？

一个架在你和 AI 上游之间的多协议网关（OpenAI Chat / Anthropic Messages / OpenAI Responses）。客户端无感接入，它在后台对每一滴流量做多层检测：模型掉包、静默路由、悄悄降智，都会被记入可疑度账本并实时告警；越界还可熔断拉闸，低质量输出直接停工。

### Q: 为什么叫古明地觉？

《东方地灵殿》的觉妖怪，读心术看穿一切表象。厂商端上来的是什么模型，觉大人看一眼（的分布）就知道。

### Q: 它怎么发现模型被掉包了？

纵深防御，没有单一银弹：

- **logprob 声纹**：token 分布是模型的物理属性，蒸馏能偷人设但偷不了分布
- **答案指纹**：固定题组 temp=0 作答与官方参考算相似度，纯黑盒——Claude 系（无 logprobs）的主力通道
- **usage 分词侧信道**：同一文本的 token 计数暴露分词器，换人即漂移（自基线，零成本）
- **计费一致性**：completion_tokens 与实收文本比例漂移，抓 token 虚报/计费克扣
- **身份题组**：多问法交叉验证，厂商自报矛盾、知识截止年漂移都是马脚
- **金丝雀考题**：有标准答案的能力题，降智直接体现在通过率上
- **规则引擎**：口癖、伪装提示词泄漏、CoT 语言穿帮，17 条内置 + 自己写
- **延迟画像**：首字节延迟的基础设施签名，漂移自基线检测
- **命中率通道**：严重规则命中率超阈值告警，专治"90% 真 10% 假"掺水

全部命中汇入同一本可疑度账，按 `SAFETY → WATCH → DEGRADED` 三级升级。

### Q: 厂商能不能反过来骗过 Satori？

可以掐掉单个字段（比如不返回 logprobs），但**掐不掉行为本身**——只要模型还在产出文本，产出就是证据。每层检测假设的配合度不同，全掐掉的代价高到不划算。这就是"不完全依赖大厂"的设计哲学。

### Q: 蒸馏模型真心相信自己是 Claude，还能抓到吗？

内容通道抓不到（它没"撒谎"，是"失忆"），但 logprob 声纹抓得到——嘴上可以冒充，token 分布冒充不了；不支持 logprobs 的端点则用**答案指纹**：作答习惯同样偷不走。所以**正式使用前务必用官方 key 采参考**（`satori collect` 采声纹、`satori answers` 采作答），否则这两条通道空转。

## 使用

### Q: 会产生额外 API 费用吗？

会。声纹指纹、答案指纹、身份题组、金丝雀都是真实 API 调用，按 `check_interval_seconds` 周期执行。上游多、模型多时留意账单；可以调大周期、调小 `identity.sample_size`、或按需禁用 checker（配置里关掉即不装配）。规则引擎、三条侧信道（分词/计费/延迟）、命中率通道零额外请求，白嫖工作流量。

### Q: Anthropic 官方 API 没有 logprobs，怎么核验 Claude 系？

主力是**答案指纹**（`satori answers` 从官方端点采参考作答，纯黑盒）；辅以身
份题组、规则引擎、延迟画像等行为通道；想上声纹也可以从 OpenRouter 等兼
容层采"二手参考指纹"（精度打折但能用）。

### Q: 录制功能安全吗？

`[record] enabled = true` 会把对话文本落盘到 `records/`。**涉敏场景谨慎开启**，管好目录权限；该目录已在 `.gitignore` 排除，不会进仓库。回放取证（`satori replay`）是它的核心价值：规则升级后重审历史流量，秋后算账。

### Q: 我要求模型扮演别的角色，会被误报吗？

不会。内置 `roleplay-excuse` 规则识别中英文假扮指令（pretend / act as / 扮演 / 假设你是……），命中给 -40 分豁免，正好抵消一条厂商自报。你也可以在 `rules.toml` 里写自己的豁免规则。

### Q: 三级报警等级是什么？

可疑度账本按分数分三级，跃迁实时广播到事件流和面板：

| 等级 | 默认分数 | 含义 | 动作 |
|---|---|---|---|
| `SAFETY` | < 25 | 安全 | 无 |
| `WATCH` | 25 ~ 49 | 可能降级，提高关注 | 事件提醒，面板橙色徽章 |
| `DEGRADED` | ≥ 50 | 质量降级 | 告警 + 熔断（若开启） |

注意账本带**半衰期衰减**（默认 1 小时，`decay_half_life_seconds` 可调）：孤立小错随时间归零，诚实上游偶发的口癖命中不会累积成冤案；只有持续异常才能积聚到 WATCH 以上。

### Q: 告警阈值怎么调？

`third_eye.toml` 的 `[rules]` 段：`suspicion_threshold`（DEGRADED/熔断线，默认 50）、`watch_threshold`（WATCH 线，默认 25）、`decay_half_life_seconds`（衰减半衰期，默认 3600）。内置规则分值设计：单条高危（伪装泄漏 50）直接越界；单条中危（厂商自报 40）需要第二条信号叠加——这是故意的，单信号不封神。

### Q: 上游掺水（90% 真 10% 假）能抓到吗？

能，这正是命中率通道的设计场景。掺水意味着每十笔交易就有一笔留下指纹：规则引擎逐条记分（累积速度远超衰减），身份题组抓"两次回答自相矛盾"，**命中率通道**统计严重规则（≥25 分）的命中比例，持续超过 5% 就告警（`hit_rate_threshold` / `hit_rate_min_samples` 可调）。实测：10% 掺水告警稳定触发，诚实上游 1% 偶发命中保持沉默。

### Q: 熔断器是什么？会误伤我的正常请求吗？

`[breaker] enabled = true` 后，可疑度越界的 上游×模型 会被拉闸：后续请求一律 503 拦截，直到你人工确认并复位（`POST /satori/breaker/reset`）。定位是"味道变了实时停工"——宁可中断也不让低质量输出流进项目。拦截只针对越界的那个 上游×模型，其他上游不受影响；越界条件本来就要求多信号叠加（见阈值设计），误伤概率很低。怕打断工作流就保持 `enabled = false`，只告警不拦截。

### Q: 支持哪些客户端协议？

三种入口：OpenAI Chat（`/v1/chat/completions`，透传）、Anthropic Messages（`/v1/messages`）、OpenAI Responses（`/v1/responses`）。v1 未翻译 tools / function calling / thinking block（检测照常，但依赖工具调用的客户端请留意）。上游侧有意保持 OpenAI 兼容——侦查对象全都说这个协议。

### Q: Windows 下 curl 测试中文请求报 400？

Windows 控制台会把 `-d` 里的中文转成 GBK，JSON 直接坏掉。不是网关的锅。用文件发帖：

```bash
printf '%s' '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}' > req.json
curl -X POST http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" --data-binary @req.json
```

## 扩展

### Q: 怎么写自己的检测规则？

编辑 `rules.toml`（项目根目录）：

```toml
[[rules]]
name = "my-rule"
field = "reasoning"   # content | reasoning | any
match = "regex"       # contains | regex
pattern = "你的模式"
score = 20            # 可为负（豁免）
description = "命中说明"
```

与内置规则同名即覆盖（调分数专用）。`target = "request"` 的规则看用户请求而不是模型输出。

### Q: 怎么写自己的 checker / 协议适配器？

都是装饰器 + 包扫描自动发现（类 Spring @ComponentScan）：

- checker：在 `checkers/` 放模块，`@register_checker` + 实现 `from_config(cfg)` 工厂和 `check()`，返回 `None` 即配置禁用
- adapter：在 `adapters/` 放模块，`@register_adapter` + 实现 `to_canonical` / `from_canonical` / `translate_sse`

不需要动任何现有文件。

### Q: 参考指纹能分享吗？

能，而且鼓励。`fingerprints/` 里的 JSON 就是纯文本分布数据，可以像杀毒软件特征库一样社区共享——谁也不求大厂。注意它含端点信息（文件名），分享前看一眼。

## 局限（丑话）

- 单通道都可被针对性绕过，这个项目的意义在于叠加后的绕过成本
- 身份自报可被角色扮演诱导——所以有豁免机制和阈值设计
- 指纹会随量化/快照更新轻微漂移，参考建议定期重采
- 我们不证明"这是真模型"，只发现"这不是当初那个模型"——行为指纹是概率性的
