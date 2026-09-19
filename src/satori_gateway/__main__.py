"""CLI 入口：
  satori serve [--config third_eye.toml]          启动网关
  satori collect --upstream NAME --model MODEL    从可信端点采集参考指纹
  satori replay FILE                              用当前规则回放取证录制文件
  satori ingest TRANSCRIPT.md --out FILE          把 markdown 会话记录转成录制格式
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import httpx
import uvicorn

from .app import KomeijiSatori
from .checkers.answerprint import answers_path, collect_answers
from .checkers.fingerprint import probe, reference_path
from .config import Config, load
from .logger import setup_logging
from .pipelines import build_pipeline
from .record import ingest_markdown, replay
from .registry import build_checkers
from .rules import RuleEngine, load_builtin_rules, load_rules, merge_rules


def _build_engine(cfg: Config) -> RuleEngine:
    builtin = load_builtin_rules()
    user = load_rules(cfg.rules.path) if cfg.rules.path.exists() else []
    rules = merge_rules(builtin, user)
    logging.getLogger("satori").info(
        "规则引擎就绪：内置 %d 条 + 用户 %d 条 → 生效 %d 条",
        len(builtin), len(user), len(rules),
    )
    return RuleEngine(rules)


async def _collect(config_path: str, upstream_name: str, model: str, prompt: str | None) -> None:
    cfg = load(config_path)
    upstream = next((u for u in cfg.upstreams if u.name == upstream_name), None)
    if upstream is None:
        raise SystemExit(f"配置里找不到 upstream {upstream_name!r}")
    if "logprobs" not in build_pipeline(upstream).capabilities:
        raise SystemExit(
            f"上游 {upstream_name!r}（协议 {upstream.protocol}）不支持 logprobs，"
            "声纹指纹无法采集——改用 satori answers 采集答案指纹"
        )

    async with httpx.AsyncClient() as client:
        dist = await probe(client, upstream, model, cfg.fingerprint, prompt)

    out = reference_path(cfg.fingerprint, upstream, model, prompt)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dist, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"参考指纹已写入 {out}（{len(dist)} 个 token）")


async def _answers(config_path: str, upstream_name: str, model: str) -> None:
    cfg = load(config_path)
    upstream = next((u for u in cfg.upstreams if u.name == upstream_name), None)
    if upstream is None:
        raise SystemExit(f"配置里找不到 upstream {upstream_name!r}")

    async with httpx.AsyncClient() as client:
        answers = await collect_answers(client, upstream, model, cfg.answerprint.max_tokens)

    out = answers_path(cfg.fingerprint, upstream, model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(answers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"参考作答已写入 {out}（{len(answers)} 题）")


def _replay(config_path: str, file: str) -> None:
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


def _ingest(transcript: str, out: str, upstream: str, model: str) -> None:
    entries = list(ingest_markdown(transcript, upstream=upstream, model=model))
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"已导入 {len(entries)} 对会话 → {out_path}")


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

    ans = sub.add_parser("answers", help="采集答案指纹参考作答")
    ans.add_argument("--config", default="third_eye.toml")
    ans.add_argument("--upstream", required=True)
    ans.add_argument("--model", required=True)

    rp = sub.add_parser("replay", help="回放取证录制文件")
    rp.add_argument("--config", default="third_eye.toml")
    rp.add_argument("file")

    ig = sub.add_parser("ingest", help="导入 markdown 会话记录为录制格式")
    ig.add_argument("transcript")
    ig.add_argument("--out", required=True)
    ig.add_argument("--upstream", default="import")
    ig.add_argument("--model", default="transcript")

    args = parser.parse_args()

    if args.cmd == "collect":
        asyncio.run(_collect(args.config, args.upstream, args.model, args.prompt))
        return
    if args.cmd == "answers":
        asyncio.run(_answers(args.config, args.upstream, args.model))
        return
    if args.cmd == "replay":
        _replay(args.config, args.file)
        return
    if args.cmd == "ingest":
        _ingest(args.transcript, args.out, args.upstream, args.model)
        return

    cfg = load(args.config)
    # 按配置重建日志（级别/文件），uvicorn 的日志交给 loguru 拦截
    setup_logging(log_level=cfg.logging.level, log_file=cfg.logging.file or None)
    satori = KomeijiSatori(cfg, build_checkers(cfg), _build_engine(cfg))
    uvicorn.run(satori.app, host=cfg.gateway.host, port=cfg.gateway.port,
                log_config=None)


if __name__ == "__main__":
    main()
