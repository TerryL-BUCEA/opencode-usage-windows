#!/usr/bin/env python3
"""opencode 用量统计：token 拆解、缓存命中率、TPS、人民币费用估算。

数据源：~/.local/share/opencode/opencode.db（只读打开，不干扰运行中的 opencode）
单价表：脚本同目录 prices.json（人民币元/百万 token）。未配置价的模型不计费，显示「缺省」。

用法：
  python oc_usage.py                  # 近 30 天，按天汇总
  python oc_usage.py --days 7
  python oc_usage.py --by model
  python oc_usage.py --by session --days 1
  python oc_usage.py --model flash
  python oc_usage.py --watch          # 常驻仪表盘：当前会话实时用量/命中率/费用
  python oc_usage.py --watch --interval 1
  python oc_usage.py --watch --session ses_e9d3   # 钉住指定会话（id 前缀）
  python oc_usage.py --ascii          # 强制 ASCII 界面（不支持框线/色块的终端）

本工具永不联网；如需从 models.dev 自动补价格，见独立脚本 sync_prices.py（可选，可删除）。
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
PRICES_PATH = SCRIPT_DIR / "prices.json"


def _load_currency():
    try:
        raw = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
        sym = raw.get("_currency")
        if isinstance(sym, str) and sym:
            return sym
    except (OSError, json.JSONDecodeError):
        pass
    return "¥"


CURRENCY = _load_currency()

# 上下文窗口上限：优先只读本地 opencode 配置（纯 JSON 解析，无执行无联网），
# 读不到回退内置默认（与常见 MiMo 模型的 limit.context 一致）
CONTEXT_LIMITS = {"mimo-v2.6": 1048576, "mimo-v2.5": 1000000}
DEFAULT_CONTEXT_LIMIT = 1048576


def _load_config_limits():
    limits = {}
    for p in (
        Path.cwd() / "opencode.json",
        Path.cwd() / "opencode.jsonc",
        Path.home() / ".config" / "opencode" / "opencode.json",
        Path.home() / ".config" / "opencode" / "opencode.jsonc",
    ):
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for prov in (cfg.get("provider") or {}).values():
            if not isinstance(prov, dict):
                continue
            for mid, meta in (prov.get("models") or {}).items():
                if isinstance(meta, dict):
                    lim = (meta.get("limit") or {}).get("context")
                    if isinstance(lim, int) and lim > 0:
                        limits[mid] = lim
    return limits


CONFIG_LIMITS = _load_config_limits()

FIELDS = ("miss", "cache_read", "cache_write", "reasoning", "output")

DIM = "\033[38;5;245m"
LBL = "\033[38;5;250m"
NUM = "\033[1;38;5;255m"
ACC = "\033[38;5;80m"
GRN = "\033[38;5;42m"
YEL = "\033[38;5;214m"
BAR_OFF = "\033[38;5;238m"
BORDER = "\033[38;5;240m"
RESET = "\033[0m"
W_MAX = 42
W = W_MAX
_ANSI_OK = None
_ASCII_MODE = False
_TOPMOST_SUFFIX = ""

ASCII_MAP = str.maketrans({
    "┌": "+", "┐": "+", "└": "+", "┘": "+", "─": "-", "│": "|",
    "├": "+", "┤": "+", "█": "#", "░": "=", "…": ".", "≈": "~",
})


def load_prices():
    """从 prices.json 读价格表；文件缺失/损坏时返回空表（全部显示缺省）。"""
    try:
        data = json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


PRICES = load_prices()


def _setup_console(force_ascii=False):
    """输出编码兜底（防 GBK 管道崩溃）+ Windows VT 探测（仅用于颜色）+ ASCII 降级。"""
    global _ANSI_OK, _ASCII_MODE, DIM, LBL, NUM, ACC, GRN, YEL, BAR_OFF, BORDER, RESET
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if _ANSI_OK is not None:
        return
    ok = False
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)) and k32.SetConsoleMode(handle, mode.value | 0x0004):
            ok = True
    except Exception:
        ok = False
    _ANSI_OK = ok
    if not ok:
        DIM = LBL = NUM = ACC = GRN = YEL = BAR_OFF = BORDER = RESET = ""
    # ASCII 降级：显式要求，或（在真终端里且无 VT，可能是老式控制台/缺字体）
    if force_ascii or os.environ.get("OC_USAGE_ASCII") == "1" or (sys.stdout.isatty() and not ok):
        _ASCII_MODE = True


def _emit(line):
    print(line.translate(ASCII_MAP) if _ASCII_MODE else line)


def _clear_screen():
    # ANSI 清屏在 conhost 会把光标归到缓冲区顶部，导致帧不断下移、滚动条增长。
    # cls/clear 会重置屏幕缓冲区，任何控制台都可靠。
    os.system("cls" if os.name == "nt" else "clear")


def _fit_width():
    global W
    try:
        cols = shutil.get_terminal_size().columns
    except OSError:
        cols = W_MAX + 2
    W = max(30, min(W_MAX, cols - 1))


def _stat_group(label, value, colw, first=False):
    lab = ("  " if first else "") + label
    pad = max(1, colw - _vw(lab) - _vw(value) - 1)
    return [(lab, LBL), (" ", None), (value, NUM), (" " * pad, None)]


def price_for(model):
    """前缀匹配价格表；未配置返回 None（不计费）。"""
    for mid in sorted(PRICES, key=len, reverse=True):
        if model.startswith(mid):
            return PRICES[mid]
    return None


def context_limit(model):
    for mid in sorted(CONFIG_LIMITS, key=len, reverse=True):
        if model.startswith(mid):
            return CONFIG_LIMITS[mid]
    for mid in sorted(CONTEXT_LIMITS, key=len, reverse=True):
        if model.startswith(mid):
            return CONTEXT_LIMITS[mid]
    return DEFAULT_CONTEXT_LIMIT


def msg_cost(tokens, price):
    return (
        (tokens.get("input") or 0) * price["miss"]
        + ((tokens.get("cache") or {}).get("read") or 0) * price["cache_read"]
        + ((tokens.get("cache") or {}).get("write") or 0) * price["cache_write"]
        + ((tokens.get("output") or 0) + (tokens.get("reasoning") or 0)) * price["output"]
    ) / 1_000_000


def new_bucket():
    b = dict.fromkeys(FIELDS, 0)
    b["calls"] = 0
    b["unpriced"] = 0
    b["cost_cny"] = 0.0
    b["oc_cost"] = 0.0  # opencode 自记费用（USD），仅供对照
    return b


def fmt_int(n):
    return f"{n:,}"


def fmt_money(n):
    return f"{CURRENCY}{n:,.2f}"


def fmt_cost(b):
    """未配置价的模型不计费：全缺省显示「缺省」，部分缺省用 + 提示。"""
    if b["unpriced"] and not b["cost_cny"]:
        return "缺省"
    return fmt_money(b["cost_cny"]) + ("+" if b["unpriced"] else "")


def fmt_tokens(n):
    if n < 1000:
        return f"{n}"
    if n < 1_000_000:
        s = f"{n / 1000:.1f}k"
        return "1.00M" if s.startswith("1000") else s
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1_000_000_000:.2f}B"


def _vw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _row(segs):
    plain = "".join(t for t, _ in segs)
    body = "".join(f"{c}{t}{RESET}" if c else t for t, c in segs)
    gap = max(0, W - _vw(plain))
    return f"{BORDER}│{RESET}{body}{' ' * gap}{BORDER}│{RESET}"


def _border(left, mid, right):
    return f"{BORDER}{left}{mid * W}{right}{RESET}"


def _add_msg(b, d):
    t = d.get("tokens") or {}
    model = d.get("modelID") or "unknown"
    b["calls"] += 1
    b["miss"] += t.get("input") or 0
    b["cache_read"] += (t.get("cache") or {}).get("read") or 0
    b["cache_write"] += (t.get("cache") or {}).get("write") or 0
    b["reasoning"] += t.get("reasoning") or 0
    b["output"] += t.get("output") or 0
    price = price_for(model)
    if price is None:
        b["unpriced"] += 1
    else:
        b["cost_cny"] += msg_cost(t, price)
    b["oc_cost"] += d.get("cost") or 0.0


def load_buckets(days, by, model_filter):
    since_ms = int((datetime.now().timestamp() - days * 86400) * 1000)
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "select session_id, time_created, data from message where time_created >= ?",
            (since_ms,),
        ).fetchall()
        titles = {
            sid: (title or "")
            for sid, title in db.execute("select id, title from session")
        }
    finally:
        db.close()

    buckets = defaultdict(new_bucket)
    for session_id, ts, raw in rows:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if d.get("role") != "assistant":
            continue
        model = d.get("modelID") or "unknown"
        if model_filter and model_filter.lower() not in model.lower():
            continue
        if by == "day":
            key = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        elif by == "model":
            key = model
        else:
            title = titles.get(session_id, "")
            key = f"{session_id[:12]} {title[:24]}".strip()
        _add_msg(buckets[key], d)
    return buckets


def print_report(buckets, by, days):
    if not buckets:
        print(f"近 {days} 天没有助手消息记录。")
        return

    head = (
        f"{'分组':<32}{'调用':>5}{'未命中输入':>13}{'缓存读':>14}"
        f"{'缓存写':>11}{'推理':>11}{'输出':>11}{'命中率':>8}{'费用':>11}"
    )
    width = len(head)
    print(head)
    print("-" * width)

    total = new_bucket()
    for key in sorted(buckets):
        b = buckets[key]
        denom = b["cache_read"] + b["miss"]
        hit = (100.0 * b["cache_read"] / denom) if denom else 0.0
        print(
            f"{key:<32}{b['calls']:>5}{fmt_int(b['miss']):>13}{fmt_int(b['cache_read']):>14}"
            f"{fmt_int(b['cache_write']):>11}{fmt_int(b['reasoning']):>11}{fmt_int(b['output']):>11}"
            f"{hit:>7.1f}%{fmt_cost(b):>11}"
        )
        for f in FIELDS:
            total[f] += b[f]
        total["calls"] += b["calls"]
        total["unpriced"] += b["unpriced"]
        total["cost_cny"] += b["cost_cny"]
        total["oc_cost"] += b["oc_cost"]

    print("-" * width)
    denom = total["cache_read"] + total["miss"]
    hit = (100.0 * total["cache_read"] / denom) if denom else 0.0
    print(
        f"{'合计':<32}{total['calls']:>5}{fmt_int(total['miss']):>13}{fmt_int(total['cache_read']):>14}"
        f"{fmt_int(total['cache_write']):>11}{fmt_int(total['reasoning']):>11}{fmt_int(total['output']):>11}"
        f"{hit:>7.1f}%{fmt_cost(total):>11}"
    )
    print()
    print("费用口径：推理 token 按输出价计；单价表 prices.json（元/百万 token）。")
    print("未配置价的模型不计费，费用列显示「缺省」；带 + 表示部分调用缺价。")
    print(f"对照：opencode 自记费用 ≈ ${total['oc_cost']:.4f}（未配置 cost 时恒为 0）")


def collect_session(session_filter=None):
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        if session_filter:
            row = db.execute(
                "select id from session where id like ? order by time_updated desc limit 1",
                (session_filter + "%",),
            ).fetchone()
        else:
            row = db.execute(
                "select id from session order by time_updated desc limit 1"
            ).fetchone()
        if not row:
            return None
        sid = row[0]
        rows = db.execute(
            "select data from message where session_id = ?", (sid,)
        ).fetchall()
    finally:
        db.close()

    st = new_bucket()
    models = set()
    last_model = ""
    last_ts = 0
    user_turns = 0
    gen_events = []  # (生成秒数, 生成 token 数) 用于 TPS
    last_context = 0
    for (raw,) in rows:
        try:
            d = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        role = d.get("role")
        if role == "user":
            user_turns += 1
            continue
        if role != "assistant":
            continue
        t = d.get("tokens") or {}
        model = d.get("modelID") or "unknown"
        models.add(model)
        last_model = model
        _add_msg(st, d)
        tm = d.get("time") or {}
        ts = tm.get("created") or 0
        tc = tm.get("completed") or 0
        if tc > ts:
            gen_events.append(((tc - ts) / 1000.0, (t.get("output") or 0) + (t.get("reasoning") or 0)))
        msg_total = (
            (t.get("input") or 0)
            + ((t.get("cache") or {}).get("read") or 0)
            + ((t.get("cache") or {}).get("write") or 0)
            + (t.get("reasoning") or 0)
            + (t.get("output") or 0)
        )
        if msg_total and ts >= last_ts:
            last_ts = ts
            last_context = msg_total

    tps = 0.0
    if gen_events:
        tail = gen_events[-5:]
        secs = sum(s for s, _ in tail)
        tps = (sum(tk for _, tk in tail) / secs) if secs > 0 else 0.0

    total_tokens = sum(st[f] for f in FIELDS)
    return {
        "id": sid,
        "stats": st,
        "models": models,
        "last_model": last_model,
        "last_ts": last_ts,
        "user_turns": user_turns,
        "tps": tps,
        "context": last_context,
        "limit": context_limit(last_model),
        "total": total_tokens,
    }


def render_dashboard(info, interval):
    _clear_screen()
    _fit_width()
    _emit(_border("┌", "─", "┐"))
    if info is None:
        _emit(_row([("  等待会话数据…", DIM)]))
        _emit(_border("└", "─", "┘"))
        _emit(f"  {DIM}随数据刷新 | q 退出{_TOPMOST_SUFFIX}{RESET}")
        return

    st = info["stats"]
    denom = st["cache_read"] + st["miss"]
    hit = (100.0 * st["cache_read"] / denom) if denom else 0.0
    ctx = (100.0 * info["context"] / info["limit"]) if info["limit"] else 0.0

    short_id = info["id"][:10] + "…"
    hhmmss = (
        datetime.fromtimestamp(info["last_ts"] / 1000).strftime("%H:%M:%S")
        if info["last_ts"]
        else "--:--:--"
    )
    model_text = info["last_model"] or "unknown"
    if len(info["models"]) > 1:
        model_text += f" (+{len(info['models']) - 1})"

    header_parts = [
        (f"  {short_id}", DIM),
        ("  ", None),
        (f"{info['user_turns']}轮 {st['calls']}调用", LBL),
        ("  ", None),
        (hhmmss, DIM),
    ]
    header = []
    vw = 0
    for text, color in header_parts:
        if vw + _vw(text) > W:
            break
        header.append((text, color))
        vw += _vw(text)
    _emit(_row(header))
    _emit(_row([(f"  {model_text}", ACC)]))
    _emit(_border("├", "─", "┤"))
    colw = max(14, (W - 2) // 2)
    cells = (
        ("输入", fmt_tokens(st["miss"])),
        ("缓存读", fmt_tokens(st["cache_read"])),
        ("推理", fmt_tokens(st["reasoning"])),
        ("输出", fmt_tokens(st["output"])),
        ("总", fmt_tokens(info["total"])),
        ("TPS", f"{info['tps']:.1f}"),
    )
    for i in range(0, 6, 2):
        _emit(_row(
            _stat_group(cells[i][0], cells[i][1], colw, first=True)
            + _stat_group(cells[i + 1][0], cells[i + 1][1], colw)
        ))
    _emit(_border("├", "─", "┤"))

    bar_len = max(6, min(16, W - 20))
    filled = min(bar_len, int(round(hit / 100.0 * bar_len)))
    _emit(_row([
        ("  命中率", LBL), (" ", None),
        ("█" * filled, GRN), ("░" * (bar_len - filled), BAR_OFF),
        ("  ", None), (f"{hit:.1f}%", NUM),
    ]))
    filled = min(bar_len, int(round(ctx / 100.0 * bar_len)))
    _emit(_row([
        ("  上下文", LBL), (" ", None),
        ("█" * filled, YEL), ("░" * (bar_len - filled), BAR_OFF),
        ("  ", None), (f"{ctx:.1f}%", NUM),
    ]))
    _emit(_border("├", "─", "┤"))

    left = [("  费用", LBL), (" ", None), (f"≈ {fmt_cost(st)}", NUM)]
    right = [(f"{fmt_tokens(info['context'])}/{fmt_tokens(info['limit'])}", DIM)]
    pad = max(1, W - sum(_vw(t) for t, _ in left) - sum(_vw(t) for t, _ in right))
    _emit(_row(left + [(" " * pad, None)] + right))
    _emit(_border("└", "─", "┘"))
    _emit(f"  {DIM}随数据刷新 | q 退出{_TOPMOST_SUFFIX}{RESET}")


def _set_topmost():
    """把仪表盘窗口置顶（仅 Windows）。返回是否成功。

    两个坑：
    1) Windows Terminal 下 GetConsoleWindow() 是隐藏代理窗口，对它置顶会"假成功"——
       只认 IsWindowVisible 的窗口。
    2) 启动瞬间窗口/标题可能尚未就绪——带重试等待。
    目标窗口定位：优先标题匹配；否则用控制台所属进程的可见顶层窗口。
    """
    try:
        import ctypes
        import time
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # 必须声明 argtypes：不声明时 -1 会被截断成非法句柄，SetWindowPos 静默失败
        u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.SetWindowPos.restype = wintypes.BOOL
        u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowLongW.restype = ctypes.c_long
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

        SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
        WS_EX_TOPMOST = 0x00000008
        HWND_TOPMOST = wintypes.HWND(-1)

        def make_topmost(hwnd):
            u32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
            return bool(u32.GetWindowLongW(hwnd, -20) & WS_EX_TOPMOST)  # GWL_EXSTYLE

        host_pids = set()
        console = k32.GetConsoleWindow()
        if console:
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(console, ctypes.byref(pid))
            host_pids.add(pid.value)

        for _ in range(6):
            by_title, by_pid = [], []
            cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @cb_t
            def _cb(h, _l):
                if not u32.IsWindowVisible(h):
                    return True
                buf = ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(h, buf, 256)
                title = buf.value
                if not title:
                    return True
                if "opencode-usage" in title.lower():
                    by_title.append(h)
                else:
                    wpid = wintypes.DWORD()
                    u32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
                    if wpid.value in host_pids:
                        by_pid.append(h)
                return True

            u32.EnumWindows(_cb, 0)
            for h in by_title + by_pid:
                if make_topmost(h):
                    return True
            time.sleep(0.2)
        return False
    except Exception:
        return False


def watch_loop(args):
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

    if args.topmost:
        ok = _set_topmost()
        globals()["_TOPMOST_SUFFIX"] = " | 置顶" if ok else " | 置顶失败"

    interval = max(0.5, args.interval)
    info = None
    sig = None
    try:
        while True:
            info = collect_session(args.session)
            if info is None:
                new_sig = None
            else:
                st = info["stats"]
                new_sig = (
                    info["id"], info["user_turns"], st["calls"],
                    st["miss"], st["cache_read"], st["cache_write"],
                    st["reasoning"], st["output"], info["last_ts"],
                )
            if new_sig != sig:
                render_dashboard(info, interval)
                sig = new_sig
            deadline = time.time() + interval
            while time.time() < deadline:
                if msvcrt is not None and msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("q", "Q"):
                        raise KeyboardInterrupt
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        _clear_screen()
        if info:
            st = info["stats"]
            print(f"已退出 | {info['user_turns']}轮 {st['calls']}调用 | 费用 ≈ {fmt_cost(st)}")
        else:
            print("已退出")


def main():
    ap = argparse.ArgumentParser(description="opencode token 用量与费用统计")
    ap.add_argument("--days", type=int, default=30, help="统计最近 N 天（默认 30）")
    ap.add_argument("--by", choices=("day", "model", "session"), default="day", help="汇总维度")
    ap.add_argument("--model", help="按模型名子串过滤，如 flash")
    ap.add_argument("--watch", action="store_true", help="常驻仪表盘：当前会话实时用量/命中率/费用")
    ap.add_argument("--interval", type=float, default=2.0, help="watch 轮询间隔秒（默认 2.0，仅数据变化时重绘）")
    ap.add_argument("--session", help="watch 钉住的会话 id 前缀；缺省跟随最近活跃会话")
    ap.add_argument("--ascii", action="store_true", help="强制 ASCII 界面（也可用环境变量 OC_USAGE_ASCII=1）")
    ap.add_argument("--topmost", action="store_true", help="watch 窗口置顶显示（仅 Windows，其他平台忽略）")
    args = ap.parse_args()

    _setup_console(force_ascii=args.ascii)

    if not DB_PATH.exists():
        print(f"未找到数据库：{DB_PATH}", file=sys.stderr)
        sys.exit(1)

    if args.watch:
        watch_loop(args)
        return

    buckets = load_buckets(args.days, args.by, args.model)
    print(
        f"# opencode 用量报表 | 近 {args.days} 天 | 按 {args.by} 汇总"
        + (f" | 模型过滤: {args.model}" if args.model else "")
    )
    print()
    print_report(buckets, args.by, args.days)


if __name__ == "__main__":
    main()
