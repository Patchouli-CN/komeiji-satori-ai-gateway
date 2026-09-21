"""satori-test-mock：本地 Mock Server（v4 Phase 5B/6E）。

开发/CI 环境验证上报逻辑用：不起真网关，接收 report、模拟裁决响应、
落盘全部上报供断言。

    satori-test-mock --port 8401                     # 接收任意签名
    satori-test-mock --port 8401 --secret s3cret     # 校验 HMAC 签名
    satori-test-mock --reject                        # 总是 500（测容错）

HEAD / 返回统计；GET /satori/test/reports 拿全部上报（JSON）。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_app(secret: str = "", reject: bool = False,
               store_file: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="satori-test-mock")
    state = {"reports": [], "rejected": 0}
    lock = threading.Lock()
    store_path = Path(store_file) if store_file else None

    @app.post("/satori/test/report")
    async def report(request: Request):
        body = await request.body()
        if reject:
            with lock:
                state["rejected"] += 1
            return JSONResponse({"error": "mock reject"}, status_code=500)
        if secret:
            ts = request.headers.get("X-Satori-Timestamp", "")
            sig = request.headers.get("X-Satori-Signature", "")
            expected = hmac.new(secret.encode(), f"{ts}.".encode() + body,
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig):
                return JSONResponse({"error": "bad signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        record = {"received_at": time.time(), "body": payload,
                  "reporter": request.headers.get("X-Satori-Reporter", "")}
        with lock:
            state["reports"].append(record)
            if store_path is not None:
                with store_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        # 模拟裁决响应：通过/失败都按 Satori 的返回形状给
        status = payload.get("status")
        return {"accepted": True,
                "action": "recorded" if status == "pass" else "recorded",
                "detail": "mock 不裁决，只记录"}

    @app.get("/satori/test/reports")
    async def reports():
        with lock:
            return {"count": len(state["reports"]),
                    "rejected": state["rejected"],
                    "reports": list(state["reports"])}

    @app.get("/")
    async def root():
        with lock:
            return {"mock": True, "reports": len(state["reports"])}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="satori-test-mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8401)
    parser.add_argument("--secret", default="",
                        help="配置后校验 HMAC 签名（模拟真网关的鉴权）")
    parser.add_argument("--reject", action="store_true",
                        help="总是返回 500——验证 reporter 的容错入队")
    parser.add_argument("--store", default=None,
                        help="上报落盘文件（JSONL，供断言）")
    args = parser.parse_args(argv)
    app = create_app(secret=args.secret, reject=args.reject,
                     store_file=args.store)
    print(f"[mock] satori-test-mock 监听 http://{args.host}:{args.port}"
          f"（{'校验签名' if args.secret else '接收任意签名'}）")
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
