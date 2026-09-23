"""模板引用的每个 companion_auto_state.<key> 都必须在 COMPANION_AUTO_FEATURES 里。

页面渲染先铺一份默认态（8 个 key 全用 None 构建，恒为「未开启」），再按
COMPANION_AUTO_FEATURES 逐个用数据库真实任务覆盖。漏注册的 key 拿不到真实
状态：页面按假状态画按钮、切换路由按数据库开关，点一下开、页面不变、再点
一下又关——「自动引道」就是这么坏的。

运行：.venv/bin/python tools/test_companion_auto_state_keys.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "src"))

from tg_game.web.biz_companion_view_model import COMPANION_AUTO_FEATURES

TEMPLATES = ROOT / "app" / "assets" / "templates"
PATTERN = re.compile(r"companion_auto_state\.([a-z0-9_]+)")


def main() -> None:
    referenced: dict[str, set[str]] = {}
    for path in TEMPLATES.rglob("*.html"):
        keys = set(PATTERN.findall(path.read_text(encoding="utf-8")))
        if keys:
            referenced[path.relative_to(TEMPLATES).as_posix()] = keys
    assert referenced, "没扫到任何 companion_auto_state 引用，正则或路径坏了"

    missing = {
        name: sorted(keys - set(COMPANION_AUTO_FEATURES))
        for name, keys in referenced.items()
        if keys - set(COMPANION_AUTO_FEATURES)
    }
    assert not missing, f"模板引用了未注册的自动开关，页面会恒显示未开启：{missing}"

    # 这几个是历史上真踩过的，钉住防回归
    assert "taiyi_yindao" in referenced.get("modules/sect.html", set())
    assert "soul_cultivation" in referenced.get("modules/cultivation.html", set())
    for key in ("taiyi_yindao", "soul_cultivation"):
        assert key in COMPANION_AUTO_FEATURES, key
    print("ok")


if __name__ == "__main__":
    main()
