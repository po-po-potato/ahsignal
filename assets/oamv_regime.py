# -*- coding: utf-8 -*-
"""
0AMV 活筹指数估算 + 资金面状态识别
基于通达信本地 .day 数据
公式: 0AMV = SMA(沪深两市成交额, 10) * 上证收盘 / MA(REF(上证收盘, 1), 5)
"""

import struct
import os

VIPDOC = r"C:/zd_zsone/vipdoc"
DAY_REC = 32


def read_day(path):
    """读取 .day 文件，返回 (dates, closes, amounts)"""
    if not os.path.exists(path):
        return [], [], []
    with open(path, 'rb') as f:
        data = f.read()
    n = len(data) // DAY_REC
    dates, closes, amounts = [], [], []
    for i in range(n):
        o = i * DAY_REC
        date, op, hi, lo, cl, amount, vol, _ = struct.unpack("<IIIIIfII", data[o:o + DAY_REC])
        dates.append(date)
        closes.append(cl / 100.0)  # 指数以 100 为因子
        amounts.append(amount)
    return dates, closes, amounts


def sma(series, window):
    """简单移动平均"""
    result = []
    for i in range(len(series)):
        if i < window - 1:
            result.append(float('nan'))
        else:
            result.append(sum(series[i - window + 1:i + 1]) / window)
    return result


def compute_oamv():
    """计算全时段 0AMV 序列"""
    sh_dates, sh_closes, sh_amounts = read_day(
        os.path.join(VIPDOC, "sh", "lday", "sh000001.day"))
    sz_dates, sz_closes, sz_amounts = read_day(
        os.path.join(VIPDOC, "sz", "lday", "sz399001.day"))

    # 对齐日期（取交集）
    date_to_sh = {d: (c, a) for d, c, a in zip(sh_dates, sh_closes, sh_amounts)}
    date_to_sz = {d: (c, a) for d, c, a in zip(sz_dates, sz_closes, sz_amounts)}

    common_dates = sorted(set(date_to_sh.keys()) & set(date_to_sz.keys()))

    pairs = []
    for d in common_dates:
        sh_c, sh_a = date_to_sh[d]
        sz_c, sz_a = date_to_sz[d]
        pairs.append((d, sh_c, sh_a + sz_a))  # date, sh_close, total_amount

    if not pairs:
        return {}, {}

    dates = [p[0] for p in pairs]
    sh_closes = [p[1] for p in pairs]
    total_amounts = [p[2] for p in pairs]

    # SMA(total_amount, 10)
    sma10_amt = sma(total_amounts, 10)

    # MA(REF(close, 1), 5): 前一日收盘的5日MA
    ref_closes = [float('nan')] + sh_closes[:-1]  # REF(sh_close, 1)
    ma5_ref = sma(ref_closes, 5)

    # 0AMV
    oamv = {}
    for i, (d, sh_c, amt) in enumerate(pairs):
        if i < 9:  # SMA10 needs 10 days
            continue
        if i < 5 + 1:  # MA5 + REF needs 6 days
            continue
        if ma5_ref[i] is None or ma5_ref[i] == 0:
            continue
        v = sma10_amt[i] * sh_c / ma5_ref[i]
        oamv[d] = v / 1e8  # Convert to 亿元

    # Regime detection
    # Rising:  oamv > ma10 AND ma5 > ma20
    # Falling: oamv < ma10 AND ma5 < ma20
    # Peaking: index 20d high but oamv not 20d high
    # Bottoming: oamv crosses above ma10 from below

    date_list = sorted(oamv.keys())
    oamv_list = [oamv[d] for d in date_list]
    sh_close_list = [date_to_sh[d][0] for d in date_list]

    ma5 = sma(oamv_list, 5)
    ma10 = sma(oamv_list, 10)
    ma20 = sma(oamv_list, 20)
    sh_ma20_high = sma(sh_close_list, 20)  # not true max, but 20d moving window max proxy
    oamv_ma20_high = sma(oamv_list, 20)

    # True rolling max for peaking detection
    sh_20h = []
    oamv_20h = []
    for i in range(len(date_list)):
        sh_20h.append(max(sh_close_list[max(0, i - 19):i + 1]))
        oamv_20h.append(max(oamv_list[max(0, i - 19):i + 1]))

    MIN_BLOCK = 3
    regime_raw = {}
    regimes = {}  # date → (regime, label)

    for i, d in enumerate(date_list):
        if i < 20:
            regime_raw[d] = 'warmup'
            continue
        o = oamv_list[i]
        m5 = ma5[i]
        m10 = ma10[i]
        m20 = ma20[i]
        sh = sh_close_list[i]

        # Peaking detection
        is_index_peak = sh >= sh_20h[i] * 0.995
        is_oamv_lagging = o < oamv_20h[i] * 0.97
        if is_index_peak and is_oamv_lagging:
            regime_raw[d] = 'peaking'
            continue

        # Rising
        if o > m10 and m5 > m20:
            regime_raw[d] = 'rising'
            continue

        # Falling
        if o < m10 and m5 < m20:
            regime_raw[d] = 'falling'
            continue

        # Bottoming: cross above ma10 in last 5 days
        is_bottoming = False
        for j in range(max(0, i - 4), i + 1):
            if j > 0:
                prev_o = oamv_list[j - 1]
                prev_m10 = ma10[j - 1]
                curr_o = oamv_list[j]
                curr_m10 = ma10[j]
                if (not (prev_o is None or prev_m10 is None or curr_o is None or curr_m10 is None)):
                    if prev_o <= prev_m10 and curr_o > curr_m10:
                        is_bottoming = True
                        break
        if is_bottoming:
            regime_raw[d] = 'bottoming'
            continue

        regime_raw[d] = 'transitional'

    # Merge short blocks (min 3 days)
    block_list = []
    current = None
    start_i = 0
    for i, d in enumerate(date_list):
        r = regime_raw.get(d, 'warmup')
        if r != current:
            if current is not None:
                block_list.append((current, start_i, i - 1))
            current = r
            start_i = i
    if current is not None:
        block_list.append((current, start_i, len(date_list) - 1))

    # Assign final regime, merging short blocks into transitional
    for regime_label, si, ei in block_list:
        days = ei - si + 1
        for j in range(si, ei + 1):
            d = date_list[j]
            if days >= MIN_BLOCK and regime_label not in ('warmup', 'transitional'):
                regimes[d] = regime_label
            else:
                regimes[d] = 'transitional'

    return oamv, regimes


def get_attack_defense_state(oamv_up=4.0, defense_dn=-2.0, up_days=1,
                            defense_cumul_days=0, defense_cumul_pct=0,
                            ma_consec_days=0, ma_period=10):
    """进攻/防守状态机
    进攻: 连续up_days天上涨且累计涨幅 >= oamv_up% 触发
    防守触发条件（任一满足即触发）:
      1. 单日跌幅 <= defense_dn%
      2. defense_cumul_days日累计跌幅 <= defense_cumul_pct%
      3. 连续ma_consec_days天0AMV < MA[ma_period]
    返回: date_int -> 'attack' or 'defense'
    """
    oamv_dict, _ = get_oamv_regime()
    dates = sorted(oamv_dict.keys())
    vals = [oamv_dict[d] for d in dates]
    
    # Pre-compute MAs
    ma_vals = None
    if ma_consec_days > 0:
        ma_vals = [None] * len(vals)
        s = 0.0
        for i, v in enumerate(vals):
            s += v
            if i >= ma_period:
                s -= vals[i - ma_period]
            if i >= ma_period - 1:
                ma_vals[i] = s / ma_period

    state = {}
    current = 'attack'
    for i, d in enumerate(dates):
        if i < max(up_days, defense_cumul_days, ma_consec_days):
            state[d] = current
            continue
        
        # Defense trigger 1: 单日大跌
        pct_1d = (vals[i] / vals[i-1] - 1) * 100
        if pct_1d <= defense_dn:
            current = 'defense'
            state[d] = current
            continue
        
        # Defense trigger 2: 多日累计下跌
        if defense_cumul_days > 0:
            cumul_dn = (vals[i] / vals[i - defense_cumul_days] - 1) * 100
            if cumul_dn <= defense_cumul_pct:
                current = 'defense'
                state[d] = current
                continue
        
        # Defense trigger 3: 连续N天跌破MA
        if ma_consec_days > 0 and ma_vals[i] is not None:
            below_count = 0
            for j in range(ma_consec_days):
                if vals[i - j] < ma_vals[i - j]:
                    below_count += 1
                else:
                    break
            if below_count >= ma_consec_days:
                current = 'defense'
                state[d] = current
                continue
        
        # Attack trigger: 单日涨幅≥ oamv_up% OR (连续up_days天涨 + 累计≥ oamv_up%)
        if pct_1d >= oamv_up:
            current = 'attack'
        else:
            all_up = True
            for j in range(1, up_days + 1):
                if (vals[i - up_days + j] / vals[i - up_days + j - 1] - 1) * 100 <= 0:
                    all_up = False
                    break
            cumul = (vals[i] / vals[i - up_days] - 1) * 100
            if all_up and cumul >= oamv_up:
                current = 'attack'
        
        state[d] = current

    return state


# ====== Cached singleton ======
_oamv_cache = None
_regime_cache = None
_ad_state_cache = None


def get_ad_state(up_days=2, defense_dn=-2.0, oamv_up=4.0, defense_cumul_days=0, defense_cumul_pct=0,
                 ma_consec_days=0, ma_period=10):
    """返回进攻/防守状态 dict: date_int → 'attack'/'defense'"""
    global _ad_state_cache
    param_key = (up_days, defense_dn, oamv_up, defense_cumul_days, defense_cumul_pct, ma_consec_days, ma_period)
    if _ad_state_cache is None:
        _ad_state_cache = (param_key, get_attack_defense_state(
            up_days=up_days, defense_dn=defense_dn, oamv_up=oamv_up,
            defense_cumul_days=defense_cumul_days, defense_cumul_pct=defense_cumul_pct,
            ma_consec_days=ma_consec_days, ma_period=ma_period))
        a = sum(1 for v in _ad_state_cache[1].values() if v == 'attack')
        d = sum(1 for v in _ad_state_cache[1].values() if v == 'defense')
        print(f"[0AMV] Attack/Defense state: attack={a}d defense={d}d")
    elif _ad_state_cache[0] != param_key:
        _ad_state_cache = (param_key, get_attack_defense_state(
            up_days=up_days, defense_dn=defense_dn, oamv_up=oamv_up,
            defense_cumul_days=defense_cumul_days, defense_cumul_pct=defense_cumul_pct,
            ma_consec_days=ma_consec_days, ma_period=ma_period))
        a = sum(1 for v in _ad_state_cache[1].values() if v == 'attack')
        d = sum(1 for v in _ad_state_cache[1].values() if v == 'defense')
        print(f"[0AMV] Attack/Defense state (updated): attack={a}d defense={d}d")
    return _ad_state_cache[1]


def get_oamv_regime():
    """返回 (oamv_dict, regime_dict) 两个 dict: date_int → value"""
    global _oamv_cache, _regime_cache
    if _oamv_cache is None:
        _oamv_cache, _regime_cache = compute_oamv()
        print(f"[0AMV] Computed {len(_oamv_cache)} daily values, "
              f"regime dist: rising={sum(1 for v in _regime_cache.values() if v=='rising')}, "
              f"falling={sum(1 for v in _regime_cache.values() if v=='falling')}, "
              f"peaking={sum(1 for v in _regime_cache.values() if v=='peaking')}, "
              f"bottoming={sum(1 for v in _regime_cache.values() if v=='bottoming')}, "
              f"transitional={sum(1 for v in _regime_cache.values() if v=='transitional')}")
    return _oamv_cache, _regime_cache


def get_regime_params(regime):
    """根据资金面状态返回策略参数调整"""
    if regime == 'rising':
        return {'max_hold': 12, 'jump_threshold': 72, 'daily_buy_max': 3,
                'label': 'RISING'}
    elif regime == 'peaking':
        return {'max_hold': 8, 'jump_threshold': 82, 'daily_buy_max': 1,
                'label': 'PEAKING'}
    elif regime == 'falling':
        return {'max_hold': 6, 'jump_threshold': 85, 'daily_buy_max': 1,
                'label': 'FALLING'}
    elif regime == 'bottoming':
        return {'max_hold': 8, 'jump_threshold': 75, 'daily_buy_max': 2,
                'label': 'BOTTOMING'}
    else:  # transitional / warmup → default
        return {'max_hold': 10, 'jump_threshold': 75, 'daily_buy_max': 3,
                'label': 'DEFAULT'}


# ====== Command-line test ======
if __name__ == '__main__':
    oamv, regimes = get_oamv_regime()
    print(f"\nTotal OAMV dates: {len(oamv)}")
    print(f"Total regime dates: {len(regimes)}")

    # Show some samples
    import datetime
    test_dates = [20260105, 20260407, 20260513, 20260701, 20260717, 20260805]
    for d in test_dates:
        o = oamv.get(d, 'N/A')
        r = regimes.get(d, 'N/A')
        params = get_regime_params(r) if r != 'N/A' else {}
        print(f"  {d}: OAMV={o:.0f if isinstance(o, float) else o}  regime={r}  params={params.get('label','')}")
