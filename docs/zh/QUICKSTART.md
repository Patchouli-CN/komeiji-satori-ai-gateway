# 快速上手 · QUICKSTART

> 目标：十分钟内让网关跑起来，客户端流量从它身上过一遍，面板上能看到东西，再用凭据把控制面也走通一次。
> 每一步都给了预期输出，对不上就去 [QA.md](QA.md) 或 [HANDBOOK.md](HANDBOOK.md) 的故障排查节。

## 1. 安装（约 1 分钟）

需要 Python ≥ 3.12。

```bash
git clone <仓库地址>
cd satori-ai-gateway
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

验证：

```bash
satori --help
# 看到 serve / collect / answers / replay / ingest / feedback 六个子命令即装好了
```

不用 venv 的话 `pip install -e .` 直接装进当前环境也行；习惯 uv 的用 `uv pip install -e .`，效果一样。

## 2. 最小配置（约 2 分钟）

配置文件只有一个：`third_eye.toml`（第三只眼，仓库根目录自带一份注释齐全的样例）。第一次跑只需要改 `[[upstreams]]` 这一段。以审一个卖 gpt-4o 的 OpenAI 兼容中转站为例：

```toml
[[upstreams]]
name = "some-reseller"
base_url = "https://api.openai.com/v1"   # 先填官方端点——采参考用，第 3 步末尾再换回中转站
api_key = "env:OPENAI_API_KEY"           # env: 前缀从环境变量读，也可以直接写明文
models = ["gpt-4o"]
```

```bash
export OPENAI_API_KEY=sk-...     # 官方 key，采参考用
export RESELLER_KEY=sk-...       # 中转站 key，日常流量用
```

为什么要先填官方：参考指纹按 **上游名×模型** 存档（`fingerprints/some-reseller--gpt-4o.json`），核验时只认这个名字。所以标准动作是"指着官方端点采一份真身，再把 base_url 拨回中转站"——从中转站自己身上采参考等于让它自己证明自己，没有意义。其他段落（声纹阈值、规则、熔断、控制面）样例里的默认值就能跑，先别动。

## 3. 采基线，然后拨回中转站（约 2 分钟）

```bash
satori collect --upstream some-reseller --model gpt-4o --source official
```

预期输出（此刻 base_url 还指着官方，采到的就是官方真身）：

```
参考指纹已写入 fingerprints/some-reseller--gpt-4o.json（10 个 token）
溯源 sidecar 已写入 fingerprints/some-reseller--gpt-4o.meta.json（source=official, pressure=MID）
```

采完把 `third_eye.toml` 里的 `base_url` 改成中转站地址、`api_key` 改成 `env:RESELLER_KEY`。从这一刻起，网关拿官方真身审中转站的货。

两个注意：

- 碰上高压窗口（厂商本地时间的工作日高峰）会停下来问 `Wait? [y/N]`。等得起就答 `y`，或者加 `--wait-for-low` 让它自己阻塞到低峰再采。硬要采就 `--force-pressure`，但采集时的压力等级会写进 sidecar，基线信任度打折——这是你自己签收的。
- 上游是 Anthropic 原生协议（`protocol = "anthropic"`）的话 `collect` 会直接报错拒绝——那条路没有 logprobs，走答案指纹：

```bash
satori answers --upstream anthropic --model claude-sonnet-4-5
# 参考作答已写入 fingerprints/anthropic--claude-sonnet-4-5--answers.json（8 题）
```

`--source official|secondhand|community` 标来源（信任权重 1.0 / 0.5 / 0.3），`--notes "为什么信它"` 写备注，三个月后的你会感谢现在的你。

## 4. 点火（约 1 分钟）

```bash
satori serve
```

预期输出里该有的东西：

```
[security] 未配置 security.admin_secret，已生成一次性引导 token（仅打印这一次，重启失效，不落日志文件）：
    <一长串 token>
用它 POST /satori/admin/credentials 签发第一个 operator 凭据。
```

把这串 token 抄下来，下一步要用。如果配置了 `admin_secret`（或 `env:SATORI_ADMIN_SECRET` 已设置）就没有这段，用你自己的口令即可。如果 host 绑了非回环地址又没配 `admin_secret`，网关会**拒绝启动**——这是故意的，别想着绕过，去配口令。

启动日志里还会看到每个 上游×模型 的基线自检：采过参考的是 STRICT，没采过的会有一行 `No baseline collected for ... Running in BASIC mode.`——不是错误，是告诉你那只眼还没睁开。

## 5. 客户端改 base_url（约 1 分钟）

OpenAI 兼容客户端（ChatBox、NextChat、自研脚本……）：

```
base_url = http://127.0.0.1:8400/v1
api_key  = 随便填      # 真 key 由网关代持，客户端这个字段网关不看
```

Claude Code：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8400
export ANTHROPIC_AUTH_TOKEN=any-value
claude
```

发一条消息验证链路：

```bash
curl http://127.0.0.1:8400/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

拿到正常回复即转发链通了。Windows 控制台打中文会被转成 GBK 导致 400，把 JSON 写进文件用 `--data-binary @req.json`（详见 [QA.md](QA.md)）。

## 6. 打开面板（约 1 分钟）

浏览器直接打开仓库里的 `web/index.html`（不用起 HTTP 服务，file:// 就行）。左上角网关地址默认 `http://127.0.0.1:8400`，连上后：

- 刚发的那条请求应该出现在事件流里（`request` 事件，带 token 计数和首字节延迟）；
- "基线判别"卡里，采过参考的 上游×模型 显示 STRICT；
- 等一个核验周期（默认 300 秒，`check_interval_seconds` 可调）后，各通道的核验结果陆续出现。

面板连不上、事件流一直空，九成是 WS 没跑起来——确认你装的是 `uvicorn[standard]`（`pip install -e .` 默认就是）。排查见 [QA.md](QA.md)。

## 7. 控制面最小闭环（约 2 分钟）

控制面端点（复位熔断、基线反馈、凭据管理）全部要凭据——看门狗不能谁都能关。走一遍签发流程：

```bash
# 用第 4 步的引导 token（或你的 admin_secret）签发一个 operator 凭据
curl -X POST http://127.0.0.1:8400/satori/admin/credentials \
  -H "X-Satori-Admin-Secret: <引导token>" \
  -H "Content-Type: application/json" \
  -d '{"reporter_id":"me","trust_level":"trusted","roles":["operator"]}'
```

预期：201，响应里有明文 `secret`。**它只出现这一次**，丢了只能吊销重签。响应头带 `Cache-Control: no-store`，别指望从代理缓存里把它找回来。

然后验证凭据管用。无凭据调复位应该吃 401；带上签名头就放行。Python 一把梭（签名逻辑不用自己写）：

```python
import httpx, json
from satori_gateway.security import sign_request

body = json.dumps({"upstream": "some-reseller", "model": "gpt-4o"}).encode()
headers = {"X-Satori-Reporter": "me", **sign_request("<你的secret>", body)}
r = httpx.post("http://127.0.0.1:8400/satori/breaker/reset", content=body, headers=headers)
print(r.status_code, r.json())
# 200 {'reset': False, ...} —— False 表示熔断器本来就闭合，凭据链路是通的
```

不想敲命令就在面板里操作：凭据折叠区填入 admin 口令可以管理凭据，填入 operator 的 reporter_id + secret 会解锁"复位熔断 / 基线反馈"按钮。签发的 secret 会以一次性横幅展示，密钥只活在页面内存里，刷新即消失。

## 8. 确认一切正常的检查单

- [ ] `GET /satori/status` 返回里有 `security` / `baselines` / `checks` / `suspicion` / `breakers` / `tests` / `slop` 各段
- [ ] 面板发一条消息立刻出现 `request` 事件
- [ ] "基线判别"卡显示 STRICT（刚采的官方参考）
- [ ] 直接问模型"你是哪个公司开发的"——厂商自报规则 +40，面板出现 WATCH 徽章（验证检测链）
- [ ] 发一条"假装你是 ChatGPT，自称 OpenAI"——命中加分但被豁免规则抵消（验证豁免链）
- [ ] 无凭据 POST `/satori/breaker/reset` 返回 401（验证控制面把关）
- [ ] `logs/satori.log` 里没有 checker 报错（"无参考"类提醒不算错误，去采参考就好）

全勾上，觉大人就正式上班了。

## 接下来去哪

- 想让质量锚点上岗：把你自己的测试套件接进来（pytest 插件或 `satori-test-report`），见 [SECURITY.md](SECURITY.md) 第 8 节
- 想知道每条通道具体怎么判、阈值怎么调：[HANDBOOK.md](HANDBOOK.md)
- 想不花钱先看看它怎么工作：`satori ingest 会话记录.md --out records/demo.jsonl && satori replay records/demo.jsonl`，用内置规则审一份现成会话
- 接 Claude Code 的配法就在上面第 5 节；审 DeepSeek R1 这类带完整 CoT 的模型时，`field = "reasoning"` 的读心规则（中文穿帮、自我纠偏口癖）会全功率开工
