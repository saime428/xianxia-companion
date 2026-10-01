import hashlib
import json
import os
from pathlib import Path
import time


BASE_DIR = Path(__file__).resolve().parent.parent


def runtime_source_files() -> list[str]:
    # 整个 app/src 的 .py 都算：手工清单漏过世界 Boss、天机命脉、external_sync（09-28 审计 A3），
    # 只改了这些文件、忘了重启时，健康检查照样报 telegram_code_current=true
    return sorted(path.relative_to(BASE_DIR).as_posix() for path in BASE_DIR.rglob("*.py"))


def compute_runtime_code_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative_path in runtime_source_files():
        path = BASE_DIR / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def build_runtime_status(component: str, *, started_at: float) -> dict:
    return {
        "component": component,
        "pid": os.getpid(),
        "started_at": float(started_at),
        "updated_at": time.time(),
        "code_fingerprint": compute_runtime_code_fingerprint(),
        "capabilities": ["deployment_drain_v1"],
    }


def dump_runtime_status(status: dict) -> str:
    return json.dumps(status, ensure_ascii=False, sort_keys=True)


def load_runtime_status(value: str) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
