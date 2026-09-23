"""CLI 入口：
  satori serve [--config third_eye.toml]          启动网关
  satori collect --upstream NAME --model MODEL    从可信端点采集参考指纹
  satori answers --upstream NAME --model MODEL    采集答案指纹参考作答
  satori feedback --upstream U --model M --reason official_update --confirm
                                                  误报反馈/确认官方更新（一键退役基线）
  satori replay FILE                              用当前规则回放取证录制文件
  satori ingest TRANSCRIPT.md --out FILE          把 markdown 会话记录转成录制格式
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import httpx
import uvicorn

from .app import KomeijiSatori
from .checkers.answerprint import answers_path, collect_answers
from .checkers.fingerprint import probe, reference_path
from .config import Config, load
from .logger import setup_logging
from .pipelines import build_pipeline
from .pressure import ProviderPressure
from .provenance import write_sidecar
from .record import ingest_markdown, replay, replay_tool_traces
from .registry import build_checkers
from .rules import RuleEngine, load_builtin_rules, load_rules, merge_rules
from .security import sign_request
from .state import StateStore


def _build_engine(cfg: Config) -> RuleEngine:
    builtin = load_builtin_rules()
    user = load_rules(cfg.rules.path) if cfg.rules.path.exists() else []
    rules = merge_rules(builtin, user)
    logging.getLogger("satori").info(
        "规则引擎就绪：内置 %d 条 + 用户 %d 条 → 生效 %d 条",
        len(builtin), len(user), len(rules),
    )
    return RuleEngine(rules)


def _record_refreshed(cfg: Config, upstream: str, model: str,
                      pressure_level: str, source: str) -> None:
    """采集成功记一笔 refreshed 事件（v4 Phase 2：TTL 的学习原料——
    与 feedback 退役事件一起喂给动态保质期引擎）。
    事件按 (upstream, model) 归属：官方与中转同名模型的账不混。"""
    store = StateStore(cfg.state.directory)
    events = store.read_baseline_events(model=model, upstream=upstream)
    last_ts = max((e.get("ts", 0.0) for e in events), default=None)
    store.append_baseline_event({
        "ts": time.time(), "upstream": upstream, "model": model,
        "event": "refreshed",
        "pressure_level_at_collect": pressure_level, "source": source,
        "days_since_last_refresh": (
            (time.time() - last_ts) / 86400 if last_ts else None),
    })


def _clear_retired_meta(cfg: Config, upstream: str, model: str) -> None:
    """重采成功后清理归档里的退役标记——否则基线恢复了，
    startup_report 还在拿旧退役时间误报 BASIC。"""
    meta_path = cfg.state.archive_dir / f"{upstream}--{model}" / "meta.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if meta.pop("retired_at", None) is not None:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"[baseline] {upstream}/{model} 已重采——退役标记清除，"
              "判别等级按可信度重新计算")


def _pressure_gate(vendor: str, force_pressure: bool,
                   wait_for_low: bool) -> str:
    """采集前压力闸门（v4 Phase 0.2）：HIGH 警告询问，--force-pressure 覆盖，
    --wait-for-low 阻塞到黄金窗口。返回实际采集时的压力等级（记入 sidecar）。"""
    pressure = ProviderPressure(vendor)
    if wait_for_low:
        target = pressure.next_golden_window()
        print(f"[pressure] 等待 LOW/MID 窗口（约 {(target - time.time()) / 60:.0f} 分钟后）…")
        while pressure.level_at() not in ("LOW", "MID"):
            time.sleep(60)
    level = pressure.level_at()
    if level in ("HIGH", "EXTR") and not force_pressure:
        print(f"[WARN] {pressure.describe()}")
        try:
            answer = input("Wait? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer != "y":
            raise SystemExit("已取消——高压窗口采集的基线会被降权"
                             "（或用 --force-pressure 强制）")
        level = pressure.level_at()
    return level


async def _collect(config_path: str, upstream_name: str, model: str,
                   prompt: str | None, source: str, notes: str,
                   force_pressure: bool, wait_for_low: bool) -> None:
    cfg = load(config_path)
    upstream = next((u for u in cfg.upstreams if u.name == upstream_name), None)
    if upstream is None:
        raise SystemExit(f"配置里找不到 upstream {upstream_name!r}")
    if "logprobs" not in build_pipeline(upstream).capabilities:
        raise SystemExit(
            f"上游 {upstream_name!r}（协议 {upstream.protocol}）不支持 logprobs，"
            "声纹指纹无法采集——改用 satori answers 采集答案指纹"
        )

    level = _pressure_gate(upstream_name, force_pressure, wait_for_low)
    async with httpx.AsyncClient() as client:
        dist = await probe(client, upstream, model, cfg.fingerprint, prompt)

    out = reference_path(cfg.fingerprint, upstream, model, prompt)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dist, ensure_ascii=False, indent=2), encoding="utf-8")
    sidecar = write_sidecar(out, source=source, pressure_level=level,
                            notes=notes, prompt=prompt, vendor=upstream_name)
    _record_refreshed(cfg, upstream_name, model, level, source)
    _clear_retired_meta(cfg, upstream_name, model)
    print(f"参考指纹已写入 {out}（{len(dist)} 个 token）")
    print(f"溯源 sidecar 已写入 {sidecar}（source={source}, pressure={level}）")
    if source != "official":
        print("[WARN] 非 official 来源——信任权重按来源折算，请确认来源可信")


async def _answers(config_path: str, upstream_name: str, model: str,
                   source: str, notes: str, force_pressure: bool,
                   wait_for_low: bool) -> None:
    cfg = load(config_path)
    upstream = next((u for u in cfg.upstreams if u.name == upstream_name), None)
    if upstream is None:
        raise SystemExit(f"配置里找不到 upstream {upstream_name!r}")

    level = _pressure_gate(upstream_name, force_pressure, wait_for_low)
    async with httpx.AsyncClient() as client:
        answers = await collect_answers(client, upstream, model, cfg.answerprint.max_tokens)

    out = answers_path(cfg.fingerprint, upstream, model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
    sidecar = write_sidecar(out, source=source, pressure_level=level,
                            notes=notes, vendor=upstream_name)
    _record_refreshed(cfg, upstream_name, model, level, source)
    _clear_retired_meta(cfg, upstream_name, model)
    print(f"参考作答已写入 {out}（{len(answers)} 题）")
    print(f"溯源 sidecar 已写入 {sidecar}（source={source}, pressure={level}）")
    if source != "official":
        print("[WARN] 非 official 来源——信任权重按来源折算，请确认来源可信")


def _replay(config_path: str, file: str, tool_traces: str | None = None) -> None:
    cfg = load(config_path)
    engine = _build_engine(cfg)
    report = replay(file, engine)

    print(f"回放 {file}：{report.total} 条录制，{report.flagged} 条命中规则")
    for alert in report.tokenwatch_alerts:
        print(f"  [tokenwatch] {alert}")
    for (upstream, model), score in sorted(
        report.suspicion.items(), key=lambda kv: -kv[1]
    ):
        mark = " ⚠️ 越界" if score >= cfg.rules.suspicion_threshold else ""
        print(f"  {upstream}/{model}: 可疑度 {score}{mark}")
    print()
    for entry, hits in report.hits[:20]:
        where = f"{entry.get('upstream')}/{entry.get('model')} @{entry.get('ts', 0):.0f}"
        for h in hits:
            print(f"  [{where}] {h.rule} ({h.score:+d}) [{h.field}] …{h.snippet}…")
    if len(report.hits) > 20:
        print(f"  … 其余 {len(report.hits) - 20} 条命中从略")

    if tool_traces:
        tr = replay_tool_traces(tool_traces)
        print(f"\n回放工具链 {tool_traces}：{tr.total} 条 trace，"
              f"{tr.suspicious} 条可疑 / {tr.clean} 条干净")
        for rec in tr.chains:
            susp = rec.get("slop_score", 0) > 0
            head = (f"  {rec.get('upstream')}/{rec.get('model')} "
                    f"@{rec.get('ts', 0):.0f} trace={rec.get('trace_id')} "
                    f"score={rec.get('slop_score', 0):.0f}"
                    f"{' ⚠️' if susp else ''}")
            print(head)
            for s in rec.get("steps", []):
                flags = f" [{', '.join(s['flags'])}]" if s.get("flags") else ""
                first = " ↳ 覚「この呼び出し、少し違和感が…」" \
                    if rec.get("first_suspicious") == s.get("index") else ""
                print(f"    #{s.get('index')} {s.get('tool')} "
                      f"args={s.get('args_bytes')}B valid={s.get('args_valid')}"
                      f"{flags}{first}")


def _ingest(transcript: str, out: str, upstream: str, model: str) -> None:
    entries = list(ingest_markdown(transcript, upstream=upstream, model=model))
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"已导入 {len(entries)} 对会话 → {out_path}")


def _feedback(config_path: str, upstream: str, model: str, reason: str,
              note: str, confirm: bool, reporter: str, secret: str) -> None:
    """向运行中的网关提交误报反馈（v4 Phase 1.1 一键确认）。"""
    cfg = load(config_path)
    if secret.startswith("env:"):
        secret = os.environ.get(secret[4:], "")
    body = {"upstream": upstream, "model": model, "reason": reason,
            "note": note, "confirm": confirm}
    raw = json.dumps(body, ensure_ascii=False).encode()
    headers = {"X-Satori-Reporter": reporter, **sign_request(secret, raw)}
    url = (f"http://{cfg.gateway.host}:{cfg.gateway.port}"
           f"/satori/baseline/feedback")
    r = httpx.post(url, content=raw, headers=headers, timeout=30)
    if r.status_code >= 400:
        print(f"[{r.status_code}] {r.text}")
        raise SystemExit(1)
    action = r.json().get("action", "")
    print(f"[{r.status_code}] {upstream}/{model} action={action}")
    if action.startswith("confirm"):
        print("  已记一笔确认——攒够阈值（或 operator 确认）才退役基线")


def main() -> None:
    setup_logging(log_level="INFO")

    parser = argparse.ArgumentParser(prog="satori", description="KomeijiSatori AI gateway")
    parser.add_argument("--config", default="third_eye.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="启动网关")
    serve.add_argument("--config", default="third_eye.toml")

    collect = sub.add_parser("collect", help="采集参考指纹")
    collect.add_argument("--config", default="third_eye.toml")
    collect.add_argument("--upstream", required=True)
    collect.add_argument("--model", required=True)
    collect.add_argument("--prompt", default=None,
                         help="自定义探针 prompt（身份题组参考用；默认取配置里的 probe_prompt）")
    collect.add_argument("--source", default="official",
                         choices=["official", "secondhand", "community"],
                         help="参考来源（信任权重：official 1.0 / secondhand 0.5 / community 0.3）")
    collect.add_argument("--notes", default="", help="来源备注（为什么信它），写进 sidecar")
    collect.add_argument("--force-pressure", action="store_true",
                         help="高压窗口强制采集（sidecar 记录事实，信任度自己负责）")
    collect.add_argument("--wait-for-low", action="store_true",
                         help="阻塞至 LOW/MID 窗口再采集")

    ans = sub.add_parser("answers", help="采集答案指纹参考作答")
    ans.add_argument("--config", default="third_eye.toml")
    ans.add_argument("--upstream", required=True)
    ans.add_argument("--model", required=True)
    ans.add_argument("--source", default="official",
                     choices=["official", "secondhand", "community"])
    ans.add_argument("--notes", default="")
    ans.add_argument("--force-pressure", action="store_true")
    ans.add_argument("--wait-for-low", action="store_true")

    rp = sub.add_parser("replay", help="回放取证录制文件")
    rp.add_argument("--config", default="third_eye.toml")
    rp.add_argument("file")
    rp.add_argument("--tool-traces", default=None,
                    help="附带回放 Tool Call 链（如 state/tool_traces.jsonl）")

    ig = sub.add_parser("ingest", help="导入 markdown 会话记录为录制格式")
    ig.add_argument("transcript")
    ig.add_argument("--out", required=True)
    ig.add_argument("--upstream", default="import")
    ig.add_argument("--model", default="transcript")

    fb = sub.add_parser("feedback", help="提交误报反馈 / 确认官方更新")
    fb.add_argument("--config", default="third_eye.toml")
    fb.add_argument("--upstream", required=True)
    fb.add_argument("--model", required=True)
    fb.add_argument("--reason", default="false_alarm",
                    choices=["official_update", "network_jitter",
                             "false_alarm", "other"])
    fb.add_argument("--note", default="")
    fb.add_argument("--confirm", action="store_true",
                    help="确认型反馈：official_update 累积到阈值触发基线退役")
    fb.add_argument("--reporter", default="cli")
    fb.add_argument("--secret", default="env:SATORI_SECRET",
                    help="HMAC 共享 secret，支持 env:VAR 形式")

    args = parser.parse_args()

    if args.cmd == "collect":
        asyncio.run(_collect(args.config, args.upstream, args.model, args.prompt,
                             args.source, args.notes, args.force_pressure,
                             args.wait_for_low))
        return
    if args.cmd == "answers":
        asyncio.run(_answers(args.config, args.upstream, args.model,
                             args.source, args.notes, args.force_pressure,
                             args.wait_for_low))
        return
    if args.cmd == "replay":
        _replay(args.config, args.file, args.tool_traces)
        return
    if args.cmd == "ingest":
        _ingest(args.transcript, args.out, args.upstream, args.model)
        return
    if args.cmd == "feedback":
        _feedback(args.config, args.upstream, args.model, args.reason,
                  args.note, args.confirm, args.reporter, args.secret)
        return

    cfg = load(args.config)
    # 按配置重建日志（级别/文件），uvicorn 的日志交给 loguru 拦截
    setup_logging(log_level=cfg.logging.level, log_file=cfg.logging.file or None)
    satori = KomeijiSatori(cfg, build_checkers(cfg), _build_engine(cfg))
    uvicorn.run(satori.app, host=cfg.gateway.host, port=cfg.gateway.port,
                log_config=None)


if __name__ == "__main__":
    main()
