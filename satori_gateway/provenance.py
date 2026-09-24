"""基线溯源 sidecar（v4 Phase 0.1）：每份参考旁边一份"出身证明"。

sidecar 与参考文件同目录同名（`{safe}.meta.json`），随参考一起归档/迁移，
不会失联。缺失 sidecar 的存量参考一律按 secondhand 0.5 处理（见
baseline.py BaselineManager._provenance）。

明文 secret 式的教训不在这里——这是"自报出身"，防的是遗忘与混淆：
三个月后你还记得这份 gpt-4o 参考是从哪个中转采的吗？
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .pressure import ProviderPressure

COLLECTOR_VERSION = "0.1.0"


def sidecar_path(reference: Path) -> Path:
    """参考文件 → sidecar 路径：openai--gpt-4o.json → openai--gpt-4o.meta.json"""
    return reference.with_suffix(".meta.json")


def probe_hash(prompt: str | None) -> str | None:
    if prompt is None:
        return None
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]


def write_sidecar(
    reference: Path,
    *,
    source: str,
    pressure_level: str | None = None,
    notes: str = "",
    prompt: str | None = None,
    vendor: str | None = None,
    collected_at: float | None = None,
) -> Path:
    """给一份刚写下的参考文件写出身证明，返回 sidecar 路径。"""
    if pressure_level is None:
        pressure_level = ProviderPressure(vendor or "").level_at() if vendor else "MID"
    meta = {
        "source": source,  # official / secondhand / community
        "collected_at": collected_at or time.time(),
        "collector": f"satori-gateway/{COLLECTOR_VERSION}",
        "pressure_level": pressure_level,  # LOW/MID/HIGH/EXTR
        "probe_prompt_hash": probe_hash(prompt),
        "notes": notes,
    }
    path = sidecar_path(reference)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_sidecar(reference: Path) -> dict:
    path = sidecar_path(reference)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
