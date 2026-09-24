# Q&A · 常见问题

> 按"你现在的处境"组织，不按功能。每条的答案给出处，细节链到 [HANDBOOK.md](HANDBOOK.md) 和 [SECURITY.md](SECURITY.md)。

## 上手前

### Q: 这东西到底是什么，一句话？

架在你和 AI 上游之间的网关。客户端照常发请求，它在后台持续回答一个问题：端上来的还是当初那个模型吗？发现不对就记分、告警，越界还能熔断拉闸。

### Q: 为什么叫古明地觉？

《东方地灵殿》的觉妖怪，天生第三只眼，读心术看穿一切表象，所以被人惧怕厌恶。做对抗掺水的工具，这个名字没有第二个候选。代码里那些 覚「…」 的告警台词是符卡传统，不是卖萌——每条都长在具体的检测场景上。

### Q: 跟 litellm / one-api 什么关系？

不冲突，也不重叠。它们是路由和计费网关，管"请求发给谁、花了多少钱"；Satori 管"回来的货对不对"。你可以把它串在那些网关前面或后面，也可以单用。

### Q: 厂商能不能反过来骗过它？

单个通道都能被针对性绕过，这是实话。但每个通道假设的配合度不同：掐掉 logprobs，答案指纹还在；伪造 usage，文本层通道还在；拿"能过你测试套件的同级模型"狸猫换太子，质量维度放行了，身份账本（声纹/答案指纹）还记着——测试 PASS 一分都洗不掉。全部绕过的成本比老实做生意高，这就是纵深防御的全部意义。

### Q: 蒸馏模型真心相信自己是 Claude，还能抓到吗？

内容通道抓不到——它没撒谎，是失忆。但声纹抓得到：嘴上可以冒充，首 token 的分布冒充不了。没有 logprobs 的端点就用答案指纹，作答习惯同样偷不走。前提是**正式使用前从官方端点采过参考**（`satori collect` / `satori answers`），否则这两条通道空转。

## 通道相关

### Q: 为什么我的 Anthropic 上游 fingerprint 通道一直"跳过"？

因为 Anthropic API 不返回 logprobs，这条通道对它天然不存在——不是配置错了。`protocol = "anthropic"` 的上游连 `satori collect` 都会直接报错拒绝。正确姿势：

```bash
satori answers --upstream anthropic --model claude-sonnet-4-5   # 答案指纹，主力通道
```

再辅以身份探针、规则引擎、三条侧信道。实在想要声纹，可以从可信第三方采二手参考（`--source secondhand`，信任权重对折，sidecar 记着出身）。

### Q: 身份探针和金丝雀为什么失败了不扣可疑度？

故意的。供词类信号（自报厂商）和单题通过率都不配单独定罪——它们只上事件流和面板，是给你看的哨兵。自动扣扳机的裁判只有：规则引擎、侧信道、命中率、声纹、答案指纹、Slop 实锤、测试 FAIL。想看它们"在不在岗"，面板 checks 区和 `GET /satori/status` 都有每轮结果。

### Q: 检测会产生多少额外 API 费用？

周期 checker（声纹 1 次 + 答案指纹 8 次 + 身份探针 sample_size 次 + 金丝雀每题 1 次调用）× 每个 上游×模型 × 每 `check_interval_seconds` 一轮。默认 300 秒一轮，上游多模型多时留意账单。省钱选项：调大 `check_interval_seconds`、调小 `identity.sample_size`、关掉不需要的 checker。规则引擎和三条侧信道零额外请求。BASIC 档下身份通道连探针都不发——闭眼期间这部分费用也省了。

### Q: 我要求模型角色扮演，会被误报吗？

不会单独定罪。内置 `roleplay-excuse` 规则识别中英文假扮指令（pretend / act as / 扮演 / 假设你是……），请求侧命中给 -40 豁免，正好抵消一条厂商自报。注意豁免是"总量豁免"，目前不区分你要求假扮的厂商和模型实际自报的厂商是否一致。

## 基线与三档

### Q: 面板显示 BASIC 了，怎么办？

先搞清楚是哪种 BASIC，`GET /satori/status` 的 `baselines` 段有答案：

- `reference: null` 且没退役过——你还没采参考。`satori collect` / `satori answers` 采一份就好。
- `retired_at` 有值——基线被退役了（你或别人确认过 official_update）。重新采集即恢复，退役标记自动失效。
- `trust` 跌破 0.4——参考老了或来源降权。重新采集刷新。

BASIC 不是故障，是觉大人明说"这只眼我闭着"——覚「見えないなら、見えないと言おう」。黑盒通道照常在岗，不会拿过期参考诬告人。

### Q: TTL 过期了会不会自动退役基线？

永远不会。TTL 只点灯：消耗 70% 黄、90% 橙、100% 红，提醒你"该重采了"。退役必须走 feedback 的地面真值：`satori feedback --upstream U --model M --reason official_update --confirm`，冷启动期 1 票、常态 3 票，或 operator 一票。理由写在代码注释里：自动退役 = 用预测杀人，误报形态会从"狼来了"变成"真狼来了却闭嘴"。

### Q: 我重新采集了，为什么还显示退役/BASIC？

两种可能：一是采到了别的上游名下（事件按 上游×模型 分账，名字要对上）；二是参考文件没落到配置的 `reference_dir`。正常情况下重采成功会打印"退役标记清除"，下一轮核验档次就按新可信度重算。另外网关是**每次核验从磁盘现读参考**的，不需要重启。

### Q: 三套数字我总搞混：trust 三档、报警三级、TTL 灯——什么关系？

- **trust 三档**（STRICT/STANDARD/BASIC）：基线的可信度，决定**盯多紧**。
- **报警三级**（SAFETY/WATCH/DEGRADED）：账本的可疑度，决定**罚不罚**。
- **TTL 灯**（黄/橙/红）：参考文件的保质期消耗，决定**该不该重采**。

一个管证据强度，一个管判决，一个管证据保鲜。互不替代。

## 测试裁决

### Q: 我的测试 flaky，会不会误熔断我的上游？

三层防护：滑窗要求最近 N×3 次里失败 ≥ N 次才触发（偶发失败不够）；历史失败率 >5% 且样本 ≥20 的测试自动标 unreliable，此后只记录不裁决；熔断权力只属于 TRUSTED 凭据。真被误熔了，operator 一条 `breaker/reset` 复位，然后好好修你的测试——flaky 测试当标尺，量谁都不准。

### Q: TRUSTED / NORMAL / UNVERIFIED 到底差在哪？

| | 报告进裁决 | PASS 衰减嫌疑 | FAIL 可熔断 |
|---|---|---|---|
| TRUSTED | 是（TRUSTED 桶） | 是（有地板） | 是，跨线优先 |
| NORMAL | 是（独立桶） | 否 | 否，只加等级分 |
| UNVERIFIED | 否，仅落盘 | 否 | 否 |

桶隔离的意思是：NORMAL 的报告再多，也稀释不了 TRUSTED 的滑窗判定。签发时想清楚给谁什么级别，细节见 [SECURITY.md](SECURITY.md)。

### Q: 测试套件怎么养才算"好标尺"？

多写 L0：只有你付钱的那个档位能稳定答对的确定性断言（精确格式输出、边界计算、长指令遵循）。少写"能过就谢天谢地"的 L1。L3 开放式不计分，别指望它。套件的下限就是标尺的下限——拿玩具测试当真标尺，被骗的是你自己。

## 凭据与控制面

### Q: secret 忘了 / 引导 token 没抄下来，怎么办？

secret 没有找回接口（list/get 永不返回），吊销重签：

```bash
curl -X DELETE http://127.0.0.1:8400/satori/admin/credentials/me \
  -H "X-Satori-Admin-Secret: <admin_secret或引导token>"
# 然后重新 POST 签发
```

引导 token 没抄到：重启 `satori serve` 会生成新的（旧的随之失效）。admin_secret 忘了：改 `SATORI_ADMIN_SECRET` 环境变量重启。

### Q: 控制面一直 401 / 403？

401 按顺序查：三枚签名头是否齐全 → 本机时钟是否飘了（时间窗 5 分钟）→ nonce 是否重复使用 → 凭据是否被吊销。403 是角色不够——复位要 operator，签发要 admin_secret。签名别手写，`from satori_gateway.security import sign_request`。

### Q: 为什么要给本地面板也搞凭据？不麻烦吗？

因为控制面端点改的是审计结论本身（详见 [SECURITY.md](SECURITY.md) 第 1 节）。回环 + 引导 token + 签一次 operator 是单人场景的最小闭环，五分钟的事。读面（status / live）是无鉴权的，面板看数据不需要凭据——只有按按钮才要。

## 面板与部署

### Q: 面板连不上 / 事件流一直空？

九成是 WebSocket 没跑起来：确认装的是 `uvicorn[standard]`（`pip install -e .` 默认就是，standard extra 提供 websockets）。反代场景确认 WS 升级头被转发。面板里网关地址填错也会连不上——它默认 `http://127.0.0.1:8400`，存在浏览器 localStorage 里。

### Q: 面板上的凭据安全吗？

admin 口令和 operator secret 只活在页面 JS 变量里，不落 localStorage，刷新即消失。签发的 secret 以一次性横幅展示，点"我已保存，销毁横幅"即焚。面板只走 HMAC；Ed25519 凭据请用 CLI。

### Q: 能把 Satori 部署到团队共享的机器上吗？

能，但按规矩来：`admin_secret` 必须配（非回环不配拒启动）、`require_tls = true`、多团队上 Ed25519、读面拿反代加鉴权。代理面 `/v1/*` 不认识客户端——谁连上都能用你代持的 key，别把代理面裸奔到公网。

## 实战

### Q: 怀疑被掺水了，按什么顺序查？

1. `GET /satori/status`：看 `suspicion` 段哪个 上游×模型 在涨分，hits 是 quality 还是 identity——身份类涨分基本坐实换模型，质量类涨分先看是不是降智。
2. 面板/`logs/satori.log`：看具体命中了哪几条规则、JS 散度多少、答案相似度多少。
3. 开了录制的话 `satori replay records/当天.jsonl` 重审，附 `--tool-traces` 看工具链。
4. 主动验证：换官方 key 直连同一模型发同样的题，对比输出。
5. 定性之后三选一：误报 → `satori feedback --reason false_alarm --confirm` 清账；官方更新 → `--reason official_update --confirm` 退役重采；实锤掺水 → 截图取证找上游对线，换家。

### Q: 上游 90% 真 10% 假地掺，能抓到吗？

这正是命中率通道的设计场景。掺水意味着每十笔交易就有一笔留下指纹：规则引擎逐条记分，累积速度远超半衰期衰减；严重规则命中率 EMA 持续超过 5% 就告警（`hit_rate_threshold` 可调）。掺得越少抓得越慢——低到 1% 量级时，任何系统都只能在更长窗口上说话了。

### Q: 熔断器会误伤正常流量吗？

拦截只针对越界的那一个 上游×模型，其他上游照常。越界本身要求多信号叠加，误伤概率低。怕打断工作流就把 `[breaker] enabled` 保持 false，只告警不拦截——但那样"味道变了"的流量会继续流进你的项目，自己权衡。

### Q: 录制功能安全吗？

`[record] enabled = true` 会把对话原文落盘到 `records/`，默认关。涉敏场景谨慎开启，管好目录权限（已在 .gitignore 排除）。工具链 trace 不含原始参数文本，只留结构化 flags。

### Q: Windows 下 curl 测中文请求报 400？

控制台的锅：`-d` 里的中文被转成 GBK，JSON 直接坏掉。写文件发帖：

```bash
printf '%s' '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}' > req.json
curl -X POST http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" --data-binary @req.json
```

## 扩展

### Q: 参考指纹能分享吗？

能，而且鼓励。`fingerprints/` 里的 JSON 是纯文本分布数据，可以像杀毒特征库一样社区共享——谁也不求大厂。文件名含端点信息，分享前看一眼。采集时带 `--source` 和 `--notes`，让下游知道该给多少信任权重。

### Q: 怎么加自己的规则 / checker / 协议适配器？

规则写在 `rules.toml`（同名覆盖内置，可以调分）。checker 放 `checkers/`、adapter 放 `adapters/`、上游协议放 `pipelines/`，都是装饰器 + 包扫描自动发现，不用改任何现有文件。模板见 [HANDBOOK.md](HANDBOOK.md) 第 10 节。

## 局限（丑话）

- 我们不证明"这是真模型"，只发现"这不是当初那个模型"。行为指纹是概率性的。
- 单通道都可被针对性绕过，项目的意义在于叠加后的绕过成本。
- 指纹会随官方快照/量化更新轻微漂移，参考要定期重采——TTL 就是干这个的。
- 测试锚点抓降级，声纹抓同级掉包，两条链互补；套件的下限就是标尺的下限。
- Slop 是结构化启发式：参数 JSON 断了认得出，"参数选错但 JSON 合法"认不出。
- 参数相关、时段相关的局部降级，只有你的套件覆盖到那个模式才抓得到。
- HMAC 模式服务端存 secret 原文——管好 `state/credentials.db`；跨网团队请上 Ed25519。
