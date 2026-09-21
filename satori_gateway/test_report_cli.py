"""satori-test-report：通用 HTTP Reporter CLI（v4 Phase 5B）。

给不用 pytest 的生态用：Java/JUnit、C++/GTest、Go/testing……把它们的
报告文件转成 Satori 的 test/report 上报。

    satori-test-report results.xml                       # 自动探测格式
    satori-test-report results.tap --format tap
    satori-test-report results.json --suite core --level L0

凭据同样走环境变量：SATORI_URL / SATORI_REPORTER / SATORI_SECRET（或
SATORI_METHOD=ed25519 + SATORI_PRIVATE_KEY）。Satori 不可达时进本地队列，
不报错退出码 2——CI 不该因为观测系统挂了而红。
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET

from .reporting import SatoriReporter

_PASS = {"pass", "passed", "ok", "success"}
_FAIL = {"fail", "failed", "failure", "error"}
_SKIP = {"skip", "skipped", "xfail", "ignored"}


def parse_junit(path: str) -> list[dict]:
    """JUnit XML → [{name, status, suite, detail}]。"""
    tree = ET.parse(path)
    out: list[dict] = []
    for case in tree.getroot().iter("testcase"):
        status, detail = "pass", ""
        for child in case:
            tag = child.tag.lower()
            if tag in ("failure", "error"):
                status, detail = "fail", (child.text or child.get("message", ""))[:2000]
                break
            if tag == "skipped":
                status, detail = "skip", (child.get("message") or "")[:200]
        out.append({
            "name": case.get("name", ""),
            "suite": case.get("classname") or case.get("suite") or "junit",
            "status": status,
            "detail": detail,
        })
    return out


def parse_tap(path: str) -> list[dict]:
    """TAP → [{name, status, suite, detail}]。YAML 诊断块取 message 行。"""
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or line.startswith("TAP "):
            continue
        if line.startswith("ok") or line.startswith("not ok"):
            failed = line.startswith("not ok")
            rest = line[len("not ok") if failed else len("ok"):].strip()
            # rest: [编号] [- ]名字  [# SKIP 原因]
            rest = rest.lstrip("0123456789").strip()
            if rest.startswith("- "):
                rest = rest[2:].strip()
            detail = ""
            if "#" in rest:
                rest, _, reason = rest.partition("#")
                detail = reason.strip()
            name = rest.strip()
            status = "fail" if failed else (
                "skip" if "SKIP" in line.upper() else "pass")
            if failed and not detail and i < len(lines):
                # 下一块 YAML 的 message: 行
                j = i
                while j < len(lines) and (lines[j].startswith(" ") or
                                          lines[j].strip().startswith("---")):
                    stripped = lines[j].strip().lstrip("-").strip()
                    if stripped.startswith("message:"):
                        detail = stripped[len("message:"):].strip()[:2000]
                        break
                    j += 1
            out.append({"name": name or "(unnamed)", "suite": "tap",
                        "status": status, "detail": detail})
    return out


def parse_json(path: str) -> list[dict]:
    """JSON：{"tests": [...]} 或直接 [...]，元素含 name/status(+detail/suite)。"""
    data = json.loads(open(path, encoding="utf-8").read())
    cases = data.get("tests") if isinstance(data, dict) else data
    out: list[dict] = []
    for c in cases or []:
        status = str(c.get("status", c.get("outcome", ""))).lower()
        status = ("pass" if status in _PASS else
                  "skip" if status in _SKIP else
                  "fail" if status in _FAIL else status)
        out.append({
            "name": c.get("name", c.get("test", "(unnamed)")),
            "suite": c.get("suite", c.get("classname", "json")),
            "status": status,
            "detail": str(c.get("detail", c.get("message", "")))[:2000],
        })
    return out


def detect_format(path: str) -> str:
    head = open(path, encoding="utf-8", errors="replace").read(2048).lstrip()
    if head.startswith("<?xml") or head.startswith("<testsuite") or head.startswith("<testsuites"):
        return "junit"
    if head.startswith("[") or head.startswith("{"):
        return "json"
    return "tap"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="satori-test-report",
        description="把测试报告文件上报给 KomeijiSatori")
    parser.add_argument("files", nargs="+", help="报告文件（junit.xml / .tap / .json）")
    parser.add_argument("--format", choices=["auto", "junit", "tap", "json"],
                        default="auto")
    parser.add_argument("--suite", default=None, help="覆盖套件名")
    parser.add_argument("--level", default="L1", help="L0/L1/L2/L3（默认 L1）")
    args = parser.parse_args(argv)

    reporter = SatoriReporter.from_env()
    if reporter is None:
        print("SATORI_URL 未配置——不上报", file=sys.stderr)
        return 2

    sent = failed = skipped = 0
    for path in args.files:
        fmt = args.format if args.format != "auto" else detect_format(path)
        cases = {"junit": parse_junit, "tap": parse_tap,
                 "json": parse_json}[fmt](path)
        for case in cases:
            if case["status"] == "skip":
                skipped += 1  # skip 不裁决，也不上报（不会进滑窗）
                continue
            report = reporter.build_payload(
                test_suite=args.suite or case["suite"],
                test_name=case["name"],
                status=case["status"],
                level=args.level,
                failure_diff=case["detail"],
            )
            result = reporter.send(report)
            if result.ok:
                sent += 1
            else:
                failed += 1
                print(f"[satori] 上报失败（已入队）：{case['name']} → "
                      f"{result.status_code} {result.detail}", file=sys.stderr)
    flushed = reporter.flush_queue()
    recovered = sum(1 for r in flushed if r.ok)
    print(f"[satori] 上报 {sent} 条（失败入队 {failed}，skip 跳过 {skipped}，"
          f"补发历史 {recovered}/{len(flushed)}）")
    # CI 不该因为观测系统挂了而红：入队即视为已交付（容错验证）
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
