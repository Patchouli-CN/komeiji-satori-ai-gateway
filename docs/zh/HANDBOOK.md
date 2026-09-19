# 运维手册 · HANDBOOK

> 从配置到原理到二次开发的全量参考。快速上手先看 [QUICKSTART.md](QUICKSTART.md)，常见问题看 [QA.md](QA.md)。

## 1. 架构总览

```
客户端 ──▶ [协议适配器 adapters/] ──▶ 内部规范(OpenAI Chat) ──▶ [检测核心] ──▶ [管线 pipelines/] ──▶ 上游(OpenAI 兼容 / Anthropic 原生)
          /v1/chat/completions(透传)        ├─ 规则引擎 rules.py（每条响应）
          /v1/messages                      ├─ 侧信道 watch.py（分词/计费/延迟/命中率）
          /v1/responses                     ├─ 周期 checker checkers/（声纹/答案/身份/金丝雀）
                                            ├─ 可疑度账本 + 三级等级 + 熔断 app.py
                                            ├─ 录制 record.py（JSONL 落盘）
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
| `[logging]` | `level` / `file` | INFO / logs/satori.log | loguru（10MB 轮转） |

## 3. 检测通道详解

### 3.1 logprob 声纹（fingerprint）

- **原理**：探针首 token 的 top-logprobs 分布是权重+分词器的物理指纹；与官方参考算 JS 散度
- **前提**：`satori collect` 采参考；上游支持 logprobs
- **抓**：掉包、静默量化。**抓不到**：同权重砍 effort（那是金丝雀的活）
- **注意**：分布随快照/量化轻微漂移，参考定期重采；`--prompt` 可给身份题单独采

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

## 4. 报警、衰减与熔断运维

**三级等级**：`SAFETY`（<25）→ `WATCH`（25~49，关注）→ `DEGRADED`（≥50，告警+熔断）。

**衰减**：每次记账先把旧分按半衰期衰减再加新分。含义：

- 孤立小错（真模型偶发口癖）→ 自动归零，不冤案
- 持续异常 → 加分速度远超衰减，照样升级
- 审计学叫"重要性水平"：不追究孤立小错，只追频率异常

**熔断 SOP**：

1. 面板/日志出现 `breaker open` → 该 上游×模型 已 503 停工
2. 取证：`logs/satori.log` 看命中详情；`satori replay` 重审录制
3. 确认是误报（比如上游官方真的更新了什么）：`POST /satori/breaker/reset {"upstream":"...","model":"..."}` 复位
4. 确认是实锤：换上游，拿着录制文件去对线退款

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
- **回放是升级武器**：新规则/新阈值出来后 `satori replay` 重审全部历史，秋后算账
- `satori ingest` 可把 markdown 会话记录转录制格式（启发式配对，角色判别是估算）
- 录制含对话原文：**涉敏场景管好 records/ 目录权限**（已在 .gitignore 排除）

## 7. 威胁模型（柯克霍夫原则）

**算法公开，密钥保密**：检测代码与内置规则全部开源，安全性不依赖算法保密，而依赖你私有的参考指纹、参考作答、自定义考题与自基线。

| 对手招式 | Satori 的应对 |
|---|---|
| 全面掉包 | 声纹 + 答案指纹 + 全部侧信道 |
| 掺水（90% 真） | 命中率通道 + 累积账本 + 身份矛盾检测 |
| 蒸馏人设 | 声纹（物理分布）+ 答案指纹（作答习惯） |
| 砍 effort 降智 | 金丝雀 + 答案指纹漂移 |
| 掐 logprobs | 降级到黑盒通道，不失明 |
| 伪造 usage | 文本层通道兜底；伪造成本高于收益 |
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

**adapter**（协议适配插件）：`adapters/` 放模块，`@register_adapter` + 实现 `to_canonical` / `from_canonical` / `translate_sse` / `finish_sse`，参考 `anthropic.py`。

**事件协议**（`ws /satori/live`）：`snapshot` / `check` / `request` / `suspicion` / `level` / `alert` / `breaker`，全部 JSON，带 `ts`。做自己的面板或接入告警机器人直接订阅即可。

两个注册表都是包扫描自动发现：**不需要改任何现有文件**。
