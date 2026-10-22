#!/usr/bin/env python3
"""【可选·联网】从 models.dev 拉公开 USD 价，补全 prices.json 缺失的模型条目。

独立于主工具：oc_usage.py 永不联网；不需要本功能可直接删除本文件。
运行前需要显式确认（--yes 可跳过交互，用于脚本化场景）。

只补缺、不覆盖已有条目。models.dev 是美元价，按用户指定汇率换算（**不联网拉取实时汇率**，
避免额外网络依赖与不可复现），换算值仅供参考；供应商官方本币价（如 MiMo 的 ¥ 价）
请手工核对后覆盖。
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent / "prices.json"
URL = "https://models.opencode.ai/api.json"
SUGGESTED_RATE = 6.6747
RATE_SOURCE = "finscope 2026-10-23 UTC+8 02:35"


def load_raw():
    if not PRICES_PATH.exists():
        return {}
    try:
        return json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def existing_keys():
    return {k for k, v in load_raw().items() if not k.startswith("_") and isinstance(v, dict)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="【联网】从 models.dev 拉公开价补全 prices.json（只补缺不覆盖）")
    ap.add_argument("--rate", type=float, default=None,
                    help="USD→目标货币的汇率；省略则交互询问（建议值见提示），不联网拉取实时汇率")
    ap.add_argument("--provider", help="只同步供应商 id 子串，如 xiaomi")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（自动化用）")
    ap.add_argument("--dry-run", action="store_true", help="只预览将补全的条目，不写文件")
    args = ap.parse_args()

    print("本操作将访问互联网：", URL)
    print("用途：获取公开 USD 定价，按汇率换算后补全 prices.json 中缺失的模型（不覆盖已有条目）。")
    print("注意：汇率需人工指定，不联网拉取实时汇率。")
    if not args.yes:
        try:
            ans = input("确认继续？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("已取消，未产生任何网络请求与文件改动。")
            return

    if args.rate is None:
        try:
            raw = input(f"USD→目标货币 汇率（建议 {SUGGESTED_RATE}，来源 {RATE_SOURCE}）[{SUGGESTED_RATE}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        try:
            args.rate = float(raw) if raw else SUGGESTED_RATE
        except ValueError:
            print("汇率输入无效，已取消。")
            return
        if not args.yes and not raw:
            print(f"未输入，采用建议值 {args.rate}（换算价仅供参考）。")
    print(f"使用汇率：USD × {args.rate}")

    req = urllib.request.Request(URL, headers={"User-Agent": "oc-usage-sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)

    known = existing_keys()
    added = {}
    for pid, prov in data.items():
        if args.provider and args.provider.lower() not in pid.lower():
            continue
        for mid, meta in (prov.get("models") or {}).items():
            if mid in known:
                continue
            cost = meta.get("cost") or {}
            if not cost:
                continue
            inp = cost.get("input") or 0.0
            out = cost.get("output") or 0.0
            crd = cost.get("cache_read") or 0.0
            if inp == 0 and out == 0 and crd == 0:
                continue  # 公开价全 0 视为无定价，留给「缺省」显示
            added[mid] = {
                "miss": round(inp * args.rate, 6),
                "cache_read": round(crd * args.rate, 6),
                "cache_write": round(inp * args.rate, 6),
                "output": round(out * args.rate, 6),
            }

    if not added:
        print("没有需要补全的模型（prices.json 已覆盖或该供应商无公开价）。")
        return

    print(f"将补全 {len(added)} 个模型（USD × {args.rate}）：")
    for mid in sorted(added):
        print(f"  {mid}: {added[mid]}")

    if args.dry_run:
        print("--dry-run：未写入文件。")
        return

    merged = load_raw()
    merged.update(added)
    PRICES_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {PRICES_PATH}（未覆盖任何已有条目）。")
    print("提示：换算价仅供参考，请按供应商官方价核对后手工覆盖。")


if __name__ == "__main__":
    main()
