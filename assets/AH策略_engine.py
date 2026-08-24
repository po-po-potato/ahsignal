# -*- coding: utf-8 -*-
"""
强势动量策略回测
- 标的去重：从 ETF 名称提取跟踪标的，同标的仅保留成交额最大 1-2 只
- 回测期：最近 3 年 (2023-07-29 ~ 2026-07-29)
- 买入：因子评分 Top-K 买入
- 卖出：动态持有，跌破 MA20 或亏损 8% 即卖出
- 输出：各权重组合的评估指标，确定最优默认权重
"""
import os, re, json, struct, statistics, time, datetime as _dt
from collections import defaultdict, Counter
from oamv_regime import get_oamv_regime, get_regime_params, get_ad_state

# ========== 费率工具 ==========
def natural_days_between(d1, d2):
    """计算两个 yyyymmdd int 之间的自然日天数"""
    import datetime
    a = datetime.date(d1 // 10000, (d1 // 100) % 100, d1 % 100)
    b = datetime.date(d2 // 10000, (d2 // 100) % 100, d2 % 100)
    return (b - a).days

def get_fee_rate(buy_date, sell_date):
    """场外ETF基金赎回费率（自然日）"""
    ndays = natural_days_between(buy_date, sell_date)
    if ndays < 7:
        return 0.015   # 1.5% 惩罚性赎回费
    elif ndays < 30:
        return 0.005   # 0.5%
    else:
        return 0.0

BUY_FEE_RATE = 0.001  # 买入费率 0.1%

# ========== 常量 ==========
VIPDOC = r"C:/zd_zsone/vipdoc"
BLKDIR = r"C:/zd_zsone/T0002/blocknew"
NAMEFILES = [r"C:/zd_zsone/T0002/hq_cache/shs.tnf",
             r"C:/zd_zsone/T0002/hq_cache/szs.tnf",
             r"C:/zd_zsone/T0002/hq_cache/bjs.tnf"]
DAY_REC = 32
BACKTEST_START = 20220101  # 回测起点：2022-01（去掉2021年，因614只ETF中500+只2020年后上市，
                            # 2021上半年仅~144只老宽基可交易、样本严重偏向宽基。2021全年作为热身期）
BACKTEST_END = int(_dt.date.today().strftime('%Y%m%d'))  # 回测终点：自动取今天
NEED_MA = (20, 30, 60, 120, 250)

# ========== 数据加载 ==========

def load_names():
    m = {}
    pat = re.compile(rb'(\d{6})([\x00-\x20]*?)([\x81-\xfe][\x40-\xfe](?:[\x40-\xfe]|[0-9A-Za-z]){1,40})')
    for p in NAMEFILES:
        if not os.path.exists(p):
            continue
        data = open(p, 'rb').read()
        for mm in pat.finditer(data):
            code = mm.group(1).decode()
            name = mm.group(3).decode('gbk', 'ignore').replace('\x00', '').strip()
            if code not in m and name:
                m[code] = name
    ihp = r"C:/zd_zsone/T0002/hq_cache/infoharbor_ex.name"
    if os.path.exists(ihp):
        try:
            for ln in open(ihp, 'rb').read().decode('gbk', 'ignore').splitlines():
                parts = ln.split('|')
                if len(parts) >= 3 and parts[1].isdigit():
                    name = parts[2].strip()
                    if name:
                        m[parts[1]] = name
        except Exception:
            pass
    return m


def price_factor(code):
    fund_prefixes = tuple(f"{i:02d}" for i in range(50, 60)) + ('15', '16')
    return 1000 if code and code[:2] in fund_prefixes else 100


def read_day(path, code=None):
    with open(path, 'rb') as f:
        data = f.read()
    n = len(data) // DAY_REC
    factor = price_factor(code)
    dates, closes, amounts, opens, highs, lows = [], [], [], [], [], []
    for i in range(n):
        o = i * DAY_REC
        date, op, hi, lo, cl, amount, vol, _ = struct.unpack("<IIIIIfII", data[o:o + DAY_REC])
        dates.append(date)
        opens.append(op / factor)
        highs.append(hi / factor)
        lows.append(lo / factor)
        closes.append(cl / factor)
        amounts.append(amount)
    return dates, opens, highs, lows, closes, amounts


# ========== 前复权处理 ==========

def forward_adjust(closes, opens, highs, lows, gap_threshold=0.25):
    """
    从最新日向历史扫描，检测单日价格跳变（除权除息），自动做前复权。
    阈值 25%：ETF 除权事件通常远大于 10%，10-15% 通常是正常的市场波动或节假日跳空。
    """
    n = len(closes)
    if n < 2:
        return closes, opens, highs, lows

    adj_closes = list(closes)
    adj_opens = list(opens)
    adj_highs = list(highs)
    adj_lows = list(lows)
    cum_factor = 1.0

    for i in range(n - 2, -1, -1):
        # 先对当天应用累积调整因子
        adj_closes[i] *= cum_factor
        adj_opens[i] *= cum_factor
        adj_highs[i] *= cum_factor
        adj_lows[i] *= cum_factor

        # 检测当天与后一天之间是否有除权跳变
        if adj_closes[i + 1] > 0:
            gap = abs(adj_closes[i] / adj_closes[i + 1] - 1)
            if gap > gap_threshold:
                div_factor = adj_closes[i + 1] / adj_closes[i]
                cum_factor *= div_factor
                # 用新累积因子重新调整当天
                adj_closes[i] = closes[i] * cum_factor
                adj_opens[i] = opens[i] * cum_factor
                adj_highs[i] = highs[i] * cum_factor
                adj_lows[i] = lows[i] * cum_factor

    return adj_closes, adj_opens, adj_highs, adj_lows


def sma(vals, n):
    L = len(vals)
    out = [None] * L
    if L < n:
        return out
    s = sum(vals[:n])
    out[n - 1] = s / n
    for i in range(n, L):
        s += vals[i] - vals[i - n]
        out[i] = s / n
    return out


def market_of(code):
    if code[0] in '69':
        return 'sh'
    if code[0] in '03':
        return 'sz'
    if code[0] in '84':
        return 'bj'
    return 'sh'


def day_path(market, code):
    for mk in [market, 'sh', 'sz', 'bj']:
        pp = f"{VIPDOC}/{mk}/lday/{mk}{code}.day"
        if os.path.exists(pp):
            return pp
    return None


def parse_blk(path):
    data = open(path, 'rb').read()
    out = []
    for line in data.split(b'\r\n'):
        line = line.strip()
        if len(line) < 7:
            continue
        mkt = line[0:1]
        code = line[1:7].decode('ascii', 'ignore')
        if not code.isdigit():
            continue
        if mkt == b'1':
            prefix = 'sh'
        elif code[0] in '84':
            prefix = 'bj'
        elif code[0] in '03':
            prefix = 'sz'
        else:
            prefix = 'sh'
        out.append((prefix, code))
    return out


# ========== ETF 去重：提取跟踪标的 ==========

# 常见的基金公司后缀（按长度排序，先匹配长的避免误匹配）
FUND_CO_SUFFIXES = [
    '华泰柏瑞', '前海开源', '汇添富', '海富通',
    '易方达', '招商', '华夏', '南方', '广发', '富国',
    '博时', '华安', '国泰', '天弘', '鹏华', '景顺',
    '工银', '嘉实', '中欧', '万家', '银华', '建信',
    '平安', '兴全', '交银', '大成', '中银',
    '摩根', '泰康', '浦银', '民生', '兴业', '申万',
    '华宝', '国联', '西藏', '财通', '信诚',
]

# 已知的跟踪标的 → 统一命名
TRACK_NORMALIZE = {
    '沪深300': '沪深300',
    '中证500': '中证500',
    '中证1000': '中证1000',
    '中证2000': '中证2000',
    '科创50': '科创50',
    '科创100': '科创100',
    '科创创业50': '科创创业50',
    '创业板': '创业板',
    '上证50': '上证50',
    '中证红利': '中证红利',
    '红利低波': '红利低波',
    'A500': 'A500',
    '中证A50': '中证A50',
    '半导体': '半导体',
    '芯片': '芯片',
    '酒': '酒',
}


def extract_track(name):
    """从 ETF 名称提取跟踪标的，如 '沪深300ETF易方达' -> '沪深300'"""
    raw = name.strip()

    # 去 ETF/LOF/基金 标记
    raw = re.sub(r'(ETF|LOF|基金)', '', raw)

    # 去基金公司后缀（按长度从长到短匹配）
    for suffix in sorted(FUND_CO_SUFFIXES, key=len, reverse=True):
        if raw.endswith(suffix):
            raw = raw[:-len(suffix)]
            break

    # 去尾部数字编号
    raw = re.sub(r'[A-H]$', '', raw).strip()

    # 尝试匹配已知标的
    for keyword, normalized in sorted(TRACK_NORMALIZE.items(), key=lambda x: -len(x[0])):
        if keyword in raw:
            return normalized

    # 其他：尝试从名称中提取核心
    # 常见的：xxx指数、xxxETF -> xxx
    core = re.sub(r'(指数|联接|增强)', '', raw).strip()
    if core:
        return core
    return raw if raw else name


def deduplicate_etfs(codes_with_data):
    """
    codes_with_data: [(prefix, code, estimate_amt), ...]
    返回去重后的代码列表（每个跟踪标的保留 1-2 只成交额最大的）
    """
    names = load_names()
    track_groups = defaultdict(list)

    for prefix, code, amt in codes_with_data:
        name = names.get(code, code)
        track = extract_track(name)
        track_groups[track].append((amt, prefix, code, name))

    selected = []
    dup_count = 0
    for track, items in sorted(track_groups.items()):
        items.sort(key=lambda x: x[0], reverse=True)  # 按成交额降序
        take = min(2, len(items))
        for i in range(take):
            selected.append((items[i][1], items[i][2]))
        if len(items) > take:
            dup_count += len(items) - take

    print(f"去重前 {len(codes_with_data)} 只 → 去重后 {len(selected)} 只（{len(track_groups)} 个跟踪标的），去除 {dup_count} 只重复")
    return selected


# ========== 因子计算 ==========

def calc_factors(idx, closes, amounts, mas, b1_score=None, trend_use_ma30=None):
    """计算单日（idx位置）的全部因子原始值
    trend_use_ma30: 传入则优先于模块全局 _TREND_USE_MA30（多线程下避免串扰）"""
    close = closes[idx]
    amt = amounts[idx] if idx < len(amounts) else 0

    # 均线多头趋势度
    tparts = []
    pairs = [(20, 60), (60, 120), (120, 250)]
    use_ma30 = _TREND_USE_MA30 if trend_use_ma30 is None else trend_use_ma30
    if use_ma30:
        pairs.insert(0, (20, 30))
    for a, b in pairs:
        ma_a = mas[a][idx] if a in mas else None
        ma_b = mas[b][idx] if b in mas else None
        if ma_a and ma_b and ma_b > 0:
            tparts.append(ma_a / ma_b - 1)
    trend = sum(tparts) / len(tparts) if tparts else 0.0

    # 站上强度（乖离率平均）
    dparts = []
    for n in NEED_MA:
        ma_v = mas[n][idx] if n in mas else None
        if ma_v and ma_v > 0:
            dparts.append(close / ma_v - 1)
    dist = sum(dparts) / len(dparts) if dparts else 0.0

    # 近60日收益
    mom60 = (close / closes[idx - 60] - 1) if idx >= 60 and closes[idx - 60] > 0 else None

    # 近20日收益
    mom20 = (close / closes[idx - 20] - 1) if idx >= 20 and closes[idx - 20] > 0 else None

    # 流动性（成交额，单位亿）
    liq = amt / 1e8

    # === 波动率原始值（用于 sharpe_eff 分母，不单独打分）===
    if idx >= 20:
        returns_20d = [closes[i] / closes[i - 1] - 1 for i in range(idx - 19, idx + 1)]
        stab_raw = statistics.pstdev(returns_20d) * 100 if len(returns_20d) >= 2 else 0
    else:
        stab_raw = 0

    # === 夏普效率（单位波动产生的收益，替代原来的 stab 反向独立打分）===
    # 慢牛：mom60=5%, stab=1.2% → sharpe=4.17
    # 暴牛：mom60=20%, stab=3.5% → sharpe=5.71 → 暴牛反超
    # 假突破：mom60=3%, stab=5% → sharpe=0.6 → 被压低
    if mom60 is not None and stab_raw > 0.001:
        sharpe_eff = mom60 * 100 / stab_raw
    else:
        sharpe_eff = 0

    # === MA20/MA60 夹角变化（10日价差扩张率，替代 freshness）===
    # 本质是趋势加速度：价差在扩大→趋势加速；价差在收敛→动能衰减
    spread_change = 0.0
    if 20 in mas and 60 in mas and idx >= 10:
        ma20_now, ma60_now = mas[20][idx], mas[60][idx]
        ma20_prev, ma60_prev = mas[20][idx-10], mas[60][idx-10]
        if all(v is not None and v > 0 for v in [ma20_now, ma60_now, ma20_prev, ma60_prev]):
            spread_now = ma20_now / ma60_now - 1
            spread_prev = ma20_prev / ma60_prev - 1
            spread_change = (spread_now - spread_prev) * 100  # 百分点

    # === 回踩确认（替代 freshness，模拟人工"等回踩再买"）===
    # 状态机：上穿→观察→回踩→确认。只有通过回踩验证才给高分
    pullback_confirm = 50.0  # 默认中性
    try:
        _pm30 = _PULLBACK_MA30
    except NameError:
        _pm30 = False
    cross_ma = 30 if _pm30 else 60
    need_ma = cross_ma if cross_ma in mas else 60
    if 20 in mas and need_ma in mas and idx >= need_ma:
        ma20, ma_short, ma_long = mas[20], mas[20] if _pm30 else mas[20], mas[need_ma]
        # 1. 找最近一次 MA20 上穿 MA{need_ma}
        cross_day = None
        for j in range(idx, max(0, idx - 120), -1):
            if (ma20[j] and mas[need_ma][j] and ma20[j-1] and mas[need_ma][j-1]
                    and ma20[j] >= mas[need_ma][j] and ma20[j-1] < mas[need_ma][j-1]):
                cross_day = j
                break
        if cross_day is not None:
            days_since = idx - cross_day
            # 2. 从上穿日以来，找离 MA20 的最小乖离（回踩深度）
            min_gap = float('inf')
            for j in range(cross_day, idx + 1):
                if ma20[j] and ma20[j] > 0:
                    gap = (closes[j] - ma20[j]) / ma20[j] * 100
                    if gap < min_gap:
                        min_gap = gap
            # 3. 分类评分
            current_gap = (closes[idx] - ma20[idx]) / ma20[idx] * 100 if ma20[idx] and ma20[idx] > 0 else 0
            if days_since < 5:
                pullback_confirm = 30  # 刚上穿，观察期
            elif min_gap < -3:
                # 回踩过深，可能是假突破
                pullback_confirm = 15 if current_gap < 0 else 25
            elif min_gap > 3:
                # 没回踩过，悬空上涨，观望
                pullback_confirm = 40
            elif min_gap >= -3 and min_gap <= 3:
                # 回踩了但不深（碰到了 MA20 附近）→ 看是否企稳反弹
                # 找最近一次回踩低点
                low_day = cross_day
                for j in range(cross_day, idx + 1):
                    if ma20[j] and ma20[j] > 0:
                        if (closes[j] - ma20[j]) / ma20[j] * 100 == min_gap:
                            low_day = j
                            break
                recovery_days = idx - low_day
                if recovery_days < 2:
                    pullback_confirm = 50  # 正在回踩中，中性
                elif closes[idx] > closes[low_day]:
                    # 回踩后反弹确认
                    bounce_pct = (closes[idx] / closes[low_day] - 1) * 100
                    penalty = max(0, current_gap - 5) * 2  # 离MA20太远扣分
                    pullback_confirm = 65 + min(bounce_pct * 4, 35) - penalty
                    pullback_confirm = max(50, min(100, pullback_confirm))
                else:
                    pullback_confirm = 45  # 回踩了但没反弹
            else:
                pullback_confirm = 50

    # 大反弹防追: 回踩确认当天涨幅过大→降级到65(等待次日洗盘)
    try:
        if _BOUNCE_CAP_PCT > 0 and pullback_confirm >= 65 and idx >= 1 and closes[idx-1] > 0:
            day_gain = (closes[idx] / closes[idx-1] - 1) * 100
            if day_gain > _BOUNCE_CAP_PCT:
                pullback_confirm = 65
    except NameError:
        pass

    # 超买警示：MA20连续上行天数（趋势走太久→可能是5浪末端，反向扣分）
    overheat = 0.0
    if 20 in mas:
        ma20 = mas[20]
        up = 0
        i = idx
        while i > 0 and ma20[i] is not None and ma20[i - 1] is not None and ma20[i] >= ma20[i - 1]:
            up += 1
            i -= 1
        overheat = min(up, 60) / 60.0 * 100.0  # 0-100，越高越危险

    # === 突破前高因子：取最大突破日数，日数越大分越高 ===
    breakout_score = 0
    if idx >= 60:
        close_now = closes[idx]
        # 从大到小检测：历史新高 → 250日 → 120日 → 60日
        all_time_high = max(closes[:idx])
        if close_now >= all_time_high * 0.995:
            breakout_score = 100  # 历史新高附近，最高分
        elif idx >= 250 and close_now > max(closes[max(0, idx-250):idx]):
            breakout_score = 75   # 突破250日前高
        elif idx >= 120 and close_now > max(closes[max(0, idx-120):idx]):
            breakout_score = 50   # 突破120日前高
        elif close_now > max(closes[max(0, idx-60):idx]):
            breakout_score = 25   # 突破60日前高
        # else: 0 = 未突破任何前高

    return {
        'trend': trend, 'dist': dist, 'mom60': mom60, 'mom20': mom20,
        'liq': liq,
        'spread_change': spread_change,
        'sharpe_eff': sharpe_eff,
        'pullback_confirm': pullback_confirm,
        'overheat': overheat,
        'breakout_score': breakout_score,  # new: 突破前高
        'b1_factor': b1_score if b1_score is not None else 50.0,  # B1因子
        'close': close,
        'ma20': mas[20][idx] if 20 in mas and mas[20][idx] else None,
        'ma60': mas[60][idx] if 60 in mas and mas[60][idx] else None,
    }


def pct_rank(values, inverse=False, lo=0, hi=100):
    """计算列表的百分位排名，映射到 lo~hi 区间"""
    n = len(values)
    if n == 0:
        return [50.0] * n
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    mid = (lo + hi) / 2
    out = [mid] * n
    if not valid:
        return out
    valid.sort(key=lambda x: x[1])
    nv = len(valid)
    for rank, (i, _) in enumerate(valid):
        pp = rank / (nv - 1) if nv > 1 else 0.5
        pp = 1.0 - pp if inverse else pp
        out[i] = lo + pp * (hi - lo)
    return out


def std_score(values, inverse=False, base=60.0, scale=12.0, cap_min=30.0, cap_max=90.0):
    """标准化评分：z-score映射到基础分±N，避免0-100极端跨度。默认60±12，限制30-90。"""
    n = len(values)
    out = [base] * n
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if not valid:
        return out
    vals = [v for _, v in valid]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = var ** 0.5
    if std < 1e-9:
        return out
    for i, v in valid:
        raw_z = (v - mean) / std
        z = -raw_z if inverse else raw_z
        score = base + z * scale
        score = max(cap_min, min(cap_max, score))
        out[i] = score
    return out


def winsorize(values, lower_pct=2, upper_pct=98):
    """MAD法截尾：用中位数±n*MAD限制极端值，比百分位法更稳健"""
    n = len(values)
    if n < 5:
        return list(values)
    valid = [v for v in values if v is not None]
    if len(valid) < 5:
        return list(values)
    sorted_v = sorted(valid)
    median = sorted_v[len(sorted_v) // 2]
    mad = sorted(abs(v - median) for v in sorted_v)[len(sorted_v) // 2]
    if mad < 1e-12:
        return list(values)
    # 用百分位确定截尾倍数（2%=~2.5*MAD, 5%=~1.5*MAD）
    lo = sorted_v[max(0, int(n * lower_pct / 100))]
    hi = sorted_v[min(n - 1, int(n * upper_pct / 100))]
    result = []
    for v in values:
        if v is None:
            result.append(None)
        else:
            result.append(max(lo, min(hi, v)))
    return result


def winsorized_zscore(values, base=60.0, scale=12.0, cap_min=30.0, cap_max=90.0):
    """业界标准管线：MAD截尾 → Z-score → 60±12映射"""
    n = len(values)
    out = [base] * n
    winsor = winsorize(values)
    valid = [(i, v) for i, v in enumerate(winsor) if v is not None]
    if not valid:
        return out
    vals = [v for _, v in valid]
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = var ** 0.5
    if std < 1e-9:
        return out
    for i, v in valid:
        z = (v - mean) / std
        out[i] = max(cap_min, min(cap_max, base + z * scale))
    return out


def calc_adx(highs, lows, closes, period=14):
    """计算ADX（不依赖方向，仅判断趋势强度）。返回整个序列的ADX值列表"""
    n = len(closes)
    adx = [None] * n
    tr = [None] * n
    plus_dm = [None] * n
    minus_dm = [None] * n

    for i in range(1, n):
        h, l, c = highs[i], lows[i], closes[i]
        ph, pl, pc = highs[i-1], lows[i-1], closes[i-1]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        down = pl - l
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0

    # Wilder's smoothing
    atr_s = [None] * n
    pdi_s = [None] * n
    mdi_s = [None] * n
    if n > period:
        atr_s[period] = sum(tr[1:period+1])
        pdi_s[period] = sum(plus_dm[1:period+1])
        mdi_s[period] = sum(minus_dm[1:period+1])
        for i in range(period + 1, n):
            atr_s[i] = atr_s[i-1] - atr_s[i-1]/period + (tr[i] or 0)
            pdi_s[i] = pdi_s[i-1] - pdi_s[i-1]/period + (plus_dm[i] or 0)
            mdi_s[i] = mdi_s[i-1] - mdi_s[i-1]/period + (minus_dm[i] or 0)

    for i in range(n):
        if i <= period or atr_s[i] is None or atr_s[i] == 0:
            continue
        pdi = pdi_s[i] / atr_s[i] * 100
        mdi = mdi_s[i] / atr_s[i] * 100
        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
        adx[i] = dx

    # ADX也需要平滑一次
    adx_smooth = [None] * n
    if n > period * 2:
        adx_smooth[period * 2] = sum(v for v in adx[period+1:period*2+1] if v is not None)
        for i in range(period * 2 + 1, n):
            prev = adx_smooth[i-1]
            cur = adx[i] if adx[i] is not None else 0
            adx_smooth[i] = prev - prev/period + cur

    return adx_smooth


def calc_trend_efficiency(closes, period=20):
    """趋势效率比：净涨跌 / 总路径长度。接近1=干净趋势，接近0=锯齿"""
    n = len(closes)
    eff = [None] * n
    if n <= period:
        return eff
    for i in range(period, n):
        net = abs(closes[i] - closes[i-period])
        path = sum(abs(closes[j] - closes[j-1]) for j in range(i-period+1, i+1))
        eff[i] = net / path if path > 0 else 0
    return eff


def bucket_for(val, boundaries):
    """根据连续值找到对应桶编号（基于训练好的区间边界）"""
    for i in range(len(boundaries)-1):
        lo, hi = boundaries[i], boundaries[i+1]
        if i == len(boundaries)-2:
            if val >= lo: return i
        elif lo <= val < hi:
            return i
    return len(boundaries)-2

def score_etfs(factor_data, weights, method='winsor_z',
               expected_return_table=None, absolute_score_map=None,
               pr_lo=0, pr_hi=100, score_mult_max=1.4):
    """因子评分合成
    score_mult_max: 乘法合成乘数上限，score=100时 m = 0.6+(score_mult_max-0.6)=score_mult_max"""
    n = len(factor_data)

    if method == 'absolute' and absolute_score_map:
        scores = []
        for f in factor_data:
            total = 0
            for fn, info in absolute_score_map.items():
                boundaries = info['boundaries']
                rets = info['avg_rets']
                min_r = info['min_ret']
                max_r = info['max_ret']
                if fn == 'ma120_250':
                    val = f.get('ma120_250_bkt', 0)
                    bi = int(val) if val in (0, 1) else 0
                elif fn == 'eff':
                    val = f.get('eff', 0) or 0
                    bi = bucket_for(val, boundaries)
                elif fn == 'stab':
                    val = f.get('stab', 0) or 0
                    bi = bucket_for(val, boundaries)
                elif fn == 'mom20':
                    val = f.get('mom20', 0) or 0
                    bi = bucket_for(val, boundaries)
                elif fn == 'above':
                    val = f.get('above_days', 0) or 0
                    bi = bucket_for(val, boundaries)
                elif fn == 'r20_60':
                    val = f.get('r20_60', 1.0) or 1.0
                    bi = bucket_for(val, boundaries)
                elif fn == 'gap':
                    val = f.get('gap', 0) or 0
                    bi = bucket_for(val, boundaries)
                else:
                    continue
                avg_r = rets.get(str(bi), 0)
                span = max_r - min_r
                if abs(span) > 0.001:
                    score = max(0, min(100, (avg_r - min_r) / span * 100))
                else:
                    score = 50
                total += score
            scores.append(round(total, 1))
        return scores

    FACTOR_KEYS = ('trend', 'mom60', 'mom20', 'liq', 'dist',
                   'spread_change', 'sharpe_eff', 'pullback_confirm', 'overheat',
                   'breakout_score', 'b1_factor')

    if method == 'pct_rank':
        factor_ranks = {}
        factor_ranks['trend']     = pct_rank([f['trend'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['dist']      = pct_rank([f['dist'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['mom60']     = pct_rank([f['mom60'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['mom20']     = pct_rank([f['mom20'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['liq']       = pct_rank([f['liq'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['spread_change']  = pct_rank([f['spread_change'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['sharpe_eff']     = pct_rank([f['sharpe_eff'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['pullback_confirm'] = pct_rank([f['pullback_confirm'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['overheat']  = pct_rank([f['overheat'] for f in factor_data], inverse=True, lo=pr_lo, hi=pr_hi)
        factor_ranks['breakout_score'] = pct_rank([f['breakout_score'] for f in factor_data], lo=pr_lo, hi=pr_hi)
        factor_ranks['b1_factor']    = pct_rank([f['b1_factor'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    else:
        factor_ranks = {}
        factor_ranks['trend']     = winsorized_zscore([f['trend'] for f in factor_data])
        factor_ranks['dist']      = winsorized_zscore([f['dist'] for f in factor_data])
        factor_ranks['mom60']     = winsorized_zscore([f['mom60'] for f in factor_data])
        factor_ranks['mom20']     = winsorized_zscore([f['mom20'] for f in factor_data])
        factor_ranks['liq']       = winsorized_zscore([f['liq'] for f in factor_data])
        factor_ranks['spread_change']  = winsorized_zscore([f['spread_change'] for f in factor_data])
        factor_ranks['sharpe_eff']     = winsorized_zscore([f['sharpe_eff'] for f in factor_data])
        factor_ranks['pullback_confirm'] = winsorized_zscore([f['pullback_confirm'] for f in factor_data])
        over_vals = [f['overheat'] for f in factor_data]
        factor_ranks['overheat']  = winsorized_zscore([-v if v is not None else None for v in over_vals])
        factor_ranks['breakout_score'] = winsorized_zscore([f['breakout_score'] for f in factor_data])
        factor_ranks['b1_factor']    = winsorized_zscore([f['b1_factor'] for f in factor_data])

    w = weights
    wsum = sum(w.get(k, 0) for k in FACTOR_KEYS) or 1.0

    # === 乘法合成：Π(0.6 + (max-0.6) * score/100)^(w_k / wsum) ===
    # score=0→0.6, score=100→score_mult_max
    composites = []
    for i in range(n):
        composite = 1.0
        for fn in FACTOR_KEYS:
            score_0_100 = factor_ranks[fn][i]
            w_norm = w.get(fn, 0) / wsum
            if w_norm < 0.001:
                continue
            m = 0.6 + (score_mult_max - 0.6) * score_0_100 / 100.0
            composite *= m ** w_norm
        composites.append(composite)

    # 将乘法合成结果映射到 0-100（用固定比例，保留分散度）
    # composite=0.7→20分, 1.0→50分, 1.5→100分
    # 不做 pct_rank 是为了保留乘法合成天然产生的分数分散度
    scores = [max(0, min(100, round((c - 1.0) * 100 + 50, 1))) for c in composites]
    if method == 'expected_return' and expected_return_table:
        scores = []
        for f in factor_data:
            s = 0
            for bk in ['ma120_250_bkt', 'eff_bkt', 'stab_bkt', 'mom_bkt', 'above_bkt', 'r20_60_bkt']:
                bv = f.get(bk, 0)
                s += expected_return_table.get(bk, {}).get(bv, 0)
            scores.append(round(s, 2))
        return scores

    return scores


# ========== 回测引擎 ==========

def simulate_trade(etf_data, buy_idx, entry_price):
    """
    模拟从 buy_idx 买入后的持有过程。
    etf_data: {dates, closes, mas}
    返回: (exit_idx, exit_price, exit_reason, max_profit_pct, hold_days)
    """
    closes = etf_data['closes']
    mas = etf_data['mas']
    dates = etf_data['dates']

    max_price = entry_price
    for i in range(buy_idx + 1, len(closes)):
        price = closes[i]
        if price > max_price:
            max_price = price

        ma20 = mas[20][i] if 20 in mas and i < len(mas[20]) else None

        # 卖出信号
        loss_pct = (price / entry_price - 1) * 100
        below_ma20 = ma20 is not None and price < ma20

        if loss_pct <= -8:
            return i, price, '亏损-8%', (max_price / entry_price - 1) * 100, i - buy_idx
        if below_ma20 and loss_pct <= -3:
            # 跌破 MA20 且已有一定亏损，卖出
            return i, price, '跌破MA20', (max_price / entry_price - 1) * 100, i - buy_idx
        if below_ma20 and loss_pct > 0:
            # 跌破 MA20 但还有盈利，给 3 天观察期
            hold_below = 1
            for j in range(i + 1, min(i + 4, len(closes))):
                ma20_j = mas[20][j] if 20 in mas and j < len(mas[20]) else None
                if ma20_j is not None and closes[j] < ma20_j:
                    hold_below += 1
                else:
                    break
                if closes[j] / entry_price - 1 < -0.02:
                    return j, closes[j], '跌破MA20(持续)', (max_price / entry_price - 1) * 100, j - buy_idx
            if hold_below >= 3:
                return i, price, '跌破MA20(3日不收回)', (max_price / entry_price - 1) * 100, i - buy_idx
            else:
                # 快速收回，继续持有
                continue

        if below_ma20:
            # 跌破 MA20 但盈利 > 0，先标记不立即卖
            pass

    # 持有到期末
    last_idx = len(closes) - 1
    return last_idx, closes[last_idx], '持有至期末', (max_price / entry_price - 1) * 100, last_idx - buy_idx


# ========== IC 分析 ==========

def calc_ic(etf_data_list, eval_dates, weights, gate=True, scoring_method='winsor_z'):
    """
    信息系数分析：横截面因子得分与未来收益的相关性。
    eval_dates: [(date_int, idx_in_etf_data), ...] 需要每个 ETF 在同一交易日有数据
    """
    all_ic = defaultdict(list)

    for date_int, idx_map in eval_dates:
        # 收集该截面所有 ETF 的因子值
        factor_data = []
        valid_etfs = []
        for ei, etf in enumerate(etf_data_list):
            idx = idx_map.get(ei)
            if idx is None or idx < 60:
                continue
            etf_idx = idx  # 在 etf 时间序列中的位置

            # Gate: 右侧硬门槛（与 evaluate_weights 保持一致）
            closes = etf['closes']
            mas = etf['mas']
            if gate:
                if (mas[20][etf_idx] is None or mas[60][etf_idx] is None
                        or closes[etf_idx] <= mas[60][etf_idx]
                        or mas[20][etf_idx] < mas[60][etf_idx]):
                    continue
                # MA20 必须上行
                if mas[20][etf_idx] < mas[20][etf_idx - 1]:
                    continue
                # 连续站上MA20 ≥ 11天
                above_days = 0; di = etf_idx
                while di >= 0 and mas[20][di] is not None and closes[di] >= mas[20][di]:
                    above_days += 1; di -= 1
                if above_days < 11:
                    continue

            f = calc_factors(etf_idx, closes, etf['amounts'], mas)
            factor_data.append(f)
            valid_etfs.append(ei)

        if len(factor_data) < 10:
            continue

        # 打分
        scores = score_etfs(factor_data, weights, method=scoring_method)
        future_rets = []
        for fi, ei in enumerate(valid_etfs):
            etf = etf_data_list[ei]
            etf_idx = idx_map[ei]
            exit_idx, exit_price, reason, max_profit, days = simulate_trade(etf, etf_idx, factor_data[fi]['close'])
            ret = (exit_price / factor_data[fi]['close'] - 1) * 100
            future_rets.append(ret)

        # 计算 rank IC (Spearman)
        if len(scores) >= 5:
            # Spearman rank correlation
            n = len(scores)
            rank_scores = sorted(range(n), key=lambda i: scores[i])
            rank_rets = sorted(range(n), key=lambda i: future_rets[i])
            d2 = sum((rank_scores[i] - rank_rets[i]) ** 2 for i in range(n))
            rho = 1 - 6 * d2 / (n * (n * n - 1))

            # 各因子单独的 IC
            factor_names = ['trend', 'dist', 'mom60', 'mom20', 'liq', 'spread_change', 'sharpe_eff', 'pullback_confirm']
            for fn in factor_names:
                vals = [f[fn] for f in factor_data if f[fn] is not None]
                rets = [future_rets[i] for i, f in enumerate(factor_data) if f[fn] is not None]
                if len(vals) >= 5:
                    idx_s = sorted(range(len(vals)), key=lambda i: vals[i])
                    idx_r = sorted(range(len(vals)), key=lambda i: rets[i])
                    d2_fn = sum((idx_s[i] - idx_r[i]) ** 2 for i in range(len(vals)))
                    ic_fn = 1 - 6 * d2_fn / (len(vals) * (len(vals) * len(vals) - 1))
                    all_ic[fn].append(ic_fn)

            all_ic['综合'].append(rho)

    result = {}
    for k, v in all_ic.items():
        if v:
            result[k] = {
                'mean': round(sum(v) / len(v), 4),
                'std': round(statistics.pstdev(v) if len(v) > 1 else 0, 4),
                'positive_pct': round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                'samples': len(v),
            }
    return result


def classify_track(factor_dict):
    """
    买入时判断持仓风格: 'slow'(慢牛/轨A) 或 'fast'(快牛/轨B)。
    慢牛: 高回踩确认 + 低超买过热 → 趋势缓但健康
    快牛: 强价差扩张 + 高效率比 → 动量爆发
    """
    pb = factor_dict.get('pullback_confirm', 50) or 50
    oh = factor_dict.get('overheat', 50) or 50
    spread = factor_dict.get('spread_change', 0) or 0
    
    if pb >= 65 and oh < 60:
        return 'slow'
    if spread >= 1.0:
        return 'fast'
    return 'slow' if pb >= 60 else 'fast'


def calc_holding_score(etf, buy_idx, current_idx, entry_price, current_buy_score=60):
    """
    持有层独立评分（0-100，越低越该换）。
    因子：
      ① 趋势衰减：MA20斜率从买入时到现在衰减了多少
      ② 时间压力：持有越久，机会成本越大（v3:10天开始）
      ③ 浮盈缓冲：浮盈厚→多给空间；浮亏中→快做决断
      ④ 效率衰减：趋势效率比是否在恶化
      ⑤ spread_change 收敛预警：MA20/MA60 价差在缩水→趋势动能衰减
      ⑥ 峰值回撤（v3新增）：从买入后最高浮盈回撤幅度→趋势反转信号
    """
    closes = etf['closes']
    mas = etf['mas']
    trend_eff = etf.get('trend_eff', [])

    # ① 趋势衰减
    buy_ma20 = mas[20][buy_idx] if buy_idx < len(mas[20]) else None
    cur_ma20 = mas[20][current_idx] if current_idx < len(mas[20]) else None
    slope_decay = 0
    if buy_ma20 and cur_ma20 and buy_idx > 0 and current_idx > 0:
        buy_slope = (mas[20][buy_idx] / mas[20][buy_idx - 1] - 1) if mas[20][buy_idx - 1] else 0
        cur_slope = (mas[20][current_idx] / mas[20][current_idx - 1] - 1) if mas[20][current_idx - 1] else 0
        if buy_slope > 0:
            slope_decay = max(0, (buy_slope - cur_slope) / buy_slope) * 100
        elif cur_slope <= 0:
            slope_decay = 80

    # ② 时间压力（v3: 从10天开始，适配平均15天持有）
    hold_days = current_idx - buy_idx
    time_pressure = max(0, min(100, (hold_days - 10) * 100 / 40))

    # ③ 浮盈缓冲（v3: 增强10-20%区间 + 峰值回撤惩罚）
    profit_pct = (closes[current_idx] / entry_price - 1) * 100
    if profit_pct <= -8:
        profit_bonus = 0
    elif profit_pct <= 0:
        profit_bonus = (profit_pct + 8) / 8 * 40
    elif profit_pct <= 10:
        profit_bonus = 40 + profit_pct / 10 * 45
    elif profit_pct <= 20:
        profit_bonus = 85 + (profit_pct - 10) / 10 * 15
    else:
        profit_bonus = min(110, 100 + (profit_pct - 20) / 30 * 10)

    # ③½ 峰值回撤（v3新增）：从买入以来最高浮盈回撤了多少
    peak_ret = 0
    for j in range(buy_idx, current_idx + 1):
        r = (closes[j] / entry_price - 1) * 100
        if r > peak_ret:
            peak_ret = r
    mtm_drawdown = max(0, peak_ret - profit_pct)  # 从峰值回撤百分比

    # ④ 效率衰减
    buy_eff = trend_eff[buy_idx] if trend_eff and buy_idx < len(trend_eff) and trend_eff[buy_idx] else 0
    cur_eff = trend_eff[current_idx] if trend_eff and current_idx < len(trend_eff) and trend_eff[current_idx] else 0
    eff_decay = max(0, (buy_eff - cur_eff) / max(buy_eff, 0.01)) * 50 if buy_eff > 0 else 0

    # ④½ 量价修正（v4新增）：利用成交额判断趋势健康度
    # ETF特性: 缩量+跌=趋势冷却, 放量+涨=资金活跃
    amounts = etf.get('amounts', [])
    amt_ma20 = etf.get('_amount_ma20', [])
    vol_ratio = 1.0
    vol_signal = 0
    if amt_ma20 and buy_idx < len(amt_ma20) and current_idx < len(amt_ma20):
        buy_vol = amt_ma20[buy_idx]
        cur_vol = amt_ma20[current_idx]
        if buy_vol and buy_vol > 0 and cur_vol and cur_vol > 0:
            vol_ratio = cur_vol / buy_vol
    recent_ret_5d = 0
    if current_idx >= 5 and closes[current_idx-5] and closes[current_idx-5] > 0:
        recent_ret_5d = (closes[current_idx] / closes[current_idx-5] - 1) * 100
    if vol_ratio < 0.7 and recent_ret_5d < 0:
        # 缩量+跌 = 无人接盘，趋势冷却
        vol_signal = (0.7 - vol_ratio) * 60
    elif vol_ratio < 0.8 and recent_ret_5d < 2:
        # 缩量+不涨 = 慢慢降温
        vol_signal = (0.8 - vol_ratio) * 30
    # 放量滞涨: 高位+暴量+不涨 = 警惕
    if vol_ratio > 1.3 and abs(recent_ret_5d) < 1 and profit_pct > 10:
        vol_signal = (vol_ratio - 1.3) * 30
    # 放量暴跌: 恐慌抛售
    if vol_ratio > 1.5 and recent_ret_5d < -3:
        vol_signal = (vol_ratio - 1.5) * 40 + 5

    # ⑤ spread_change 收敛预警（新增）
    # 计算持有期间的价差变化：如果在收敛 → 趋势动能衰减 → 扣分
    spread_decay = 0
    if 20 in mas and 60 in mas and current_idx >= 10:
        ma20_now, ma60_now = mas[20][current_idx], mas[60][current_idx]
        prev_idx = max(buy_idx, current_idx - 10)
        ma20_prev, ma60_prev = mas[20][prev_idx], mas[60][prev_idx]
        if all(v is not None and v > 0 for v in [ma20_now, ma60_now, ma20_prev, ma60_prev]):
            spread_now = ma20_now / ma60_now - 1
            spread_prev = ma20_prev / ma60_prev - 1
            spread_change_10d = (spread_now - spread_prev) * 100
            # 收敛 > 1pp/10天 → 扣 40 分；温和收敛 0-1pp → 扣 10~40；扩张 → 不扣
            if spread_change_10d < -1.0:
                spread_decay = 40 + min(0, spread_change_10d + 1.0) * 10  # max 60
            elif spread_change_10d < 0:
                spread_decay = abs(spread_change_10d) * 20  # 0-20
            # spread_change_10d >= 0 → spread_decay = 0 (趋势健康)

    # ⑥ 过热惩罚：MA20连涨太久 → 趋势末端风险
    overheat_penalty = 0
    if 20 in mas:
        ma20_vals = mas[20]
        up = 0; i = current_idx
        while i > 0 and ma20_vals[i] is not None and ma20_vals[i-1] is not None and ma20_vals[i] >= ma20_vals[i-1]:
            up += 1; i -= 1
        oh = min(up, 60) / 60.0 * 100.0
        # 过热>30天开始扣分，>50天加速扣分
        if oh > 30:
            overheat_penalty = min(30, (oh - 30) * 0.8)

    # 综合评分（v4: 加量价修正）
    holding_score = (60 
                     - slope_decay * 0.20 
                     - time_pressure * 0.30 
                     + (profit_bonus - 50) * 0.35 
                     - eff_decay * 0.18
                     - spread_decay * 0.25
                     - mtm_drawdown * 1.2
                     - vol_signal)
    holding_score = max(10, min(90, holding_score))

    return holding_score, {
        'slope_decay': round(slope_decay, 0),
        'time_pressure': round(time_pressure, 0),
        'profit_bonus': round(profit_bonus, 0),
        'eff_decay': round(eff_decay, 0),
        'spread_decay': round(spread_decay, 0),
        'overheat_penalty': round(overheat_penalty, 0),
        'mtm_dd': round(mtm_drawdown, 1),
        'vol_ratio': round(vol_ratio, 2),
        'vol_signal': round(vol_signal, 1),
    }


# ========== 权重评估 ==========

def evaluate_weights(etf_data_list, weights, gate=True, max_hold=10, daily_buy_max=3,
                     max_gap=1.08, sell_confirm_days=2, sell_gap_pct=5, above_ma20_min=7,
                     min_hold_days=0, buy_cooldown=0, adx_min=0, eff_min=0.2,
                     m20ratio=1.02, vol_ratio_lo=0.6, vol_ratio_hi=2.5,
                     rally_lo=0, rally_hi=999,
                     scoring_method='winsor_z', use_holding_layer=False,
                     expected_return_table=None, absolute_score_map=None,
                     buy_min_score=0, snapshot_callback=None,
                     ma20_tolerance_pct=0, breakeven_trigger_pct=0,
                     max_cumul_gain_pct=0, use_oamv_gate=False, use_ad_state=False,
                     pr_lo=0, pr_hi=100, score_mult_max=1.4,
                     vol_break_high=1.5, vol_break_low=0.7,
                     vol_high_confirm_adj=2, vol_low_confirm_adj=2,
                     apply_fee=True, stop_loss_pct=-10,
                     trend_use_ma30=False, pullback_ma30=False,
                     gate_ma_n=0, crossover_gap=0, bounce_cap_pct=0,
                     crash_day_pct=0, crash_liquidate_type='',
                     bull_buy_mult=1.0,
                     bull_score_threshold=65, bull_min_candidates=5,
                     ad_defense_dn=-2.3, ad_ma_consec=0, ad_ma_period=10,
                     ad_attack_up=4.0, score_relax_threshold=0,
                     dual_track=False, track_b_weights=None,
                     bull_top_avg=0,
                     hitech_above_min=0, hitech_gap=0,
                     b1_gate=False, b1_gate_threshold=80,
                     b1_fast_exit=3,
                     b1_bonus=0, b1_bonus_threshold=78):
    """bull_top_avg: Top10候选平均分门槛(0=不检查), 用于牛市加仓的质量过滤"""
    """apply_fee: True=按场外ETF基金真实费率计算费后收益和夏普
    stop_loss_pct: 硬止损线(负值), 如 -8 表示亏损8%立即止损
    trend_use_ma30: trend因子加入MA20/MA30组
    pullback_ma30: 回踩确认改为MA20上穿MA30→回踩MA20
    gate_ma_n: Gate增加 收盘>MA[N] 长线趋势, 0=关闭, 120=半年线, 250=年线
    bounce_cap_pct: pullback反弹>此%时pullback分封顶65(防追大阳线), 0=关闭
    crash_day_pct: 市场单日跌幅>=此值→触发崩盘规则(如-2.3), 0=关闭
    crash_liquidate_type: 'consecutive'=连续2日触及清仓, 'cumulative'=连续2日累计跌>=2倍crash_day_pct清仓
    bull_buy_mult: 牛市加仓乘数,>1时 若高评分候选≥bull_min_candidates, daily_buy_max翻倍
    bull_score_threshold: 高评分分数线
    bull_min_candidates: 触发加仓的最少候选数"""
    global _TREND_USE_MA30, _PULLBACK_MA30, _BOUNCE_CAP_PCT
    _TREND_USE_MA30 = trend_use_ma30
    _PULLBACK_MA30 = pullback_ma30
    _BOUNCE_CAP_PCT = bounce_cap_pct
    from collections import Counter

    # 高波动板块识别（半导体/芯片/科创 —— 天然高beta，趋势恢复快）
    _HITECH_KW = ('半导体', '芯片', '科创')
    def _is_hitech(track):
        return bool(track) and any(k in track for k in _HITECH_KW)

    cooldown_days = 10
    BASE_JUMP = 75
    BASE_JUMP_HS_MAX = 55  # 持有分超过此值不会被插队替换
    BASE_MAX_HOLD = max_hold
    jump_attempts = 0
    jump_success = 0

    # 0AMV 资金面状态
    _, regime_map = get_oamv_regime()
    regime_stats = Counter()
    USE_OAMV_GATE = use_oamv_gate
    USE_AD_STATE = use_ad_state

    # 进攻/防守状态机
    prev_ad = None
    ad_force_sells = 0
    if USE_AD_STATE:
        ad_state = get_ad_state(up_days=2, defense_dn=ad_defense_dn,
                                oamv_up=ad_attack_up,
                                ma_consec_days=ad_ma_consec, ma_period=ad_ma_period)
    else:
        ad_state = {}

    trades = []
    daily_log = []
    # positions: ei -> {'buy_date','orig_buy_idx','check_from','entry_price','max_price','score','below_ma20_days'}
    positions = {}
    cooldown_map = {}  # ei -> 冷却结束的trading_days索引（在此之前不能重新买入）

    # 1) 为每个 ETF 构建 date -> idx 映射
    date_to_idx = []
    for etf in etf_data_list:
        d2i = {}
        for i, d in enumerate(etf['dates']):
            d2i[d] = i
        date_to_idx.append(d2i)

    # 预计算成交额 20 日均线（量能突破判断用）
    for etf in etf_data_list:
        etf['_amount_ma20'] = sma(etf['amounts'], 20)

    # 2) 收集所有回测期内的交易日（至少 100 只 ETF 有数据）
    date_counts = Counter()
    for etf in etf_data_list:
        for d in etf['dates']:
            if BACKTEST_START <= d <= BACKTEST_END:
                date_counts[d] += 1
    trading_days = sorted(d for d, c in date_counts.items()
                          if c >= 100 and BACKTEST_START <= d <= BACKTEST_END)

    # 买入冷却期：用trading_days下标，而非日历日期差
    last_buy_di = -99999  # 上次买入的 trading_days 下标

    # 崩盘检测：计算ETF市场中位数日收益
    _market_ret = {}
    if crash_day_pct < 0:
        _all_rets = {}
        for etf in etf_data_list:
            for i in range(1, len(etf['dates'])):
                if etf['closes'][i-1] > 0:
                    d = etf['dates'][i]
                    _all_rets.setdefault(d, []).append((etf['closes'][i]/etf['closes'][i-1]-1)*100)
        for d in _all_rets:
            if len(_all_rets[d]) >= 10:
                _market_ret[d] = statistics.median(_all_rets[d])
    _prev_mr = None  # 上日市场收益

    # 3) 每日循环
    for di, date_int in enumerate(trading_days):
        # === 资金面状态：根据 0AMV 动态调整参数 ===
        regime = regime_map.get(date_int, 'transitional')
        rp = get_regime_params(regime)
        effective_max_hold = max_hold
        effective_jump = rp['jump_threshold']
        effective_buy_max = daily_buy_max
        regime_stats[regime] += 1

        # === 进攻/防守状态机 ===
        prev_ad = None
        if USE_AD_STATE:
            cur_ad = ad_state.get(date_int, 'attack')
            # 防守状态：只禁买，不清仓（让已有仓位自然运行）
            if prev_ad is not None and prev_ad != cur_ad and cur_ad == 'defense':
                pass  # 不清仓
            prev_ad = cur_ad

        # 构建当天的 idx_map
        idx_map = {}
        for ei in range(len(etf_data_list)):
            idx = date_to_idx[ei].get(date_int)
            if idx is not None and idx >= 60:
                idx_map[ei] = idx

        if len(idx_map) < 20:
            continue

        # --- 卖出检查：持仓期间逐日 ---
        to_close = []
        for ei, pos in list(positions.items()):
            etf = etf_data_list[ei]
            idx = idx_map.get(ei)
            if idx is None:
                continue

            buy_idx = pos['orig_buy_idx']
            entry_price = pos['entry_price']
            max_p = pos['max_price']

            # 逐日检查：从 check_from 到 idx
            for di_check in range(pos['check_from'], idx + 1):
                close = etf['closes'][di_check]
                high = etf['highs'][di_check]
                if high > max_p:
                    max_p = high
                pos['max_price'] = max_p

                ma20 = etf['mas'][20][di_check] if di_check < len(etf['mas'][20]) else None
                loss_pct = (close / entry_price - 1) * 100
                # 追踪最大浮盈（用于保本止损判断）
                if loss_pct > pos.get('max_profit_pct', 0):
                    pos['max_profit_pct'] = loss_pct
                # MA20容忍度: close必须低于MA20 * (1-tolerance%) 才计为"跌破"
                # 例如 tolerance=2% → close < MA20*0.98 才算跌破
                below_ma20 = ma20 is not None and close < ma20 * (1 - ma20_tolerance_pct / 100)
                below_ma20_deep = ma20 is not None and close < ma20 * (1 - sell_gap_pct / 100)

                # 防抖：记录连续低于MA20的天数
                if below_ma20:
                    pos['below_ma20_days'] = pos.get('below_ma20_days', 0) + 1
                    # 量能感知：跌破 MA20 的第一天检查量比
                    if pos['below_ma20_days'] == 1:
                        amount = etf['amounts'][di_check] if di_check < len(etf['amounts']) else 0
                        avg_vol = (etf['_amount_ma20'][di_check] if di_check < len(etf['_amount_ma20']) and etf['_amount_ma20'][di_check] else 1)
                        vol_ratio = amount / max(avg_vol, 1)
                        if vol_ratio >= vol_break_high:
                            pos['_vol_break_type'] = 'high'
                        elif vol_ratio <= vol_break_low:
                            pos['_vol_break_type'] = 'low'
                        else:
                            pos['_vol_break_type'] = 'normal'
                else:
                    pos['below_ma20_days'] = 0
                    pos.pop('_vol_break_type', None)

                sell = False
                reason = ''
                # 最低持有天数内不卖（防止手续费侵蚀），但硬止损永远生效
                in_min_hold = (date_int - pos['buy_date']) < min_hold_days
                # 保本止损: 浮盈达到触发线后，止损从-10%上移到成本线
                effective_stop = stop_loss_pct
                max_profit_pct = pos.get('max_profit_pct', 0)
                if breakeven_trigger_pct > 0 and max_profit_pct >= breakeven_trigger_pct:
                    effective_stop = 0  # 上移到成本线
                if loss_pct <= effective_stop:  # 止损永远生效，不受min_hold限制
                    sell = True
                    reason = f'止损+{breakeven_trigger_pct}%保本' if effective_stop == 0 else f'止损{effective_stop}%'
                elif below_ma20_deep and not in_min_hold:
                    sell = True
                    reason = f'跌破MA20-{sell_gap_pct}%'
                # 量能感知：调整有效 confirm 天数
                effective_confirm = sell_confirm_days
                # 买入后立即破MA20→加速止损（买错了就认，不等）
                if pos['below_ma20_days'] >= 1:
                    hold_age = di_check - buy_idx
                    if hold_age <= b1_fast_exit:
                        effective_confirm = 1  # 立即止损，不等确认
                        pos['_fast_exit'] = True
                vbt = pos.get('_vol_break_type', 'normal')
                if vbt == 'high':
                    effective_confirm = max(1, sell_confirm_days - vol_high_confirm_adj)
                elif vbt == 'low':
                    effective_confirm = sell_confirm_days + vol_low_confirm_adj
                if pos['below_ma20_days'] >= effective_confirm and not in_min_hold:
                    sell = True
                    vt_label = '放量' if vbt == 'high' else ('缩量' if vbt == 'low' else '')
                    reason = f'跌破MA20({vt_label}连续{effective_confirm}日)'
                elif use_holding_layer and not in_min_hold:
                    # 持有层主动卖出：双轨阈值
                    # 慢牛（轨A）用较低阈值25，保护慢趋势不被误杀
                    # 快牛（轨B）用较高阈值35，积极淘汰动量衰竭的仓位
                    h_score, _ = calc_holding_score(etf, buy_idx, di_check, entry_price)
                    track = pos.get('_track', 'slow')
                    kill_threshold = 25 if track == 'slow' else 35
                    if h_score < kill_threshold:
                        sell = True
                        reason = f'持有分低{track}({h_score:.0f})'
                        pos['holding_score'] = h_score

                if sell:
                    ret_pct = (close / entry_price - 1) * 100
                    max_pct = (max_p / entry_price - 1) * 100
                    hold_days = di_check - buy_idx
                    trades.append({
                        'etf_idx': ei,
                        'code': etf['code'],
                        'name': etf['name'],
                        'track': etf.get('track', ''),
                        'buy_date': pos['buy_date'],
                        'sell_date': etf['dates'][di_check],
                        'entry': round(entry_price, 3),
                        'exit': round(close, 3),
                        'ret_pct': round(ret_pct, 2),
                        'max_pct': round(max_pct, 2),
                        'hold_days': hold_days,
                        'reason': reason,
                        'score': pos['score'],
                        'regime': pos.get('regime', '?'),
                    })
                    to_close.append(ei)
                    cooldown_map[ei] = di + cooldown_days  # 卖出后冷却N个交易日（di是trading_days下标）
                    break  # 卖出跳出循环

            if ei not in to_close:
                # 未卖出：更新 check_from
                pos['check_from'] = idx + 1

        for ei in to_close:
            del positions[ei]

        # --- 买入 + 替换 ---
        # 收集可买入的 ETF（未持仓、未在冷却期）
        gate_failures = {}  # code → failure_reason, 供上帝视角分析
        factor_data = []
        for ei, idx in idx_map.items():
            if ei in positions:
                gate_failures[etf_data_list[ei]['code']] = '已持仓'
                continue
            cool_end = cooldown_map.get(ei, -1)
            if di <= cool_end:  # di是trading_days下标，cool_end也是交易日下标
                remain = cool_end - di + 1  # 剩余冷却天数
                gate_failures[etf_data_list[ei]['code']] = '冷却%dd' % remain
                continue
            etf = etf_data_list[ei]
            closes = etf['closes']
            mas = etf['mas']
            if gate:
                c = closes[idx]; m20 = mas[20][idx]; m60 = mas[60][idx]
                # ① 收盘价 > MA60
                if m20 is None or m60 is None or c <= m60:
                    r = c/m60*100 if m60 and m60>0 else 0
                    gate_failures[etf['code']] = 'C<=MA60(%.0f%%)' % r
                    continue
                # ② MA20 ≥ MA60 × m20ratio（多头排列强度确认）
                ratio = m20 / m60
                if ratio < m20ratio:
                    gap_pp = (ratio - m20ratio) * 100
                    gate_failures[etf['code']] = 'M20/M60=%.3f(差%.1fpp)' % (ratio, gap_pp)
                    continue
                # ②½ 收盘 > MA[N]（长线趋势过滤, gate_ma_n>0时生效）
                if gate_ma_n > 0:
                    if gate_ma_n in mas and mas[gate_ma_n][idx] is not None and c <= mas[gate_ma_n][idx]:
                        r = c/mas[gate_ma_n][idx]*100
                        gate_failures[etf['code']] = 'C<=MA%d(%.0f%%)' % (gate_ma_n, r)
                        continue
                # ③ MA20 必须上行
                if m20 < mas[20][idx - 1]:
                    chg = (m20/mas[20][idx-1]-1)*100
                    gate_failures[etf['code']] = 'MA20↓(%.2f%%)' % chg
                    continue
                # ④ 连续站上MA20 ≥ N天
                _eff_am = above_ma20_min
                if hitech_above_min > 0 and hitech_above_min < _eff_am:
                    track = etf.get('track', '')
                    if _is_hitech(track):
                        _eff_am = hitech_above_min
                above_days = 0; di_check = idx
                while di_check >= 0 and mas[20][di_check] is not None and closes[di_check] >= mas[20][di_check]:
                    above_days += 1; di_check -= 1
                if above_days < _eff_am:
                    gate_failures[etf['code']] = 'above_MA20=%dd(需≥%d)' % (above_days, _eff_am)
                    continue
            # ⑤ 趋势效率比过滤
            if eff_min > 0 and etf.get('trend_eff') and idx < len(etf['trend_eff']):
                eff_v = etf['trend_eff'][idx]
                if eff_v is not None and eff_v < eff_min:
                    continue
            # ⑥ 量比过滤：剔除极缩量(<0.6)和极放量(>2.5)
            if vol_ratio_lo > 0 and etf.get('vol_ratios') and idx < len(etf['vol_ratios']):
                vr = etf['vol_ratios'][idx]
                if vr is not None and (vr < vol_ratio_lo or vr > vol_ratio_hi):
                    continue
            # ⑦ ADX过滤（已默认关闭，之前实验证明有害）
            if adx_min > 0 and etf.get('adx') and idx < len(etf['adx']):
                adx_v = etf['adx'][idx]
                if adx_v is not None and adx_v < adx_min:
                    continue
            # ⑦ 涨幅过滤：近60日起涨点涨幅在合理区间
            if rally_lo > 0 or rally_hi < 999:
                lb = min(60, idx)
                low_idx = idx - lb
                for j in range(idx - lb, idx):
                    if closes[j] < closes[low_idx]:
                        low_idx = j
                rally_pct = (closes[idx] / closes[low_idx] - 1) * 100 if closes[low_idx] > 0 else 0
                if rally_pct < rally_lo or rally_pct > rally_hi:
                    continue
            # ⑧ 累计涨幅过滤：禁止从起涨点（MA20上穿MA60）以来涨幅超过限制的标的
            if max_cumul_gain_pct > 0:
                cumul_gain = 0
                for j in range(idx, max(0, idx-500), -1):
                    if mas[20][j] and mas[60][j] and j > 0 and mas[20][j-1] and mas[60][j-1]:
                        if mas[20][j-1] <= mas[60][j-1] and mas[20][j] > mas[60][j]:
                            cumul_gain = (closes[idx] / closes[j] - 1) * 100
                            break
                if cumul_gain > max_cumul_gain_pct:
                    continue
            b1_map = etf.get('b1_factor', {})
            b1_val = b1_map.get(date_int, 50.0)
            # B1 Gate: 仅允许 B1 分数 ≥ 阈值的 ETF 通过
            if b1_gate and b1_val < b1_gate_threshold:
                gate_failures[etf['code']] = 'B1<%d' % b1_gate_threshold
                continue
            f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val)
            if f['ma20'] is None or f['close'] <= f['ma20']:
                gate_failures[etf['code']] = 'C<=MA20'
                continue
            gap = f['close'] / f['ma20']
            # 金叉窗口乖离放宽：预上穿(≤3天) ~ 金叉后5天，临放宽至 crossover_gap
            # 这段时间高乖离是MA20追赶MA60导致，不视为风险信号
            _effective_max_gap = max_gap
            if crossover_gap > 0 and f['ma60']:
                if f['ma20'] < f['ma60']:
                    # 预上穿：预测≤3天内可上穿
                    for n in range(1, 4):
                        old_sum = sum(closes[idx - 19:idx - 19 + n])
                        ma20_proj = f['ma20'] + (n * closes[idx] - old_sum) / 20
                        if ma20_proj >= f['ma60']:
                            _effective_max_gap = max(max_gap, crossover_gap)
                            break
                elif f['ma20'] >= f['ma60']:
                    # 金叉后：检查是否在5个交易日内刚上穿
                    for lb in range(1, 6):
                        prev_idx = idx - lb
                        if prev_idx >= 0 and mas[20][prev_idx] and mas[60][prev_idx]:
                            if mas[20][prev_idx] < mas[60][prev_idx]:
                                _effective_max_gap = max(max_gap, crossover_gap)
                                break
            # 高波动板块额外乖离放宽（半导体/科创等天然趋势乖离大）
            if hitech_gap > 0:
                track = etf.get('track', '')
                if _is_hitech(track):
                    _effective_max_gap = max(_effective_max_gap, hitech_gap)
            if gap > _effective_max_gap:
                gate_failures[etf['code']] = 'gap=%.1f%%>%.0f%%' % ((gap-1)*100, (_effective_max_gap-1)*100)
                continue
            # 预期收益法：计算因子桶
            if expected_return_table:
                eff_v = etf.get('trend_eff', [None]*len(closes))[idx] or 0
                stab_v = f.get('stab', 0)
                mom_v = f.get('mom20', 0) or 0
                ma120i = mas[120][idx] if 120 in mas else None
                ma250i = mas[250][idx] if 250 in mas else None
                above_days = 0; di_chk = idx
                while di_chk >= 0 and mas[20][di_chk] is not None and closes[di_chk] >= mas[20][di_chk]:
                    above_days += 1; di_chk -= 1
                r20_60 = mas[20][idx] / mas[60][idx] if mas[60][idx] and mas[60][idx] > 0 else 1.0
                gap_v = (closes[idx] / mas[20][idx] - 1) * 100 if mas[20][idx] and mas[20][idx] > 0 else 0
                f['ma120_250_bkt'] = 1 if ma120i and ma250i and ma120i >= ma250i else 0
                f['eff'] = eff_v
                f['above_days'] = above_days
                f['r20_60'] = r20_60
                f['gap'] = gap_v
                # 保留旧桶字段兼容 expected_return
                f['eff_bkt'] = 0 if eff_v < 0.15 else (1 if eff_v < 0.30 else (2 if eff_v < 0.40 else 3))
                f['stab_bkt'] = 0 if stab_v < 1.0 else (1 if stab_v < 1.5 else (2 if stab_v < 2.5 else 3))
                f['mom_bkt'] = 0 if mom_v < 3 else (1 if mom_v < 9 else (2 if mom_v < 12 else 3))
                f['above_bkt'] = 0 if above_days < 7 else (1 if above_days < 15 else (2 if above_days < 20 else 3))
                f['r20_60_bkt'] = 0 if r20_60 < 1.02 else (1 if r20_60 < 1.04 else (2 if r20_60 < 1.06 else 3))
            if dual_track:
                factor_data.append((ei, f, 'A'))
            else:
                factor_data.append((ei, f))

        # === 双轨制：轨B快突破二次扫 ===
        # 轨B专捕强势突破型（ATH/250D新高+短站MA20+高乖离），靠因子共振而非板块名
        # 设计准则：B轨Gate放宽但因子要求更高，独立权重偏向动量信号
        if dual_track and gate:
            factor_data_b = []
            # B轨专属权重：重动量轻稳健，突破+价差扩张+趋势为主，回踩/过热降权
            if track_b_weights is None:
                track_b_weights = dict(weights)
                track_b_weights['breakout_score'] = 25
                track_b_weights['spread_change'] = 22
                track_b_weights['trend'] = 15
                track_b_weights['sharpe_eff'] = 15
                track_b_weights['pullback_confirm'] = 10
                track_b_weights['overheat'] = 1
            
            track_a_codes = {etf_data_list[ei]['code']: ei for ei, idx in idx_map.items()
                           if ei not in positions and di > cooldown_map.get(ei, -1)
                           and etf_data_list[ei]['code'] in 
                           {etf_data_list[fd['ei'] if isinstance(fd, dict) else fd[0]]['code'] 
                            for fd in factor_data}}
            
            for ei, idx in idx_map.items():
                if ei in positions:
                    continue
                cool_end = cooldown_map.get(ei, -1)
                if di <= cool_end:
                    continue
                etf = etf_data_list[ei]
                if etf['code'] in track_a_codes:
                    continue
                closes = etf['closes']
                mas = etf['mas']
                # ①②②½③ 同轨A
                if mas[20][idx] is None or mas[60][idx] is None or closes[idx] <= mas[60][idx]:
                    continue
                if mas[20][idx] < mas[60][idx] * m20ratio:
                    continue
                if gate_ma_n > 0 and gate_ma_n in mas and mas[gate_ma_n][idx] is not None and closes[idx] <= mas[gate_ma_n][idx]:
                    continue
                if mas[20][idx] < mas[20][idx - 1]:
                    continue
                # ④ 放宽: above_ma20 ≥ 3
                abd = 0; dck = idx
                while dck >= 0 and mas[20][dck] is not None and closes[dck] >= mas[20][dck]:
                    abd += 1; dck -= 1
                if abd < 3:
                    continue
                b1_map_b = etf.get('b1_factor', {})
                b1_val_b = b1_map_b.get(date_int, 50.0)
                if b1_gate and b1_val_b < b1_gate_threshold:
                    continue
                f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val_b)
                if f['ma20'] is None or f['close'] <= f['ma20']:
                    continue
                # gap放宽到 1.12
                if f['close'] / f['ma20'] > 1.12:
                    continue
                # breakout ≥ 75（250日新高或ATH）
                if f['breakout_score'] < 75:
                    continue
                factor_data_b.append((ei, f, 'B'))
            
            if factor_data_b:
                scores_a = score_etfs([fd[1] for fd in factor_data], weights, method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi, score_mult_max=score_mult_max)
                scores_b = score_etfs([fd[1] for fd in factor_data_b], track_b_weights, method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi, score_mult_max=score_mult_max)
                
                new_factor_data = []
                new_scores = []
                for i, (ei, f, _) in enumerate(factor_data):
                    new_factor_data.append((ei, f, 'A'))
                    new_scores.append(scores_a[i])
                for i, (ei, f, _) in enumerate(factor_data_b):
                    new_factor_data.append((ei, f, 'B'))
                    # B轨扣8分：Gate放宽的代价，只有真强势才能跨过门槛
                    new_scores.append(max(0, scores_b[i] - 8))
                
                factor_data = new_factor_data
                _scored_dual_track = True
                _dual_scores = new_scores
            else:
                factor_data = [(ei, f, 'A') for ei, f, _ in factor_data]
                _scored_dual_track = False
        else:
            _scored_dual_track = False

        if not factor_data:
            # 即使无候选也保留快照（上帝视角需要空仓日的参考数据）
            if snapshot_callback is not None:
                day_holdings = []
                day_candidates = []
                for ei_p, pos_p in positions.items():
                    etf_p = etf_data_list[ei_p]
                    pidx = idx_map.get(ei_p, len(etf_p['closes']) - 1)
                    cur_price = etf_p['closes'][pidx] if pidx < len(etf_p['closes']) else pos_p['entry_price']
                    ret_pct = round((cur_price / pos_p['entry_price'] - 1) * 100, 2)
                    h_score = pos_p.get('holding_score', pos_p['score'])
                    day_holdings.append({'code': etf_p['code'], 'name': etf_p['name'],
                        'buy_date': pos_p['buy_date'], 'buy_score': pos_p['score'],
                        'holding_score': round(h_score, 1),
                        'entry_price': round(pos_p['entry_price'], 3),
                        'cur_price': round(cur_price, 3),
                        'ret_pct': ret_pct})
                snapshot_callback(date_int, day_holdings, day_candidates, gate_failures)
            continue

        if not _scored_dual_track:
            scores = score_etfs([fd[1] for fd in factor_data], weights, method=scoring_method, expected_return_table=expected_return_table, absolute_score_map=absolute_score_map, pr_lo=pr_lo, pr_hi=pr_hi, score_mult_max=score_mult_max)
        else:
            scores = new_scores
        # B1 确信度加成: B1信号确认(≥threshold)的候选额外加分
        if b1_bonus > 0 and b1_bonus_threshold > 0:
            for fi in range(len(factor_data)):
                fd_ei = factor_data[fi][0] if isinstance(factor_data[fi], (list, tuple)) else factor_data[fi]['ei']
                b1_val = etf_data_list[fd_ei].get('b1_factor', {}).get(date_int, 30)
                if b1_val >= b1_bonus_threshold:
                    scores[fi] += b1_bonus
        ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)

        # === 高分放宽Gate：评分≥阈值的ETF，二次扫描放宽above_ma20(7→3)和gap(1.05→1.12) ===
        if score_relax_threshold > 0 and gate:
            relax_factor_data = []
            relax_ei_list = []
            for ei, idx in idx_map.items():
                if ei in positions:
                    continue
                cool_end = cooldown_map.get(ei, -1)
                if di <= cool_end:
                    continue
                # 跳过已经在 factor_data 中的
                if ei in {fd[0] for fd in factor_data}:
                    continue
                etf = etf_data_list[ei]
                closes = etf['closes']
                mas = etf['mas']
                # ①②②½③ 必须通过（不放松）
                if mas[20][idx] is None or mas[60][idx] is None or closes[idx] <= mas[60][idx]:
                    continue
                if mas[20][idx] < mas[60][idx] * m20ratio:
                    continue
                if gate_ma_n > 0:
                    if gate_ma_n in mas and mas[gate_ma_n][idx] is not None and closes[idx] <= mas[gate_ma_n][idx]:
                        continue
                if mas[20][idx] < mas[20][idx - 1]:
                    continue
                # ④ 放宽: above_ma20 ≥ 3（原7）
                abd = 0; dck = idx
                while dck >= 0 and mas[20][dck] is not None and closes[dck] >= mas[20][dck]:
                    abd += 1; dck -= 1
                if abd < 3:
                    continue
                # 因子计算
                b1_map_c = etf_data_list[ei].get('b1_factor', {})
                b1_val_c = b1_map_c.get(date_int, 50.0)
                f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val_c)
                if f['ma20'] is None or f['close'] <= f['ma20']:
                    continue
                # gap放宽到 1.12
                gap = f['close'] / f['ma20']
                if gap > 1.12:
                    continue
                relax_factor_data.append((ei, f))
                relax_ei_list.append(ei)
            
            if relax_factor_data:
                # 合并到主 factor_data 重新评分
                full_factor_data = factor_data + relax_factor_data
                full_scores = score_etfs([f for _, f in full_factor_data], weights, method=scoring_method, expected_return_table=expected_return_table, absolute_score_map=absolute_score_map, pr_lo=pr_lo, pr_hi=pr_hi, score_mult_max=score_mult_max)
                
                # 只保留原 factor_data 的评分 + 新增中 ≥阈值的
                new_factor_data = []
                new_scores = []
                for i, (ei, f) in enumerate(full_factor_data):
                    sc = full_scores[i]
                    if i < len(factor_data):
                        # 原有的，保留（但分数可能因横截面变化而微调）
                        new_factor_data.append((ei, f))
                        new_scores.append(sc)
                    elif sc >= score_relax_threshold:
                        # 新增的，且分数达标
                        new_factor_data.append((ei, f))
                        new_scores.append(sc)
                
                factor_data = new_factor_data
                scores = new_scores
                ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)

        # 当前持仓持有评分 + 替换决策
        if use_holding_layer:
            pos_scores = []
            for ei, pos in positions.items():
                etf_p = etf_data_list[ei]
                pidx = idx_map.get(ei)
                if pidx is None:
                    continue
                h_score, h_detail = calc_holding_score(etf_p, pos['orig_buy_idx'], pidx, pos['entry_price'])
                pos['holding_score'] = h_score
                pos_scores.append((ei, h_score, pos['score'], h_detail, pos.get('_track', 'slow')))
            pos_scores.sort(key=lambda x: x[1])
        else:
            pos_scores = [(ei, pos['score'], pos['score'], {}, 'slow') for ei, pos in positions.items()]
            pos_scores.sort(key=lambda x: x[1])

        # --- 崩盘检测 ---
        _mr_today = _market_ret.get(date_int) if crash_day_pct < 0 else None
        _is_crash = _mr_today is not None and _mr_today <= crash_day_pct
        _prev_crash = _prev_mr is not None and crash_day_pct < 0 and _prev_mr <= crash_day_pct
        # 连续崩盘清仓
        if _is_crash and crash_liquidate_type == 'consecutive' and _prev_crash:
            to_close_all = [(ei, pos) for ei, pos in positions.items()]
            for ei, pos in to_close_all:
                idx = idx_map.get(ei)
                if idx is not None:
                    close = etf_data_list[ei]['closes'][idx]
                    ret_pct = (close / pos['entry_price'] - 1) * 100
                    trades.append({'etf_idx': ei, 'code': etf_data_list[ei]['code'],
                        'name': etf_data_list[ei]['name'], 'buy_date': pos['buy_date'],
                        'sell_date': date_int, 'entry': round(pos['entry_price'], 3),
                        'exit': round(close, 3), 'ret_pct': round(ret_pct, 2),
                        'max_pct': round((pos['max_price']/pos['entry_price']-1)*100,2),
                        'hold_days': idx - pos['orig_buy_idx'],
                        'reason': f'崩盘清仓(连续-2.3%)', 'score': pos['score']})
                    cooldown_map[ei] = di + cooldown_days
            positions.clear()
        # 累计崩盘清仓
        elif _is_crash and crash_liquidate_type == 'cumulative' and _prev_mr is not None and _mr_today + _prev_mr <= crash_day_pct * 2:
            to_close_all = [(ei, pos) for ei, pos in positions.items()]
            for ei, pos in to_close_all:
                idx = idx_map.get(ei)
                if idx is not None:
                    close = etf_data_list[ei]['closes'][idx]
                    ret_pct = (close / pos['entry_price'] - 1) * 100
                    trades.append({'etf_idx': ei, 'code': etf_data_list[ei]['code'],
                        'name': etf_data_list[ei]['name'], 'buy_date': pos['buy_date'],
                        'sell_date': date_int, 'entry': round(pos['entry_price'], 3),
                        'exit': round(close, 3), 'ret_pct': round(ret_pct, 2),
                        'max_pct': round((pos['max_price']/pos['entry_price']-1)*100,2),
                        'hold_days': idx - pos['orig_buy_idx'],
                        'reason': f'崩盘清仓(累计{_mr_today+_prev_mr:.1f}%)', 'score': pos['score']})
                    cooldown_map[ei] = di + cooldown_days
            positions.clear()

        bought = 0
        # 牛市加仓：Top10均分达标 → buy_max翻倍
        if bull_buy_mult > 1.0:
            top_scores = sorted([s for _, s in ranked], reverse=True)[:10]
            top_avg = sum(top_scores) / len(top_scores) if top_scores else 0
            if bull_top_avg <= 0 or top_avg >= bull_top_avg:
                effective_buy_max = min(effective_max_hold, int(daily_buy_max * bull_buy_mult))
                daily_log.append((date_int, 'bull_on', top_avg, len(positions)))
        for rank_i, (fi, score) in enumerate(ranked):
            # === 资金面硬关：仅 Peaking 状态禁止新买入 ===
            if USE_OAMV_GATE and regime == 'peaking':
                break

            # === 攻防状态机：防守状态禁止买入 ===
            if USE_AD_STATE and prev_ad == 'defense':
                break

            # 崩盘日：只卖不买
            if _is_crash:
                break

            if bought >= effective_buy_max:
                break
            # 买入冷却期：两次买入之间至少间隔 buy_cooldown 个交易日（只在跨天时生效）
            if buy_cooldown > 0 and di != last_buy_di and di - last_buy_di < buy_cooldown:
                break
            fd_item = factor_data[fi]
            ei, f = fd_item[0], fd_item[1]
            idx = idx_map[ei]

            # 信号质量过滤：分数不够不买
            if buy_min_score > 0 and score < buy_min_score:
                continue

            if len(positions) < effective_max_hold:
                # 有空位，直接买入（不管分数、不管冷却）
                style = classify_track(f)
                positions[ei] = {
                    'buy_date': date_int, 'orig_buy_idx': idx,
                    'check_from': idx + 1, 'entry_price': f['close'],
                    'max_price': f['close'], 'score': round(score, 1),
                    'below_ma20_days': 0,
                    'regime': regime,
                    '_track': style,
                }
                bought += 1
                last_buy_di = di
            elif pos_scores:
                # 满仓 → 检查是否替换
                worst_ei, worst_hscore, worst_buy_score, h_detail, worst_track = pos_scores[0]

                # === ① 插队通道：超级强势标的，直接淘汰最弱持仓 ===
                if score >= effective_jump and worst_hscore < BASE_JUMP_HS_MAX:
                    jump_attempts += 1
                    # 超级强势 + 最弱持仓确实不行 → 替换
                    worst_pos = positions[worst_ei]
                    etf_worst = etf_data_list[worst_ei]
                    sell_idx = idx_map.get(worst_ei)
                    if sell_idx is not None:
                        sell_close = etf_worst['closes'][sell_idx]
                    ret_pct = (sell_close / worst_pos['entry_price'] - 1) * 100
                    max_pct = (worst_pos['max_price'] / worst_pos['entry_price'] - 1) * 100
                    hold_days = sell_idx - worst_pos['orig_buy_idx']
                    trades.append({
                        'etf_idx': worst_ei,
                        'code': etf_worst['code'], 'name': etf_worst['name'],
                        'track': etf_worst.get('track', ''),
                        'buy_date': worst_pos['buy_date'],
                        'sell_date': date_int,
                        'entry': round(worst_pos['entry_price'], 3),
                        'exit': round(sell_close, 3),
                        'ret_pct': round(ret_pct, 2),
                        'max_pct': round(max_pct, 2),
                        'hold_days': hold_days,
                        'reason': '被插队',
                        'score': worst_pos['score'],
                        'holding_score': worst_pos.get('holding_score', 50),
                        'regime': worst_pos.get('regime', '?'),
                    })
                    cooldown_map[worst_ei] = di + cooldown_days
                    del positions[worst_ei]

                    # 超级强势标的上车
                    style = classify_track(f)
                    positions[ei] = {
                        'buy_date': date_int, 'orig_buy_idx': idx,
                        'check_from': idx + 1, 'entry_price': f['close'],
                        'max_price': f['close'], 'score': round(score, 1),
                        'below_ma20_days': 0,
                        '_track': style,
                    }
                    bought += 1
                    last_buy_di = di
                    jump_success += 1
                    pos_scores = [(eii, pos.get('holding_score', 50), pos['score'], {}, pos.get('_track', 'slow'))
                                  for eii, pos in positions.items()]
                    pos_scores.sort(key=lambda x: x[1])

                # === ② 常规替换通道 ===
                elif buy_cooldown <= 0 or di - last_buy_di >= buy_cooldown:
                    if use_holding_layer:
                        if worst_hscore <= 30:
                            dyn_threshold = 1.0
                        elif worst_hscore <= 50:
                            dyn_threshold = 2.0
                        else:
                            dyn_threshold = 4.0
                    else:
                        dyn_threshold = 3.0

                    if score >= worst_hscore + dyn_threshold:
                        worst_pos = positions[worst_ei]
                        etf_worst = etf_data_list[worst_ei]
                        sell_idx = idx_map.get(worst_ei)
                        if sell_idx is not None:
                            sell_close = etf_worst['closes'][sell_idx]
                        ret_pct = (sell_close / worst_pos['entry_price'] - 1) * 100
                        max_pct = (worst_pos['max_price'] / worst_pos['entry_price'] - 1) * 100
                        hold_days = sell_idx - worst_pos['orig_buy_idx']
                        trades.append({
                            'etf_idx': worst_ei,
                            'code': etf_worst['code'], 'name': etf_worst['name'],
                            'track': etf_worst.get('track', ''),
                            'buy_date': worst_pos['buy_date'],
                            'sell_date': date_int,
                            'entry': round(worst_pos['entry_price'], 3),
                            'exit': round(sell_close, 3),
                            'ret_pct': round(ret_pct, 2),
                            'max_pct': round(max_pct, 2),
                            'hold_days': hold_days,
                            'reason': '被替换',
                            'score': worst_pos['score'],
                            'holding_score': worst_pos.get('holding_score', 50),
                            'regime': worst_pos.get('regime', '?'),
                        })
                        cooldown_map[worst_ei] = di + cooldown_days
                        del positions[worst_ei]

                        style = classify_track(f)
                        positions[ei] = {
                            'buy_date': date_int, 'orig_buy_idx': idx,
                            'check_from': idx + 1, 'entry_price': f['close'],
                            'max_price': f['close'], 'score': round(score, 1),
                            'below_ma20_days': 0,
                            '_track': style,
                        }
                        bought += 1
                        last_buy_di = di
                        pos_scores = [(eii, pos.get('holding_score', 50), pos['score'], {}, pos.get('_track', 'slow'))
                                      for eii, pos in positions.items()]
                        pos_scores.sort(key=lambda x: x[1])
            else:
                break  # 满仓且无足够优势的替换对象

        # --- 每日快照 ---
        if snapshot_callback is not None:
            # 构建当天通过的候选列表（含因子详情 + pct_rank得分）
            day_candidates = []
            _cand_factors = []
            for fi, _ in ranked:
                ei_c, f_c = factor_data[fi][0], factor_data[fi][1]
                etf_c = etf_data_list[ei_c]
                _cand_factors.append(f_c)
                day_candidates.append({
                    'code': etf_c['code'],
                    'name': etf_c['name'],
                    'score': round(scores[fi], 1),
                    'close': round(f_c['close'], 3),
                    'ma20': round(f_c.get('ma20', 0) or 0, 3),
                    'breakout_score': round(f_c.get('breakout_score', 0) or 0, 0),
                })
            
            # 对当天所有候选做横截面 pct_rank，存入因子得分（0-100）
            if day_candidates:
                factor_keys = ['trend', 'dist', 'mom60', 'mom20', 'liq',
                              'spread_change', 'sharpe_eff', 'pullback_confirm', 'overheat']
                for fk in factor_keys:
                    vals = [fc.get(fk, 0) or 0 for fc in _cand_factors]
                    # pct_rank: inverse for overheat only
                    is_inverse = (fk == 'overheat')
                    sorted_vals = sorted(vals)
                    n = len(vals)
                    ranks = []
                    for v in vals:
                        if is_inverse:
                            cnt = sum(1 for sv in sorted_vals if sv > v) + 0.5 * sum(1 for sv in sorted_vals if sv == v)
                        else:
                            cnt = sum(1 for sv in sorted_vals if sv < v) + 0.5 * sum(1 for sv in sorted_vals if sv == v)
                        ranks.append(round(cnt / n * 100, 1))
                    # breakout_score doesn't use pct_rank (it's absolute 0/25/50/75/100)
                    for i, rv in enumerate(ranks):
                        day_candidates[i][fk + '_score'] = rv
                
                # breakout_score stays as raw (0/25/50/75/100)

            # 当前持仓（含持有评分）
            day_holdings = []
            for ei_p, pos_p in positions.items():
                etf_p = etf_data_list[ei_p]
                pidx = idx_map.get(ei_p, len(etf_p['closes']) - 1)
                cur_price = etf_p['closes'][pidx] if pidx < len(etf_p['closes']) else pos_p['entry_price']
                ret_pct = round((cur_price / pos_p['entry_price'] - 1) * 100, 2)
                h_score = pos_p.get('holding_score', pos_p['score'])
                day_holdings.append({
                    'code': etf_p['code'],
                    'name': etf_p['name'],
                    'buy_date': pos_p['buy_date'],
                    'buy_score': pos_p['score'],
                    'holding_score': round(h_score, 1),
                    'entry_price': round(pos_p['entry_price'], 3),
                    'cur_price': round(cur_price, 3),
                    'ret_pct': ret_pct,
                })

            snapshot_callback(date_int, day_holdings, day_candidates, gate_failures)

        # 更新上日市场收益（崩盘检测用）
        if crash_day_pct < 0:
            _prev_mr = _mr_today

    # --- 强制平仓剩余持仓（逐日检查到最后一根K线）---
    for ei, pos in list(positions.items()):
        etf = etf_data_list[ei]
        last_idx = len(etf['closes']) - 1

        max_p = pos['max_price']
        for di_check in range(pos['check_from'], last_idx + 1):
            high = etf['highs'][di_check]
            if high > max_p:
                max_p = high

            close = etf['closes'][di_check]
            ma20 = etf['mas'][20][di_check] if di_check < len(etf['mas'][20]) else None
            loss_pct = (close / pos['entry_price'] - 1) * 100
            below_ma20 = ma20 is not None and close < ma20
            below_ma20_deep = ma20 is not None and close < ma20 * (1 - sell_gap_pct / 100)

            if below_ma20:
                pos['below_ma20_days'] = pos.get('below_ma20_days', 0) + 1
            else:
                pos['below_ma20_days'] = 0

            sell_final = False
            in_min_hold = (date_int - pos['buy_date']) < min_hold_days
            if (loss_pct <= -8 or below_ma20_deep or pos['below_ma20_days'] >= sell_confirm_days) and not in_min_hold:
                sell_final = True

            if sell_final:
                ret_pct = (close / pos['entry_price'] - 1) * 100
                max_pct = (max_p / pos['entry_price'] - 1) * 100
                hold_days = di_check - pos['orig_buy_idx']
                trades.append({
                    'etf_idx': ei,
                    'code': etf['code'], 'name': etf['name'],
                    'track': etf.get('track', ''),
                    'buy_date': pos['buy_date'],
                    'sell_date': etf['dates'][di_check],
                    'entry': round(pos['entry_price'], 3),
                    'exit': round(close, 3),
                    'ret_pct': round(ret_pct, 2),
                    'max_pct': round(max_pct, 2),
                    'hold_days': hold_days,
                    'reason': '止损-8%' if loss_pct <= -8 else '跌破MA20',
                    'score': pos['score'],
                    'regime': pos.get('regime', '?'),
                })
                break
        else:
            # 没触发卖出，持有到期末
            close = etf['closes'][last_idx]
            ret_pct = (close / pos['entry_price'] - 1) * 100
            max_pct = (max_p / pos['entry_price'] - 1) * 100
            hold_days = last_idx - pos['orig_buy_idx']
            trades.append({
                'etf_idx': ei,
                'code': etf['code'], 'name': etf['name'],
                'track': etf.get('track', ''),
                'buy_date': pos['buy_date'],
                'sell_date': etf['dates'][last_idx],
                'entry': round(pos['entry_price'], 3),
                'exit': round(close, 3),
                'ret_pct': round(ret_pct, 2),
                'max_pct': round(max_pct, 2),
                'hold_days': hold_days,
                'reason': '强制平仓(期末)',
                'score': pos['score'],
                'regime': pos.get('regime', '?'),
            })

    if not trades:
        return None

    # ─── 构建每日组合权益曲线 ───
    # 收集所有相关日期
    all_dates = set(trading_days)
    all_dates = sorted(d for d in all_dates if BACKTEST_START <= d <= BACKTEST_END)
    
    # date→close 映射
    dc = [{} for _ in etf_data_list]
    for ei, etf in enumerate(etf_data_list):
        for i, d in enumerate(etf['dates']):
            dc[ei][d] = etf['closes'][i]

    # 为每笔交易补 etf_idx
    for t in trades:
        if 'etf_idx' not in t:
            for ei, etf in enumerate(etf_data_list):
                if etf['code'] == t['code']:
                    t['etf_idx'] = ei
                    break

    # 按日期排序交易事件
    events = []
    for t in trades:
        events.append(('buy', t['buy_date'], t))
        events.append(('sell', t['sell_date'], t))
    events.sort(key=lambda x: x[1])

    # ─── 构建权益曲线（费前 + 费后双轨）───
    def _sim_equity(events, all_dates, dc, initial_cash, slot_size, with_fee):
        """模拟权益曲线，with_fee=True时计入买入0.1%+赎回费"""
        cash = initial_cash
        positions = {}
        equity = []
        peak = cash
        max_dd = 0.0
        total_fee_cost = 0.0
        evt_idx = 0

        for d in all_dates:
            while evt_idx < len(events) and events[evt_idx][1] == d:
                evt_type, _, t = events[evt_idx]
                ei = t.get('etf_idx')
                if evt_type == 'buy' and ei is not None:
                    entry_price = t['entry']
                    if with_fee:
                        # 买入: 实际成本 = entry * (1 + 0.1%)
                        cost_per_share = entry_price * (1 + BUY_FEE_RATE)
                        fee_this = slot_size * BUY_FEE_RATE
                        total_fee_cost += fee_this
                    else:
                        cost_per_share = entry_price
                    shares = slot_size / cost_per_share
                    cash -= slot_size
                    positions[ei] = {'shares': shares, 'entry': entry_price, 'buy_date': t['buy_date']}
                elif evt_type == 'sell' and ei is not None and ei in positions:
                    pos = positions.pop(ei)
                    exit_price = t['exit']
                    if with_fee:
                        sell_fee_rate = get_fee_rate(pos['buy_date'], t['sell_date'])
                        net_exit = exit_price * (1 - sell_fee_rate)
                        fee_this = pos['shares'] * exit_price * sell_fee_rate
                        total_fee_cost += fee_this
                    else:
                        net_exit = exit_price
                    cash += pos['shares'] * net_exit
                evt_idx += 1

            holdings_val = 0
            for ei, pos in positions.items():
                p = dc[ei].get(d)
                if p:
                    holdings_val += pos['shares'] * p
            total = cash + holdings_val
            equity.append(total)
            if total > peak:
                peak = total
            dd = (total / peak - 1) * 100 if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd

        return equity, max_dd, total_fee_cost

    # 费前权益
    eq_gross, dd_gross, _ = _sim_equity(events, all_dates, dc, 300000, 300000/BASE_MAX_HOLD, with_fee=False)

    # 费后权益
    if apply_fee:
        eq_net, dd_net, total_fee_cost = _sim_equity(events, all_dates, dc, 300000, 300000/BASE_MAX_HOLD, with_fee=True)
    else:
        eq_net, dd_net, total_fee_cost = eq_gross, dd_gross, 0.0

    # ─── 夏普比率（从费后权益曲线）───
    sharpe = calmar = ann_ret = ann_vol = 0.0
    if eq_net and len(eq_net) >= 2:
        daily_rets = []
        for i in range(1, len(eq_net)):
            if eq_net[i-1] > 0:
                daily_rets.append(eq_net[i] / eq_net[i-1] - 1)
        if len(daily_rets) >= 10:
            avg_d = sum(daily_rets) / len(daily_rets)
            var_d = sum((r - avg_d)**2 for r in daily_rets) / (len(daily_rets) - 1)
            std_d = var_d ** 0.5 if var_d > 1e-12 else 0
            if std_d > 1e-10:
                ann_ret = avg_d * 244
                ann_vol = std_d * (244 ** 0.5)
                sharpe = round((ann_ret - 0.025) / ann_vol, 2)  # 无风险利率2.5%
                # Calmar ratio: 年化收益 / |最大回撤|
                if abs(dd_net) > 0.1:
                    calmar = round(ann_ret / abs(dd_net/100), 2)

    # fallback
    if not eq_gross:
        eq_gross = [300000]
    if not eq_net:
        eq_net = [300000]

    rets = [t['ret_pct'] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    hold_days = [t['hold_days'] for t in trades]

    avg_ret = sum(rets) / len(rets)
    win_rate = len(wins) / len(rets) * 100 if rets else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float('inf')

    # 累计收益
    gross_return_pct = round((eq_gross[-1] / 300000 - 1) * 100, 1)
    net_return_pct = round((eq_net[-1] / 300000 - 1) * 100, 1)
    fee_pct = round(total_fee_cost / 300000 * 100, 2)  # 总费率占初始资金%
    final_capital = round(eq_net[-1], 0)

    avg_hold = sum(hold_days) / len(hold_days) if hold_days else 1

    return {
        'trades_count': len(trades),
        'trades': trades,
        'daily_log': daily_log,
        'bull_triggers': sum(1 for x in daily_log if len(x) >= 3 and x[1] == 'bull_on'),
        'avg_ret_pct': round(avg_ret, 2),
        'win_rate': round(win_rate, 1),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'inf',
        'max_dd': round(dd_net, 2),
        'max_dd_gross': round(dd_gross, 2),
        'total_return_pct': net_return_pct,     # 费后收益（主指标）
        'total_return_pct_gross': gross_return_pct,  # 费前收益
        'final_capital': final_capital,
        'avg_hold_days': round(avg_hold, 0),
        'reasons': {r: sum(1 for t in trades if t['reason'] == r) for r in set(t['reason'] for t in trades)},
        'jump_stats': {'attempts': jump_attempts, 'success': jump_success},
        'regime_stats': dict(regime_stats),
        # 费率 + 夏普
        'fee_pct': fee_pct,
        'sharpe': sharpe,
        'ann_return_pct': round(ann_ret * 100, 1) if ann_ret else 0.0,
        'ann_vol_pct': round(ann_vol * 100, 1) if ann_vol else 0.0,
        'calmar': calmar,
    }


# ========== 主流程 ==========

def main():
    t0 = time.time()
    print("=" * 60)
    print("强势动量策略回测")
    print("=" * 60)

    # 1. 加载名称
    print("\n[1/5] 加载 ETF 名称表...")
    names = load_names()
    print(f"  名称表条目: {len(names)}")

    # 2. 加载 QBETF 并去重
    print("\n[2/5] 加载全部 ETF 数据并去重...")
    blkpath = os.path.join(BLKDIR, 'QBETF.blk')
    all_codes = parse_blk(blkpath)
    print(f"  QBETF.blk 原始: {len(all_codes)} 只")

    # 先快速扫描所有 ETF，获取最新成交额用于去重
    codes_with_data = []
    for prefix, code in all_codes:
        p = day_path(prefix, code)
        if not p:
            continue
        try:
            dates, opens, highs, lows, closes, amounts = read_day(p, code)
            if len(closes) < 120:  # 至少需要 120 天数据
                continue
            # 用最新一天的成交额
            latest_amt = amounts[-1] if amounts else 0
            codes_with_data.append((prefix, code, latest_amt))
        except Exception:
            continue

    print(f"  有足够数据的: {len(codes_with_data)} 只")

    # 去重
    selected_codes = deduplicate_etfs(codes_with_data)
    selected_set = set(f"{p}{c}" for p, c in selected_codes)

    # 3. 加载去重后 ETF 的完整历史数据
    print("\n[3/5] 加载去重后 ETF 的完整历史数据并预计算均线...")
    etf_data_list = []
    load_skipped = 0
    load_total = len(selected_codes)
    for i, (prefix, code) in enumerate(selected_codes):
        if (i + 1) % 50 == 0:
            print(f"  加载进度: {i + 1}/{load_total}")

        p = day_path(prefix, code)
        if not p:
            load_skipped += 1
            continue
        try:
            dates, opens, highs, lows, closes, amounts = read_day(p, code)
            if len(closes) < 120:
                load_skipped += 1
                continue
        except Exception:
            load_skipped += 1
            continue

        # 前复权处理（修正除权除息导致的假暴跌）
        closes, opens, highs, lows = forward_adjust(closes, opens, highs, lows)

        # 预计算各周期均线（用前复权后的价格）
        mas = {n: sma(closes, n) for n in NEED_MA}

        # 预计算 ADX 和趋势效率比（用于市场状态过滤）
        adx_vals = calc_adx(highs, lows, closes)
        trend_eff = calc_trend_efficiency(closes)

        # 预计算量比（5日均量/20日均量）
        vol_ratios = [None] * len(closes)
        vol_ma5 = sma(amounts, 5)
        vol_ma20 = sma(amounts, 20)
        for i in range(len(closes)):
            if vol_ma5[i] and vol_ma20[i] and vol_ma20[i] > 0:
                vol_ratios[i] = vol_ma5[i] / vol_ma20[i]

        etf_data_list.append({
            'code': code,
            'name': names.get(code, code),
            'track': extract_track(names.get(code, code)),
            'dates': dates,
            'opens': opens,
            'highs': highs,
            'lows': lows,
            'closes': closes,
            'amounts': amounts,
            'mas': mas,
            'adx': adx_vals,
            'trend_eff': trend_eff,
            'vol_ratios': vol_ratios,
        })

    print(f"  成功加载: {len(etf_data_list)} 只, 跳过: {load_skipped}")

    # 4. 确定回测交易日（需要所有 ETF 都在同一天有数据的截面）
    print("\n[4/5] 确定回测截面日期...")
    # 收集所有 ETF 的日期集
    date_sets = []
    for etf in etf_data_list:
        date_set = set(d for d in etf['dates']
                       if BACKTEST_START <= d <= BACKTEST_END)
        date_sets.append(date_set)

    # 取交集（所有 ETF 都需要有数据的交易日只取少数几个关键截面）
    # 实际上每个 ETF 上市时间不同，交集太小。改为：每月取一个截面
    # 截面日期：每月1日和15日附近
    all_dates = set()
    for ds in date_sets:
        all_dates.update(ds)
    all_dates = sorted(d for d in all_dates if BACKTEST_START <= d <= BACKTEST_END)

    # 采样：每 20 个交易日取一个截面（约每月一次）
    sample_dates = []
    for i in range(0, len(all_dates), 20):
        sample_dates.append(all_dates[i])

    # 为每个截面日期，找到每个 ETF 在该日期的索引
    eval_dates = []
    for date_int in sample_dates:
        idx_map = {}
        valid_count = 0
        for ei, etf in enumerate(etf_data_list):
            try:
                idx = etf['dates'].index(date_int)
                if idx >= 60:  # 至少需要 60 天历史数据在该日期
                    idx_map[ei] = idx
                    valid_count += 1
            except ValueError:
                pass
        if valid_count >= 20:  # 至少有 20 只 ETF
            eval_dates.append((date_int, idx_map))


    # 5. 训练预期收益表 + 对比实验
    print("\n[5/5] 训练预期收益表 + 对比实验...")
    
    # 训练: 2023-07 ~ 2024-06, 只用Gate过滤的信号
    TRAIN_END = 20240630
    train_signals = []
    for etf in etf_data_list:
        cl = etf['closes']; mas = etf['mas']
        ev = calc_trend_efficiency(cl)
        for i in range(250, len(cl)-20, 20):
            d = etf['dates'][i]
            if d > TRAIN_END: break
            c = cl[i]; mv = mas[20][i]; m60i = mas[60][i]
            if not mv or not m60i: continue
            if c <= m60i or mv < m60i: continue
            if not mas[20][i-1] or mv < mas[20][i-1]: continue
            above = 0; di_chk = i
            while di_chk>=0 and mas[20][di_chk] and cl[di_chk]>=mas[20][di_chk]: above+=1; di_chk-=1
            if above<7: continue
            if ev[i] is None or ev[i]<0.2: continue
            if mv < m60i*1.02: continue
            last=len(cl)-1; below=0; ret=0
            for j in range(i+1,last+1):
                if j<len(mas[20]) and mas[20][j] and cl[j]<mas[20][j]: below+=1
                else: below=0
                if below>=2: ret=(cl[j]/c-1)*100; break
                if cl[j]/c-1<=-0.08: ret=(cl[j]/c-1)*100; break
            else: ret=(cl[last]/c-1)*100
            stab_v = statistics.pstdev([cl[j]/cl[j-1]-1 for j in range(i-19,i+1)])*100
            mom20_v = (c/cl[i-20]-1)*100 if i>=20 else 0
            eff_v = ev[i] or 0
            ma120i=mas[120][i]; ma250i=mas[250][i]
            r20_60 = mv/m60i if m60i>0 else 1.0
            train_signals.append({'ret':ret,
                'ma120_250_bkt': 1 if ma120i and ma250i and ma120i>=ma250i else 0,
                'eff_bkt': 0 if eff_v<0.15 else (1 if eff_v<0.30 else (2 if eff_v<0.40 else 3)),
                'stab_bkt': 0 if stab_v<1.0 else (1 if stab_v<1.5 else (2 if stab_v<2.5 else 3)),
                'mom_bkt': 0 if mom20_v<3 else (1 if mom20_v<9 else (2 if mom20_v<12 else 3)),
                'above_bkt': 0 if above<7 else (1 if above<15 else (2 if above<20 else 3)),
                'r20_60_bkt': 0 if r20_60<1.02 else (1 if r20_60<1.04 else (2 if r20_60<1.06 else 3)),
            })

    er_table = {}
    for bk in ['ma120_250_bkt','eff_bkt','stab_bkt','mom_bkt','above_bkt','r20_60_bkt']:
        er_table[bk] = {}
        for bv in [0,1,2,3]:
            grp = [s['ret'] for s in train_signals if s[bk]==bv]
            er_table[bk][bv] = round(sum(grp)/len(grp),2) if grp else 0
    print(f"训练信号: {len(train_signals)}, 预期收益表: {er_table}")

    base_weights = {
        'trend': 15, 'mom60': 8, 'mom20': 5, 'liq': 10,
        'dist': 6, 'spread_change': 15, 'sharpe_eff': 15,
        'pullback_confirm': 18, 'overheat': 8, 'breakout_score': 10,
    }
    gcfg = {'above_ma20_min':7,'eff_min':0,'m20ratio':1.02,'rally_lo':0,'rally_hi':999,'vol_ratio_lo':0,'vol_ratio_hi':999}

    # 加载绝对分映射表
    abs_map = None
    map_path = os.path.join(os.path.dirname(__file__), 'absolute_score_map.json')
    if os.path.exists(map_path):
        with open(map_path, 'r') as f:
            raw_map = json.load(f)
        abs_map = {}
        for fn, info in raw_map.items():
            abs_map[fn] = {
                'boundaries': info['boundaries'],
                'avg_rets': {int(k): v for k, v in info['avg_rets'].items()},
                'min_ret': info['min_ret'],
                'max_ret': info['max_ret'],
            }
        print(f"  加载绝对分映射表: {list(abs_map.keys())}")
    else:
        print(f"  警告: 未找到 {map_path}")

    experiments = [
        # (label, scoring, gcfg, hold_layer, cooldown, er_tbl, min_score, abs_map, daily_buy_max, use_oamv_gate, use_ad_state)
        ("[BASELINE] 保本5%+量能感知", 'pct_rank', gcfg, True, 0, None, 62, None, 3, False, False),
        ("[0AMV] 仅Peaking禁买", 'pct_rank', gcfg, True, 0, None, 62, None, 3, True, False),
        ("[AD] +4%进攻/-2.3%防守", 'pct_rank', gcfg, True, 0, None, 62, None, 3, False, True),
    ]

    # ====== A+H 限定池实验 ======
    print(f"\n{'='*80}")
    print(f"  A+H 限定池实验（排除海外/商品 ETF）")
    print(f"{'='*80}")

    overseas_kw = ['纳指','标普','日经','德国','法国','韩国','印度','越南','巴西','道琼斯','MSCI','海外','全球']
    commodity_kw = ['黄金','金ETF','石油','油气','能源ETF','商品','有色','白银','豆粕','原油','煤炭']
    ah_data = []
    excluded_names = []
    for etf in etf_data_list:
        name = etf['name']
        if any(k in name for k in overseas_kw) or any(k in name for k in commodity_kw):
            excluded_names.append(name)
            continue
        ah_data.append(etf)
    print(f"  A+H ETF: {len(ah_data)} (排除 {len(excluded_names)} 只海外/商品)")
    if excluded_names:
        print(f"  排除名单: {', '.join(excluded_names[:10])}...")

    if ah_data:
        # A+H BASELINE
        ah_r = evaluate_weights(ah_data, base_weights, gate=True, max_hold=10,
                                daily_buy_max=3, min_hold_days=15, buy_cooldown=0,
                                sell_confirm_days=4, sell_gap_pct=10,
                                scoring_method='pct_rank', use_holding_layer=True,
                                breakeven_trigger_pct=5, buy_min_score=62,
                                use_oamv_gate=False, use_ad_state=False, **gcfg)
        # A+H + 0AMV
        ah_r2 = evaluate_weights(ah_data, base_weights, gate=True, max_hold=10,
                                 daily_buy_max=3, min_hold_days=15, buy_cooldown=0,
                                 sell_confirm_days=4, sell_gap_pct=10,
                                 scoring_method='pct_rank', use_holding_layer=True,
                                 breakeven_trigger_pct=5, buy_min_score=62,
                                 use_oamv_gate=True, use_ad_state=False, **gcfg)
        # A+H + AD
        ah_r3 = evaluate_weights(ah_data, base_weights, gate=True, max_hold=10,
                                 daily_buy_max=3, min_hold_days=15, buy_cooldown=0,
                                 sell_confirm_days=4, sell_gap_pct=10,
                                 scoring_method='pct_rank', use_holding_layer=True,
                                 breakeven_trigger_pct=5, buy_min_score=62,
                                 use_oamv_gate=False, use_ad_state=True, **gcfg)
        if ah_r:
            print(f"\n  {'实验':<40} {'交易':>6} {'胜率':>6} {'费前收益':>9} {'费后收益':>9} {'费率':>7} {'回撤':>8} {'夏普':>6}")
            print(f"  {'-'*85}")
            for ah_label, ar in [("[A+H] BASELINE", ah_r), ("[A+H+0AMV] Peaking禁买", ah_r2), ("[A+H+AD] 攻防禁买", ah_r3)]:
                if not ar: continue
                fc = ar['final_capital']
                fee = ar.get('fee_pct', 0)
                gross_ret = ar.get('total_return_pct_gross', ar['total_return_pct'])
                net_ret = ar['total_return_pct']
                sharpe = ar.get('sharpe', 0)
                dd = ar['max_dd']
                tr = Counter(t.get('regime','?') for t in ar['trades'])
                print(f"  {ah_label:<40} {ar['trades_count']:>6} {ar['win_rate']:>5.1f}% {gross_ret:>+8.1f}% {net_ret:>+8.1f}% {fee:>+6.1f}% {dd:>+7.1f}% {sharpe:>+5.2f}")
                print(f"    交易分布: buy@R:{tr.get('rising',0)} F:{tr.get('falling',0)} P:{tr.get('peaking',0)} B:{tr.get('bottoming',0)}")

    for label, scoring, gcfg, hold_layer, cooldown, er_tbl, min_score, abs_tbl, daily_buy_max, use_oamv, use_ad in experiments:
        r = evaluate_weights(etf_data_list, base_weights, gate=True, max_hold=10,
                             daily_buy_max=daily_buy_max,
                             min_hold_days=15, buy_cooldown=cooldown, scoring_method=scoring,
                             sell_confirm_days=4, sell_gap_pct=10,
                             use_holding_layer=hold_layer, expected_return_table=er_tbl,
                             absolute_score_map=abs_tbl, buy_min_score=min_score,
                             breakeven_trigger_pct=5,
                             use_oamv_gate=use_oamv, use_ad_state=use_ad, **gcfg)
        if r:
            fc = r['final_capital']
            js = r.get('jump_stats', {})
            rs = r.get('regime_stats', {})
            regime_str = f"R:{rs.get('rising',0)} F:{rs.get('falling',0)} P:{rs.get('peaking',0)} B:{rs.get('bottoming',0)}"
            # Trades by regime
            tr = Counter(t.get('regime','?') for t in r['trades'])
            tr_str = f"buy@R:{tr.get('rising',0)} F:{tr.get('falling',0)} P:{tr.get('peaking',0)} B:{tr.get('bottoming',0)}"
            print(f"  {label:<28} {r['trades_count']:>6} {r['win_rate']:>5.1f}% ¥{fc:>8,.0f} {r['total_return_pct']:>7.1f}% {r['max_dd']:>7.1f}%")
            fee_pct = r.get('fee_pct', 0)
            gross = r.get('total_return_pct_gross', r['total_return_pct'])
            sharpe = r.get('sharpe', 0)
            calmar = r.get('calmar', 0)
            print(f"    费前{gross:+.1f}% | 费后{r['total_return_pct']:+.1f}% | 费率{fee_pct:.1f}% | 夏普{sharpe:.2f} | Calmar{calmar:.2f}")
            print(f"    {regime_str:<20}")
            print(f"    交易分布: {tr_str}")
            print(f"    卖出原因: {r['reasons']}")
            trades_sorted = sorted(r['trades'], key=lambda t: t['ret_pct'], reverse=True)
            print(f"    Top3: ", end='')
            for t in trades_sorted[:3]:
                print(f"{t['code']}({t['ret_pct']:+.1f}%) ", end='')
            print(f"  Bot3: ", end='')
            for t in trades_sorted[-3:]:
                print(f"{t['code']}({t['ret_pct']:+.1f}%) ", end='')
            print()

    elapsed = time.time() - t0
    print(f"\n总耗时: {elapsed:.1f} 秒")

    # ====== 按资金面状态分析 BASELINE 交易分布 ======
    _do_trade_regime_analysis(etf_data_list, base_weights, gcfg)


def _do_trade_regime_analysis(etf_data_list, base_weights, gcfg):
    """按 0AMV 状态分组分析 BASELINE 交易表现"""
    print(f"\n{'='*80}")
    print(f"  BASELINE 交易按 0AMV 状态分组分析")
    print(f"{'='*80}")

    r = evaluate_weights(etf_data_list, base_weights, gate=True, max_hold=10,
                         daily_buy_max=3, min_hold_days=15, buy_cooldown=0,
                         sell_confirm_days=4, sell_gap_pct=10,
                         scoring_method='pct_rank', use_holding_layer=True,
                         buy_min_score=62,
                         use_oamv_gate=False, use_ad_state=False, **gcfg)
    if not r:
        return

    trades = r['trades']
    by_regime = defaultdict(list)
    for t in trades:
        regime = t.get('regime', '?')
        by_regime[regime].append(t)

    for reg in ['rising', 'falling', 'peaking', 'bottoming']:
        ts = by_regime.get(reg, [])
        if not ts:
            continue
        rets = [t['ret_pct'] for t in ts]
        wins = [r for r in rets if r > 0]
        avg_ret = sum(rets) / len(rets)
        total = sum(rets)
        print(f"\n  [{reg.upper()}] {len(ts)} trades  WR={len(wins)/len(ts)*100:.1f}%  avg={avg_ret:+.1f}%  total={total:+.1f}%")

        # Return distribution
        bins = [(-100, -15), (-15, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 100)]
        print(f"  分布: ", end='')
        for lo, hi in bins:
            cnt = sum(1 for r in rets if lo <= r < hi)
            if cnt > 0:
                print(f"[{lo:>4}~{hi:>4}]:{cnt} ", end='')
        print()

        # Top 10 & Bottom 10
        ts_sorted = sorted(ts, key=lambda t: t['ret_pct'], reverse=True)
        print(f"  Top5: ", end='')
        for t in ts_sorted[:5]:
            n = t.get('name', '?')
            if len(n) > 12: n = n[:11] + '.'
            print(f"{t['code']}({n}: {t['ret_pct']:+.1f}%) ", end='')
        print()
        print(f"  Bot5: ", end='')
        for t in ts_sorted[-5:]:
            n = t.get('name', '?')
            if len(n) > 12: n = n[:11] + '.'
            print(f"{t['code']}({n}: {t['ret_pct']:+.1f}%) ", end='')
        print()


if __name__ == '__main__':
    main()
