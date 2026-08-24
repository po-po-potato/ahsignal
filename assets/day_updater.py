#!/usr/bin/env python3
"""
通达信 .day 文件增量更新工具（不依赖通达信客户端）
==============================

三种数据源（自动选择）:
  1. pytdx 直连通达信服务器（批量最快）
  2. tdx-connector MCP（通过 WorkBuddy，单只可靠）

用法：
  python day_updater.py scan                     # 扫描 QBETF 缺失日期
  python day_updater.py scan --all                # 扫描全部品种
  python day_updater.py fetch                     # 从 pytdx 批量下载 QBETF 缺失数据
  python day_updater.py fetch --all --days 500    # 全量下载最近 500 天
  python day_updater.py fetch-one 510050 sh       # 下载单只
  python day_updater.py read 510050               # 查看最后 5 条
"""

import os, sys, json, struct, glob, time
from datetime import datetime
from collections import Counter

VIPDOC = "C:/zd_zsone/vipdoc"
BLKDIR = "C:/zd_zsone/T0002/blocknew"
DAY_REC = 32
STRUCT_FMT = "<IIIIIfII"

# pytdx 配置
TDX_SERVERS = [
    ("180.153.18.170", 7709),
    ("123.125.108.14", 7709),
    ("180.153.39.51", 7709),
]


# ============================================================
#  价格精度
# ============================================================

def price_factor(code: str) -> int:
    fund_prefixes = tuple(f"{i:02d}" for i in range(50, 60)) + ("15", "16")
    return 1000 if code[:2] in fund_prefixes else 100


def market_code(mkt: str) -> int:
    """市场字符串 -> pytdx market 编号"""
    return {"sh": 1, "sz": 0, "bj": 2}.get(mkt, 1)


# ============================================================
#  .day 文件 I/O
# ============================================================

def read_day(path: str, code: str = None):
    with open(path, "rb") as f:
        data = f.read()
    n = len(data) // DAY_REC
    factor = price_factor(code) if code else 100
    dates, opens, highs, lows, closes, amounts = [], [], [], [], [], []
    for i in range(n):
        offset = i * DAY_REC
        d, o, h, l, c, a, _v, _r = struct.unpack(STRUCT_FMT, data[offset:offset + DAY_REC])
        dates.append(d)
        opens.append(o / factor)
        highs.append(h / factor)
        lows.append(l / factor)
        closes.append(c / factor)
        amounts.append(a)
    return dates, opens, highs, lows, closes, amounts


def day_path(market: str, code: str) -> str | None:
    p = f"{VIPDOC}/{market}/lday/{market}{code}.day"
    if os.path.exists(p):
        return p
    for mk in ("sh", "sz", "bj"):
        pp = f"{VIPDOC}/{mk}/lday/{mk}{code}.day"
        if os.path.exists(pp):
            return pp
    return None


def day_path_ensure(market: str, code: str) -> str:
    d = f"{VIPDOC}/{market}/lday"
    os.makedirs(d, exist_ok=True)
    return f"{d}/{market}{code}.day"


def write_day_record(path: str, code: str, date: int, open_p: float,
                     high: float, low: float, close: float, amount: float):
    factor = price_factor(code)
    rec = struct.pack(STRUCT_FMT,
        date, round(open_p * factor), round(high * factor),
        round(low * factor), round(close * factor),
        float(amount), 0, 0)
    with open(path, "ab") as f:
        f.write(rec)


def get_last_date(path: str, code: str) -> int:
    """获取 .day 文件最后日期"""
    if not os.path.exists(path):
        return 0
    try:
        dates, *_ = read_day(path, code)
        return max(dates) if dates else 0
    except Exception:
        return 0


# ============================================================
#  pytdx 下载
# ============================================================

def _connect_tdx():
    """连接通达信行情服务器"""
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    for host, port in TDX_SERVERS:
        ok = api.connect(host, port, time_out=5)
        if ok:
            try:
                # 设置 socket 超时，防止网络中断导致 get_security_bars 卡死
                api.client.settimeout(10)
            except Exception:
                pass
            return api
    return None


def fetch_tdx_daily(mkt: str, code: str, count: int = 100) -> list[dict]:
    """
    通过 pytdx 获取日线数据。
    返回: [{"date": 20260731, "open": ..., "high": ..., "low": ..., "close": ..., "amount": ...}, ...]
    """
    api = _connect_tdx()
    if not api:
        raise RuntimeError("无法连接通达信行情服务器")

    mc = market_code(mkt)
    try:
        data = api.get_security_bars(9, mc, code, 0, count)
    finally:
        api.disconnect()

    if not data:
        return []

    rows = []
    for bar in data:
        d = bar["year"] * 10000 + bar["month"] * 100 + bar["day"]
        rows.append({
            "date": d,
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "amount": float(bar["amount"]),
        })
    return rows


def fetch_quotes(targets: list[tuple[str, str]]) -> dict:
    """批量拉实时行情快照（盘中最新价）。

    targets: [(market, code), ...]
    返回: {code: {'price': 现价, 'last_close': 昨收, 'amount': 成交额}}
    休市/无数据/连接失败时返回空 dict（容错，不抛异常）。
    """
    api = _connect_tdx()
    if not api:
        return {}
    result = {}
    try:
        # 按市场分组，分批拉取（通达信单次约 80 只上限）
        by_mkt = {}
        for mkt, code in targets:
            by_mkt.setdefault(mkt, []).append(code)
        for mkt, codes in by_mkt.items():
            mc = market_code(mkt)
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                try:
                    quotes = api.get_security_quotes(mc, batch)
                except Exception:
                    continue
                if not quotes:
                    continue
                for q in quotes:
                    code = q.get("code")
                    price = q.get("price")
                    if not code or price is None:
                        continue
                    try:
                        price_f = float(price)
                    except (TypeError, ValueError):
                        continue
                    if price_f <= 0:
                        continue
                    result[code] = {
                        "price": price_f,
                        "last_close": float(q.get("last_close", 0) or 0),
                        "amount": float(q.get("amount", 0) or 0),
                    }
    finally:
        api.disconnect()
    return result


# ============================================================
#  板块解析
# ============================================================

def parse_blk(path: str) -> list[tuple[str, str]]:
    with open(path, "rb") as f:
        data = f.read()
    out = []
    for line in data.split(b"\r\n"):
        line = line.strip()
        if len(line) < 7:
            continue
        mkt = line[0:1]
        code = line[1:7].decode("ascii", "ignore")
        if not code.isdigit():
            continue
        if mkt == b"1":
            prefix = "sh"
        elif mkt == b"0":
            prefix = "sz"
        elif mkt == b"2":
            prefix = "bj"
        elif code[0] in "84":
            prefix = "bj"
        elif code[0] in "03":
            prefix = "sz"
        else:
            prefix = "sh"
        out.append((prefix, code))
    return out


def get_scan_targets(blk_file: str = None, scan_all: bool = False) -> list[tuple[str, str]]:
    """确定扫描目标列表"""
    if blk_file:
        return parse_blk(blk_file)
    if scan_all:
        targets = []
        for mkt in ("sh", "sz", "bj"):
            pdir = f"{VIPDOC}/{mkt}/lday/"
            if os.path.exists(pdir):
                for fn in glob.glob(pdir + "*.day"):
                    code = os.path.basename(fn)[2:8]
                    targets.append((mkt, code))
        return targets
    etf_blk = f"{BLKDIR}/QBETF.blk"
    if os.path.exists(etf_blk):
        return parse_blk(etf_blk)
    return []


# ============================================================
#  CLI: scan
# ============================================================

def cmd_scan(args: list[str]):
    target_date = int(datetime.now().strftime("%Y%m%d"))
    blk_file = None
    scan_all = False
    i = 0
    while i < len(args):
        if args[i] == "--date" and i + 1 < len(args):
            target_date = int(args[i + 1]); i += 2
        elif args[i] == "--blk" and i + 1 < len(args):
            blk_file = args[i + 1]; i += 2
        elif args[i] == "--all":
            scan_all = True; i += 1
        else:
            i += 1

    targets = get_scan_targets(blk_file, scan_all)
    label = os.path.basename(blk_file) if blk_file else ("全部品种" if scan_all else "QBETF")
    print(f"扫描 {label}，目标日期 {target_date}，共 {len(targets)} 只...")

    missing = []
    for market, code in targets:
        path = day_path(market, code)
        if not path:
            missing.append((market, code, "NO_FILE"))
            continue
        try:
            dates, *_ = read_day(path, code)
            if target_date not in dates:
                last = max(dates) if dates else 0
                behind = (target_date - last) if last else "?"
                missing.append((market, code, last, behind))
        except Exception as e:
            missing.append((market, code, f"ERR:{e}"))

    no_file = sum(1 for m in missing if m[2] == "NO_FILE")
    behind = len(missing) - no_file
    print(f"\n结果: 已齐全 {len(targets) - len(missing)}, 缺数据 {behind}, 无文件 {no_file}")

    if behind > 0:
        cnt = Counter(m[3] for m in missing if m[2] != "NO_FILE")
        print(f"落后天数分布:")
        for d, c in sorted(cnt.items(), key=lambda x: x[0] if isinstance(x[0], int) else 0):
            print(f"  落后 {d} 天: {c} 只")

    return missing


# ============================================================
#  CLI: fetch (pytdx)
# ============================================================

def cmd_fetch_one(mkt: str, code: str, days: int = 500):
    """下载单只品种"""
    print(f"下载 {mkt}{code} (最近 {days} 天)...", end=" ", flush=True)
    try:
        rows = fetch_tdx_daily(mkt, code, days)
    except Exception as e:
        print(f"失败: {e}")
        return 0

    if not rows:
        print("无数据")
        return 0

    path = day_path_ensure(mkt, code)
    existing = set()
    if os.path.exists(path):
        try:
            dates, *_ = read_day(path, code)
            existing = set(dates)
        except Exception:
            pass

    written = 0
    for row in sorted(rows, key=lambda r: r["date"]):
        if row["date"] in existing:
            continue
        write_day_record(path, code, row["date"],
                         row["open"], row["high"], row["low"],
                         row["close"], row["amount"])
        written += 1

    last_date = max(r["date"] for r in rows) if rows else 0
    print(f"+{written} 条, 最新 {last_date}")
    return written


def cmd_fetch(args: list[str]):
    """批量下载"""
    blk_file = None
    scan_all = False
    days = 100
    i = 0
    while i < len(args):
        if args[i] == "--blk" and i + 1 < len(args):
            blk_file = args[i + 1]; i += 2
        elif args[i] == "--all":
            scan_all = True; i += 1
        elif args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1]); i += 2
        else:
            i += 1

    targets = get_scan_targets(blk_file, scan_all)
    label = os.path.basename(blk_file) if blk_file else ("全部品种" if scan_all else "QBETF")
    print(f"批量下载 {label}，目标 {len(targets)} 只，每只拉取 {days} 天...")
    print(f"预计耗时: ~{len(targets) * 0.8:.0f} 秒 (~{len(targets) * 0.8 / 60:.1f} 分钟)\n")

    total_written = 0
    t0 = time.time()
    for idx, (mkt, code) in enumerate(targets):
        w = cmd_fetch_one(mkt, code, days)
        total_written += w
        # 每 100 只输出进度
        if (idx + 1) % 100 == 0:
            elapsed = time.time() - t0
            print(f"  [{idx + 1}/{len(targets)}] 已写入 {total_written} 条, 耗时 {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n完成! {len(targets)} 只, 写入 {total_written} 条, 耗时 {elapsed:.0f}s")


# ============================================================
#  CLI: read
# ============================================================

def cmd_read(code: str = None):
    code = code or "510050"
    path = day_path("sh", code) or day_path("sz", code) or day_path("bj", code)
    if not path:
        print(f"未找到 {code} 的 .day 文件")
        return
    dates, opens, highs, lows, closes, amounts = read_day(path, code)
    n = min(5, len(dates))
    print(f"文件: {path} ({len(dates)} 条)")
    for i in range(-n, 0):
        j = len(dates) + i
        print(f"  {dates[j]}  O={opens[j]:.3f} H={highs[j]:.3f} L={lows[j]:.3f} C={closes[j]:.3f} Amt={amounts[j]:.0f}")


# ============================================================
#  main
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "scan":
        cmd_scan(rest)
    elif cmd == "fetch":
        cmd_fetch(rest)
    elif cmd == "fetch-one":
        if len(rest) < 2:
            print("用法: python day_updater.py fetch-one <code> <market> [--days 500]")
            return
        code = rest[0]
        mkt = rest[1]
        days = 500
        if len(rest) >= 4 and rest[2] == "--days":
            days = int(rest[3])
        cmd_fetch_one(mkt, code, days)
    elif cmd == "read":
        cmd_read(rest[0] if rest else None)
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
