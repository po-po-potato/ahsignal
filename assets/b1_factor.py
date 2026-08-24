# -*- coding: utf-8 -*-
"""
B1因子模块：在周线级别检测B1信号，信号后连续N周站上M周线→加分
7种B1子信号（超卖缩量拐头B/超卖缩量B/原始B1/超卖超缩量B/回踩白线B/回踩超级B/回踩黄线B）
统一视为"B1信号买入提示"

设计理念：
  - 周线检测B1信号 → 信号后连续站上短期均线视为趋势确认 → 加分
  - 未确认或过期 → 中立分
  - 默认参数: 信号后3周连续站上5周线 = 确认, 7周未确认 = 失效
"""

import os

# ========== 日线转周线 ==========

def daily_to_weekly(dates, opens, highs, lows, closes, amounts):
    """日线数据聚合为周线（周一~周五为一周）
    返回: w_dates(list), w_opens, w_highs, w_lows, w_closes, w_amounts
    每周取: open=周一开盘, high=周最高, low=周最低, close=周五收盘, amount=周成交额和"""
    import datetime
    n = len(dates)
    if n < 5:
        return [], [], [], [], [], []

    weeks = []
    current_week = None
    week_data = []

    for i in range(n):
        dt = datetime.date(dates[i] // 10000, (dates[i] // 100) % 100, dates[i] % 100)
        iso = dt.isocalendar()
        week_key = (iso[0], iso[1])  # (year, week_number)

        if week_key != current_week:
            if week_data:
                weeks.append(week_data)
            current_week = week_key
            week_data = [(dates[i], opens[i], highs[i], lows[i], closes[i], amounts[i])]
        else:
            week_data.append((dates[i], opens[i], highs[i], lows[i], closes[i], amounts[i]))

    if week_data:
        weeks.append(week_data)

    w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts = [], [], [], [], [], []
    for wd in weeks:
        w_dates.append(wd[0][0])  # 周一日期
        w_opens.append(wd[0][1])  # 周一开盘
        w_highs.append(max(d[2] for d in wd))
        w_lows.append(min(d[3] for d in wd))
        w_closes.append(wd[-1][4])  # 周五收盘
        w_amounts.append(sum(d[5] for d in wd))

    return w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts


# ========== 指标计算 ==========

def sma_series(values, n):
    """简单移动平均"""
    L = len(values)
    out = [None] * L
    if L < n or n <= 0:
        return out
    s = sum(values[:n])
    out[n - 1] = s / n
    for i in range(n, L):
        s += values[i] - values[i - n]
        out[i] = s / n
    return out


def ema_series(values, n):
    """EMA: alpha = 2/(n+1)"""
    L = len(values)
    out = [None] * L
    if L < n:
        return out
    # 找到第n个非None值作为起点
    valid_count = 0
    start_idx = 0
    for i in range(L):
        if values[i] is not None:
            valid_count += 1
            if valid_count >= n:
                start_idx = i
                break
    else:
        return out  # 没有足够的非None值

    # 用前n个有效值的SMA作为初始值
    valid_vals = [v for v in values[:start_idx + 1] if v is not None]
    s = sum(valid_vals[-n:]) / n
    out[start_idx] = s
    alpha = 2.0 / (n + 1)
    for i in range(start_idx + 1, L):
        if values[i] is not None:
            s = alpha * values[i] + (1 - alpha) * s
        out[i] = s
    return out


def llv_series(values, n):
    """滚动N周期最低值"""
    L = len(values)
    out = [None] * L
    for i in range(L):
        start = max(0, i - n + 1)
        window = [v for v in values[start:i + 1] if v is not None]
        if window:
            out[i] = min(window)
    return out


def hhv_series(values, n):
    """滚动N周期最高值"""
    L = len(values)
    out = [None] * L
    for i in range(L):
        start = max(0, i - n + 1)
        window = [v for v in values[start:i + 1] if v is not None]
        if window:
            out[i] = max(window)
    return out


def tdx_sma(values, n, m):
    """通达信SMA: SMA(X,N,M) = (M*X + (N-M)*SMA_prev) / N
    n=周期, m=权重"""
    L = len(values)
    out = [None] * L
    if L < n:
        return out
    # 初始值用SMA
    init = sum(v for v in values[:n] if v is not None) / n
    out[n - 1] = init
    for i in range(n, L):
        if values[i] is not None and out[i - 1] is not None:
            out[i] = (m * values[i] + (n - m) * out[i - 1]) / n
        elif out[i - 1] is not None:
            out[i] = out[i - 1]
    return out


def calc_kdj(highs, lows, closes, n=9):
    """标准KDJ计算
    返回: K, D, J 三个数组"""
    L = len(closes)
    K = [None] * L
    D = [None] * L
    J = [None] * L

    if L < n:
        return K, D, J

    # 计算RSV
    rsv = [None] * L
    for i in range(n - 1, L):
        h = max(highs[i - n + 1:i + 1])
        l = min(lows[i - n + 1:i + 1])
        if h - l > 1e-9:
            rsv[i] = (closes[i] - l) / (h - l) * 100
        else:
            rsv[i] = 50.0

    # K=2/3*K_prev + 1/3*RSV, D=2/3*D_prev + 1/3*K
    k_val = 50.0
    d_val = 50.0
    for i in range(L):
        if rsv[i] is not None:
            k_val = 2.0 / 3.0 * k_val + 1.0 / 3.0 * rsv[i]
        K[i] = k_val
        d_val = 2.0 / 3.0 * d_val + 1.0 / 3.0 * k_val
        D[i] = d_val
        J[i] = 3 * k_val - 2 * d_val

    return K, D, J


def calc_rsi(closes, period=3):
    """RSI计算（通达信方式：SMA(MAX(C-LC,0),N,1)/SMA(ABS(C-LC),N,1)*100）"""
    L = len(closes)
    if L < period + 1:
        return [None] * L

    gain = [None] * L
    abs_chg = [None] * L
    for i in range(1, L):
        chg = closes[i] - closes[i - 1]
        gain[i] = max(chg, 0)
        abs_chg[i] = abs(chg)

    sma_gain = tdx_sma(gain, period, 1)
    sma_abs = tdx_sma(abs_chg, period, 1)

    rsi = [None] * L
    for i in range(L):
        if sma_gain[i] is not None and sma_abs[i] is not None and sma_abs[i] > 1e-9:
            rsi[i] = sma_gain[i] / sma_abs[i] * 100
    return rsi


# ========== B1公式核心指标 ==========

def calc_b1_indicators(weekly_data):
    """在周线数据上计算所有B1公式所需的核心指标
    weekly_data = (w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts)
    返回 dict 包含所有中间指标"""
    w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts = weekly_data
    n = len(w_closes)

    result = {
        'dates': w_dates, 'opens': w_opens, 'highs': w_highs,
        'lows': w_lows, 'closes': w_closes, 'amounts': w_amounts,
        'n': n,
    }

    if n < 120:
        return result

    # === 趋势白线: EMA(EMA(C,10),10) ===
    ema10 = ema_series(w_closes, 10)
    trend_white = ema_series(ema10, 10)  # EMA of EMA
    result['trend_white'] = trend_white

    # === 大哥黄线: (MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4 ===
    ma14 = sma_series(w_closes, 14)
    ma28 = sma_series(w_closes, 28)
    ma57 = sma_series(w_closes, 57)
    ma114 = sma_series(w_closes, 114)
    yellow_line = [None] * n
    for i in range(n):
        if all(v is not None for v in [ma14[i], ma28[i], ma57[i], ma114[i]]):
            yellow_line[i] = (ma14[i] + ma28[i] + ma57[i] + ma114[i]) / 4
    result['yellow_line'] = yellow_line

    # === BBI: (MA(C,3)+MA(C,6)+MA(C,12)+MA(C,24))/4 ===
    ma3 = sma_series(w_closes, 3)
    ma6 = sma_series(w_closes, 6)
    ma12 = sma_series(w_closes, 12)
    ma24 = sma_series(w_closes, 24)
    bbi = [None] * n
    for i in range(n):
        if all(v is not None for v in [ma3[i], ma6[i], ma12[i], ma24[i]]):
            bbi[i] = (ma3[i] + ma6[i] + ma12[i] + ma24[i]) / 4
    result['bbi'] = bbi

    # === 短期 = 100*(C-LLV(L,3))/(HHV(C,3)-LLV(L,3)) ===
    llv_l3 = llv_series(w_lows, 3)
    hhv_c3 = hhv_series(w_closes, 3)
    short_term = [None] * n
    for i in range(n):
        if llv_l3[i] is not None and hhv_c3[i] is not None and hhv_c3[i] > llv_l3[i]:
            short_term[i] = 100 * (w_closes[i] - llv_l3[i]) / (hhv_c3[i] - llv_l3[i])
    result['short_term'] = short_term

    # === 长期 = 100*(C-LLV(L,21))/(HHV(C,21)-LLV(L,21)) ===
    llv_l21 = llv_series(w_lows, 21)
    hhv_c21 = hhv_series(w_closes, 21)
    long_term = [None] * n
    for i in range(n):
        if llv_l21[i] is not None and hhv_c21[i] is not None and hhv_c21[i] > llv_l21[i]:
            long_term[i] = 100 * (w_closes[i] - llv_l21[i]) / (hhv_c21[i] - llv_l21[i])
    result['long_term'] = long_term

    # === KDJ ===
    K, D, J = calc_kdj(w_highs, w_lows, w_closes, 9)
    result['K'] = K
    result['D'] = D
    result['J'] = J

    # === RSI(3) ===
    rsi = calc_rsi(w_closes, 3)
    result['rsi'] = rsi

    # === 基础均线（5周/20周/60周）===
    result['ma5'] = sma_series(w_closes, 5)
    result['ma10'] = sma_series(w_closes, 10)
    result['ma20'] = sma_series(w_closes, 20)
    result['ma60'] = sma_series(w_closes, 60)

    # === 成交量相关 ===
    result['vol_hhv20'] = hhv_series(w_amounts, 20)
    result['vol_hhv30'] = hhv_series(w_amounts, 30)
    result['vol_hhv40'] = hhv_series(w_amounts, 40)
    result['vol_hhv50'] = hhv_series(w_amounts, 50)

    # VDAY: 40周期内最大成交量距今的bar数
    vday = [None] * n
    for i in range(n):
        start = max(0, i - 39)
        window = w_amounts[start:i + 1]
        if window:
            max_idx = window.index(max(window))
            vday[i] = i - (start + max_idx)
    result['vday'] = vday

    return result


# ========== B1信号检测 ==========

def detect_b1_signals(indi, ampl_range=5.0, relax_coef=1.0):
    """检测7种B1信号（周线级别）
    ampl_range=5: 振幅区间（去掉了CODELIKE, 默认5）
    relax_coef=1.0: 放宽系数（去掉了CODELIKE, 默认1.0）

    返回: [{signal_type, week_idx, week_date, J, RSI, ...}] 按周索引排序
    """
    n = indi['n']
    if n < 120:
        return []

    closes = indi['closes']
    opens = indi['opens']
    highs = indi['highs']
    lows = indi['lows']
    amounts = indi['amounts']
    trend_white = indi.get('trend_white', [])
    yellow_line = indi.get('yellow_line', [])
    bbi = indi.get('bbi', [])
    short_term = indi.get('short_term', [])
    long_term = indi.get('long_term', [])
    J = indi.get('J', [])
    K = indi.get('K', [])
    rsi = indi.get('rsi', [])
    ma5 = indi.get('ma5', [])
    ma20 = indi.get('ma20', [])
    ma60 = indi.get('ma60', [])
    vol_hhv20 = indi.get('vol_hhv20', [])
    vol_hhv30 = indi.get('vol_hhv30', [])
    vol_hhv40 = indi.get('vol_hhv40', [])
    vol_hhv50 = indi.get('vol_hhv50', [])
    vday = indi.get('vday', [])

    signals = []

    def _ok(val):
        return val is not None

    def _safe_min(arr, start, end):
        """安全取min，过滤None"""
        vals = [v for v in arr[start:end + 1] if v is not None]
        return min(vals) if vals else None

    def _safe_max(arr, start, end):
        """安全取max，过滤None"""
        vals = [v for v in arr[start:end + 1] if v is not None]
        return max(vals) if vals else None

    def _ref(arr, i, offset):
        """REF: 前offset根bar的值"""
        j = i - offset
        if j < 0 or j >= len(arr):
            return None
        return arr[j]

    def _barslast(arr, i, cond_fn):
        """BARSLAST: 最近一次满足条件距今的bar数"""
        for j in range(i, -1, -1):
            if cond_fn(j):
                return i - j
        return 999  # 从未发生

    def _count(arr, start, end, cond_fn):
        """COUNT: 区间内满足条件的次数"""
        cnt = 0
        for j in range(max(0, start), min(end + 1, len(arr))):
            if cond_fn(j):
                cnt += 1
        return cnt

    def _every(arr, start, end, cond_fn):
        """EVERY: 区间内所有bar都满足条件"""
        for j in range(max(0, start), min(end + 1, len(arr))):
            if not cond_fn(j):
                return False
        return True

    # 预计算 RSI+J 的 25周期滚动最小值
    rj_vals = [rsi[i] + J[i] if (_ok(rsi[i]) and _ok(J[i])) else None for i in range(n)]
    rj_min_25 = [None] * n
    for i in range(n):
        start = max(0, i - 24)
        vals = [v for v in rj_vals[start:i + 1] if v is not None]
        if vals:
            rj_min_25[i] = min(vals)

    for i in range(114, n):  # 需要至少114根周线（大哥黄线最长MA）
        if not all(_ok(arr[i]) for arr in [closes, opens, highs, lows, amounts,
                                            trend_white, yellow_line, J, rsi,
                                            short_term, long_term]):
            continue

        c = closes[i]
        o = opens[i]
        h = highs[i]
        l = lows[i]
        v = amounts[i]

        # ========== 基础判断 ==========
        # 当日振幅
        daily_ampl = (h - l) / l * 100 if l > 0 else 0

        # 当日涨跌幅
        prev_c = _ref(closes, i, 1) if i >= 1 else c
        daily_chg = abs(c - prev_c) / prev_c * 100 * relax_coef if prev_c > 0 else 0

        # 上涨十字星
        up_cross_star = c > prev_c and (abs(c - o) / o * 100 * relax_coef) < 1.8 if o > 0 and prev_c > 0 else False

        # 单针下20
        st_i = short_term[i] if _ok(short_term[i]) else 0
        lt_i = long_term[i] if _ok(long_term[i]) else 0
        needle20 = (st_i <= 20 and lt_i >= 75) or ((lt_i - st_i) >= 70)

        # 聚宝盆: COUNT(长期>=75,8)>=6 AND COUNT(短期<=70,7)>=4 AND COUNT(短期<=50,8)>=1
        pot_of_gold = (_count(long_term, i - 7, i, lambda j: _ok(long_term[j]) and long_term[j] >= 75) >= 6 and
                       _count(short_term, i - 6, i, lambda j: _ok(short_term[j]) and short_term[j] <= 70) >= 4 and
                       _count(short_term, i - 7, i, lambda j: _ok(short_term[j]) and short_term[j] <= 50) >= 1)

        # 双叉戟: EVERY(长期>=75,8) AND COUNT(短期<=50,6)>=2 AND COUNT(短期<=20,7)>=1
        trident = (_every(long_term, i - 7, i, lambda j: _ok(long_term[j]) and long_term[j] >= 75) and
                   _count(short_term, i - 5, i, lambda j: _ok(short_term[j]) and short_term[j] <= 50) >= 2 and
                   _count(short_term, i - 6, i, lambda j: _ok(short_term[j]) and short_term[j] <= 20) >= 1)

        # 红肥绿瘦: COUNT(C>=O,15)>7 OR COUNT(C>REF(C,1),11)>5
        red_strong = (_count(closes, i - 14, i, lambda j: closes[j] >= opens[j]) > 7 or
                      _count(closes, i - 10, i, lambda j: j > 0 and closes[j] > closes[j - 1]) > 5)

        # ========== 量能条件 ==========
        # 大绿棒
        vd = vday[i] if _ok(vday[i]) else 0
        vday_c = closes[i - vd] if i - vd >= 0 else 0
        vday_o = opens[i - vd] if i - vd >= 0 else 0
        vday_pc = closes[i - vd - 1] if i - vd - 1 >= 0 else 0
        not_big_green = (vday_c >= vday_pc if vday_pc > 0 else True) or (vday_c >= vday_o if vday_o > 0 else True)
        big_green = not not_big_green
        big_green_far = vd >= 15 and big_green

        # 缩量
        hv20 = vol_hhv20[i] if _ok(vol_hhv20[i]) else v
        hv50 = vol_hhv50[i] if _ok(vol_hhv50[i]) else v
        shrink = (v < hv20 * 0.416) or (v < hv50 / 3)

        # 回踩缩量
        shrink_pullback = (v < hv20 * 0.45) or (v < hv50 / 3)

        # 适当缩量
        shrink_moderate = (v < hv20 * 0.618) or (v < hv50 / 3)

        # 超缩量
        hv30 = vol_hhv30[i] if _ok(vol_hhv30[i]) else v
        super_shrink = (v < hv30 / 4) or (v < hv50 / 6)

        # ========== 趋势判定 ==========
        tw = trend_white[i] if _ok(trend_white[i]) else 0
        yl = yellow_line[i] if _ok(yellow_line[i]) else 0

        # 做上涨趋势
        uptrend = tw >= yl and (c >= yl or (c > yl * 0.975 and c > o))

        # ========== 异动判定 ==========
        # 近期异动 (N=20)
        h20 = max(highs[max(0, i - 19):i + 1]) if i >= 20 else h
        l20 = min(lows[max(0, i - 19):i + 1]) if i >= 20 else l
        recent_amp = (h20 - l20) / l20 * 100 if l20 > 0 else 0
        h12 = max(highs[max(0, i - 11):i + 1]) if i >= 12 else h
        l14 = min(lows[max(0, i - 13):i + 1]) if i >= 14 else l
        recent_amp2 = (h12 - l14) / l14 * 100 if l14 > 0 else 0
        recent_turb = recent_amp >= 15 or recent_amp2 >= 11

        # 远期异动 (M=50)
        h50 = max(highs[max(0, i - 49):i + 1]) if i >= 50 else h
        l50 = min(lows[max(0, i - 49):i + 1]) if i >= 50 else l
        far_amp = (h50 - l50) / l50 * 100 if l50 > 0 else 0
        far_turb = far_amp >= 30

        # 洗盘异动
        wash_turb = (_count(short_term, i - 9, i, lambda j: needle20_helper(short_term, long_term, j)) >= 2 or
                     pot_of_gold or trident)

        # 超级异动
        super_turb = recent_amp >= 60

        # RSI+J 合值 (用于 LLV(RSI+J, 25))
        _safe_min_rj = rj_min_25[i]

        # ========== 趋势股判定 ==========
        # 强趋势股
        bbi_arr = bbi
        prev_yl = _ref(yellow_line, i, 1) if i >= 1 else yl
        prev_tw = _ref(trend_white, i, 1) if i >= 1 else tw

        strong_trend = (_every(yellow_line, i - 12, i,
                              lambda j: _ok(yellow_line[j]) and _ok(_ref(yellow_line, j, 1)) and
                              yellow_line[j] >= _ref(yellow_line, j, 1) * 0.999) and
                        _ok(prev_tw) and tw >= prev_tw and
                        _every(trend_white, i - 19, i,
                              lambda j: _ok(trend_white[j]) and _ok(yellow_line[j]) and
                              trend_white[j] > yellow_line[j]) and
                        _every(trend_white, i - 10, i,
                              lambda j: _ok(trend_white[j]) and _ok(_ref(trend_white, j, 1)) and
                              trend_white[j] >= _ref(trend_white, j, 1)) and
                        red_strong)

        # 超牛股
        bbi_20_up = _every(bbi_arr, i - 19, i,
                          lambda j: _ok(bbi_arr[j]) and _ok(_ref(bbi_arr, j, 1)) and
                          bbi_arr[j] >= _ref(bbi_arr, j, 1) * 0.999)
        bbi_25_near = _count(bbi_arr, i - 24, i,
                            lambda j: _ok(bbi_arr[j]) and _ok(_ref(bbi_arr, j, 1)) and
                            bbi_arr[j] >= _ref(bbi_arr, j, 1)) >= 23
        # BARSLAST(CROSS(C,大哥黄线))
        cross_yl_bars = _barslast(closes, i, lambda j: (j > 0 and
                                 _ok(yellow_line[j]) and _ok(yellow_line[j-1]) and
                                 closes[j] > yellow_line[j] and closes[j - 1] <= yellow_line[j - 1]))

        super_bull = ((bbi_20_up or bbi_25_near) and
                      (recent_amp >= 30 or far_amp > 80) and
                      cross_yl_bars > 12)

        # ========== 回踩条件 ==========
        # 距离白线
        dist_white = abs(c - tw) / c * 100 if c > 0 else 0
        L_dist_white = abs(l - tw) / tw * 100 if tw > 0 else 0

        # 距离BBI
        bb_val = bbi[i] if _ok(bbi[i]) else 0
        dist_bbi = abs(c - bb_val) / c * 100 if c > 0 else 0
        L_dist_bbi = abs(l - bb_val) / bb_val * 100 if bb_val > 0 else 0

        # 回踩白线
        pullback_white = ((c >= tw and dist_white <= 2) or
                          (c < tw and dist_white < 0.8) or
                          (c >= bb_val and dist_bbi < 2.5 and L_dist_bbi < 1 and
                           dist_white <= 3 and daily_chg < 1 and c > prev_c))

        # 白线支撑
        white_support = c >= tw and dist_white < 1.5

        # 强势回踩不破
        strong_pullback = ((L_dist_white < 1 or L_dist_bbi < 0.5) and
                           c > tw and dist_white <= 3.5)

        # ========== 回踩黄线 ==========
        dist_yellow = abs(c - yl) / yl * 100 if yl > 0 else 0
        pullback_yellow = ((c >= yl and (dist_yellow <= 1.5 or
                                        (dist_yellow <= 2 and daily_chg < 1))) or
                           (c < yl and dist_yellow <= 0.8))

        # ========== 异动或 ==========
        has_turb = recent_turb or far_turb or wash_turb

        # ========== 7种B1信号检测 ==========
        j_val = J[i] if _ok(J[i]) else 50
        rsi_val = rsi[i] if _ok(rsi[i]) else 50
        prev_rsi = _ref(rsi, i, 1) if i >= 1 else rsi_val
        prev_J = _ref(J, i, 1) if i >= 1 else j_val
        prev_yl_val = _ref(yellow_line, i, 1) if i >= 1 else yl
        prev_tw_val = _ref(trend_white, i, 1) if i >= 1 else tw

        signal_hit = None

        # 1) 超卖缩量拐头B (RSI拐头+缩量) — 原色: COLOR00D0FF
        if (uptrend and (rsi_val - 15) >= prev_rsi and
                (prev_rsi < 20 or prev_J < 14) and
                daily_ampl < (ampl_range + 0.5) and
                (daily_chg < 2.3 or up_cross_star) and
                (not big_green or big_green_far) and
                has_turb and c >= yl):
            signal_hit = '拐头B'

        # 2) 超卖缩量B — 原色: COLOR5656FF
        elif (uptrend and (j_val < 14 or rsi_val < 23) and
              (rsi_val + j_val < 55 or (_safe_min(J, max(0, i - 19), i) is not None and j_val == _safe_min(J, max(0, i - 19), i))) and
              daily_ampl < ampl_range and
              (daily_chg < 2.5 or up_cross_star) and
              (not big_green or big_green_far) and
              (shrink or (shrink_moderate and daily_chg < 1)) and
              has_turb):
            signal_hit = '缩量B'

        # 3) 原始B1 — 原色: COLORWHITE (不与缩量B同时出现)
        elif (tw > yl and c >= yl * 0.99 and yl >= prev_yl_val and
              (j_val < 13 or rsi_val < 21) and
              (rsi_val + j_val) < (_safe_min(J, max(0, i - 14), i) or 0) * 1.5 and
              shrink_moderate and
              (not big_green or big_green_far) and
              (abs(c - o) * 100 / o < 1.5 or super_shrink or
               (shrink_moderate and (dist_white < 1.8 or dist_bbi < 1.5 or dist_yellow < 2.8))) and
              has_turb):
            signal_hit = '原始B1'

        # 4) 超卖超缩量B — 原色: COLORCYAN
        elif (uptrend and (j_val < 14 or rsi_val < 23) and
              rsi_val + j_val < 60 and
              far_amp >= 45 and
              (daily_ampl < ampl_range or (super_turb and daily_ampl < ampl_range + 3.2 and c > o and c > tw)) and
              ((c < o and v < _ref(amounts, i, 1) and c >= yl) or (c >= o)) and
              (daily_chg < 2 or up_cross_star) and
              (not big_green or big_green_far) and
              super_shrink and
              has_turb):
            signal_hit = '超缩量B'

        # 5) 回踩白线B — 原色: COLOREA75AF (不与缩量B/原始B1同时出现)
        elif (strong_trend and (j_val < 30 or rsi_val < 40 or wash_turb) and
              rsi_val + j_val < 70 and
              (daily_ampl < ampl_range + 0.5 or dist_white < 1 or dist_bbi < 1) and
              pullback_white and
              (daily_chg < 2 or (daily_chg < 5 and white_support)) and
              (not big_green or big_green_far) and
              shrink_pullback and
              has_turb and l <= prev_c):
            signal_hit = '回踩白B'

        # 6) 回踩超级B — 原色: COLOR5ACC0A (不与缩量B/拐头B/超缩量B同时出现)
        elif (super_bull and (j_val < 35 or rsi_val < 45 or wash_turb) and
              rsi_val + j_val < 80 and
              (_safe_min_rj is not None and (rsi_val + j_val) == _safe_min_rj) and
              daily_ampl < ampl_range + 1 and
              (daily_chg < 2.5 or dist_white < 2) and
              strong_pullback and
              (not big_green or big_green_far) and
              has_turb and
              shrink_moderate):
            signal_hit = '回踩超级B'

        # 7) 回踩黄线B — 原色: COLOR00D0FF
        elif (tw >= yl and c >= yl * 0.975 and
              (j_val < 13 or rsi_val < 18) and
              pullback_yellow and
              (not big_green or big_green_far) and
              (shrink or (shrink_moderate and
                          ((_safe_min(J, max(0, i - 19), i) is not None and j_val == _safe_min(J, max(0, i - 19), i)) or
                           (_safe_min(rsi, max(0, i - 13), i) is not None and rsi_val == _safe_min(rsi, max(0, i - 13), i))))) and
              yl >= prev_yl_val * 0.997 and
              _ok(ma60[i]) and _ok(_ref(ma60, i, 1)) and ma60[i] >= _ref(ma60, i, 1) and
              recent_amp >= 11.9 and far_amp >= 19.5):
            signal_hit = '回踩黄B'

        if signal_hit:
            signals.append({
                'type': signal_hit,
                'week_idx': i,
                'week_date': indi['dates'][i],
                'J': round(j_val, 1),
                'RSI': round(rsi_val, 1),
                'close': round(c, 3),
                'ma5': round(ma5[i], 3) if _ok(ma5[i]) else None,
            })

    return signals


def needle20_helper(short_term, long_term, j):
    """单针下20辅助判断"""
    st = short_term[j] if short_term[j] is not None else 50
    lt = long_term[j] if long_term[j] is not None else 50
    return (st <= 20 and lt >= 75) or ((lt - st) >= 70)


# ========== B1因子计算 ==========

def calc_b1_factor(daily_dates, weekly_dates, weekly_closes, weekly_ma5, b1_signals,
                   confirm_weeks=3, expiry_weeks=7,
                   weekly_ma10=None, weekly_ma20=None, weekly_opens=None):
    """B1因子每日分数。严格模式(传入ma10/ma20/opens):
      - 信号周MA10和MA20必须上行
      - confirm_weeks=2时第2周收盘须 > 第1周开盘"""
    n_weeks = len(weekly_dates)
    n_days = len(daily_dates)
    strict = (weekly_ma10 is not None and weekly_ma20 is not None and weekly_opens is not None)

    cw = confirm_weeks
    def streak_to_score(s):
        if s <= 0: return 35
        if s < cw: return int(30 + s / cw * 25)
        if s == cw: return 100
        if s == cw + 1: return 88
        if s == cw + 2: return 78
        return 68

    day_week_map = {}
    wi = 0
    for di in range(n_days):
        dd = daily_dates[di]
        while wi + 1 < n_weeks and weekly_dates[wi + 1] <= dd:
            wi += 1
        if wi < n_weeks:
            day_week_map[dd] = wi

    signal_map = {s['week_idx']: s for s in b1_signals}
    week_state = [None] * n_weeks

    for sig_wi in sorted(signal_map):
        sig_valid = True
        if strict and sig_wi >= 1:
            m10r = (weekly_ma10[sig_wi] and weekly_ma10[sig_wi-1] and weekly_ma10[sig_wi] >= weekly_ma10[sig_wi-1])
            m20r = (weekly_ma20[sig_wi] and weekly_ma20[sig_wi-1] and weekly_ma20[sig_wi] >= weekly_ma20[sig_wi-1])
            if not (m10r and m20r):
                sig_valid = False

        streak = 0; max_streak = 0; close_gt_open_ok = False
        for wi in range(sig_wi + 1, min(sig_wi + 1 + expiry_weeks, n_weeks)):
            if weekly_ma5[wi] is not None and weekly_closes[wi] >= weekly_ma5[wi]:
                streak += 1
                if streak > max_streak:
                    max_streak = streak
                if strict and cw == 2 and streak >= 2 and sig_wi + 2 < n_weeks:
                    o1 = weekly_opens[sig_wi + 1]
                    c2 = weekly_closes[wi]
                    if o1 and c2 and c2 > o1:
                        close_gt_open_ok = True
            else:
                streak = 0

            weeks_since = wi - sig_wi
            expired = (max_streak < cw and weeks_since >= expiry_weeks)
            if strict and cw == 2 and max_streak >= cw and not close_gt_open_ok:
                expired = (weeks_since >= expiry_weeks)

            if week_state[wi] is not None:
                continue
            week_state[wi] = (sig_wi, streak, max_streak if sig_valid else 0, expired or not sig_valid)

    result = {}
    for dd in daily_dates:
        if dd not in day_week_map:
            result[dd] = 30; continue
        dw = day_week_map[dd]
        ws = week_state[dw]
        if ws is None:
            result[dd] = 30
        else:
            _, _, ms, expired = ws
            result[dd] = 30 if expired else max(30, streak_to_score(ms))
    return result


# ========== 主入口：为ETF计算B1因子 ==========

def compute_b1_for_etf(daily_dates, daily_opens, daily_highs, daily_lows, daily_closes, daily_amounts,
                       confirm_weeks=3, expiry_weeks=7, strict=False):
    """一次性计算某只ETF的B1因子每日分数
    strict=True: 信号周需MA10/MA20上行, confirm=2时需第2周收盘>第1周开盘
    返回: dict {date_int: factor_score (0-100)}
    """
    if len(daily_closes) < 250:
        return {}

    # 日线转周线
    w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts = daily_to_weekly(
        daily_dates, daily_opens, daily_highs, daily_lows, daily_closes, daily_amounts)

    if len(w_closes) < 120:
        return {}

    # 计算B1指标
    indi = calc_b1_indicators((w_dates, w_opens, w_highs, w_lows, w_closes, w_amounts))

    if indi.get('n', 0) < 120:
        return {}

    # 检测B1信号
    signals = detect_b1_signals(indi)

    # 计算B1因子
    weekly_ma5 = indi.get('ma5', [])
    weekly_ma10 = indi.get('ma10', []) if strict else None
    weekly_ma20 = indi.get('ma20', []) if strict else None
    w_opens_arr = w_opens if strict else None

    factor = calc_b1_factor(daily_dates, w_dates, w_closes, weekly_ma5, signals,
                            confirm_weeks=confirm_weeks, expiry_weeks=expiry_weeks,
                            weekly_ma10=weekly_ma10, weekly_ma20=weekly_ma20,
                            weekly_opens=w_opens_arr)

    return factor


# ========== 批量预计算（用于回测）==========

def precompute_b1_factors(etf_data_list, confirm_weeks=3, expiry_weeks=7, strict=False):
    """为所有ETF预计算B1因子"""
    factors = []
    for i, etf in enumerate(etf_data_list):
        f = compute_b1_for_etf(
            etf['dates'], etf.get('opens', []), etf.get('highs', []),
            etf.get('lows', []), etf['closes'], etf['amounts'],
            confirm_weeks=confirm_weeks, expiry_weeks=expiry_weeks,
            strict=strict)
        factors.append(f)
        if i % 100 == 0:
            sig_count = sum(1 for v in f.values() if v >= 100)
            print(f"  B1 precompute: {i + 1}/{len(etf_data_list)} ETFs, "
                  f"#{i + 1} has {sig_count} confirmed days")
    return factors


# ======================================================================
# 日线 B1：直接在日线上检测 B1 信号 + 连续站上 MA 加分
# 参数映射: 3周→15天, 5周线→25MA, 7周过期→35天
# ======================================================================

def streak_to_score_daily(s, confirm_days=15):
    """日线连续站上MA的评分映射（峰值在确认日）"""
    # 按比例映射: confirm_days/3 = days_equivalent_per_week
    one_w = max(1, confirm_days // 3)
    if s <= 0: return 35
    if s <= one_w: return 42          # ≈1周
    if s <= one_w * 2: return 55      # ≈2周
    if s <= confirm_days: return 100  # 峰值：确认成立
    if s <= confirm_days + one_w: return 88  # 降分
    if s <= confirm_days + one_w * 2: return 78
    return 68  # 太久


def calc_b1_daily_factor(dates, closes, daily_ma, b1_signals,
                         confirm_days=15, expiry_days=35):
    """日线 B1 因子：B1信号后连续 confirm_days 天站上 daily_ma → 加分
    daily_ma: 预计算的 MA 数组，长度同 dates
    b1_signals: detect_b1_signals 在日线数据上返回的信号列表"""
    n = len(dates)
    n_days = n

    # 构建信号索引
    signal_idxs = {s['week_idx']: s for s in b1_signals}  # 复用week_idx字段名

    # 每日状态: (sig_idx, current_streak, max_streak, expired)
    day_state = [None] * n_days

    for sig_idx in sorted(signal_idxs.keys()):
        streak = 0
        max_streak = 0
        for di in range(sig_idx + 1, min(sig_idx + 1 + expiry_days, n_days)):
            if daily_ma[di] is not None and closes[di] >= daily_ma[di]:
                streak += 1
                if streak > max_streak:
                    max_streak = streak
            else:
                streak = 0

            days_since = di - sig_idx
            expired = (max_streak < confirm_days and days_since >= expiry_days)

            if day_state[di] is not None:
                continue  # 保留最新信号

            day_state[di] = (sig_idx, streak, max_streak, expired)

    result = {}
    for di in range(n_days):
        ws = day_state[di]
        if ws is None:
            result[dates[di]] = 30
        else:
            _, _, max_streak, expired = ws
            if expired:
                result[dates[di]] = 30
            else:
                result[dates[di]] = max(30, streak_to_score_daily(max_streak, confirm_days))

    return result


def compute_b1_daily_for_etf(dates, opens, highs, lows, closes, amounts,
                              confirm_days=15, expiry_days=35, ma_period=25):
    """日线 B1：直接在日线数据上计算 B1 因子
    返回: dict {date_int: factor_score (0-100)}"""
    if len(closes) < 250:
        return {}

    # 直接用日线数据计算指标和信号（不做周线转换）
    indi = calc_b1_indicators((dates, opens, highs, lows, closes, amounts))

    if indi.get('n', 0) < 120:
        return {}

    # 检测 B1 信号
    signals = detect_b1_signals(indi)

    # 计算需要的 MA
    daily_ma = sma_series(closes, ma_period)

    # 计算日线 B1 因子
    factor = calc_b1_daily_factor(dates, closes, daily_ma, signals,
                                  confirm_days=confirm_days, expiry_days=expiry_days)

    return factor


# ======================================================================
# 简化 B1 变体 (BUY_SIGNAL): TJ1+TJ2+TJ3+TJ4
# TJ1: M20>M30>M60   TJ2: M20/M30/M60/M120 全线上升
# TJ3: 量价同步      TJ4: J<22(最近5日内)
# ======================================================================

def detect_buy_signal(dates, opens, highs, lows, closes, amounts, mas):
    """检测 BUY_SIGNAL = TJ1 AND TJ2 AND TJ3 AND TJ4"""
    n = len(closes)
    if n < 130:
        return []

    m20 = mas[20]; m30 = mas[30]; m60 = mas[60]; m120 = mas[120]
    K, D, J = calc_kdj(highs, lows, closes, 9)

    signals = []
    for i in range(120, n):
        if not all(v and v[i] is not None for v in [m20,m30,m60,m120,closes,opens,amounts]):
            continue
        # TJ1
        if not (m20[i] > m30[i] > m60[i]): continue
        # TJ2
        if not (i>=1 and m20[i]>=m20[i-1] and m30[i]>=m30[i-1]
                and m60[i]>=m60[i-1] and m120[i]>=m120[i-1]): continue
        # TJ3: 涨放量 OR 跌缩量
        vu = (closes[i]>opens[i] and amounts[i]>amounts[i-1])
        vd = (closes[i]<opens[i] and amounts[i]<amounts[i-1])
        if not (vu or vd): continue
        # TJ4: J<22 最近5日内
        if not any(J[i-j] is not None and J[i-j]<22 for j in range(5) if i-j>=0): continue

        signals.append({'week_idx': i, 'week_date': dates[i],
                        'J': round(J[i],1) if J[i] else 50, 'close': round(closes[i],3)})
    return signals


def compute_buy_signal_for_etf(dates, opens, highs, lows, closes, amounts,
                                confirm_days=15, expiry_days=35, ma_period=25):
    """计算 BUY_SIGNAL 因子"""
    if len(closes) < 250: return {}
    mas = {p: sma_series(closes, p) for p in [20,30,60,120]}
    signals = detect_buy_signal(dates, opens, highs, lows, closes, amounts, mas)
    daily_ma = sma_series(closes, ma_period)
    factor = calc_b1_daily_factor(dates, closes, daily_ma, signals,
                                  confirm_days=confirm_days, expiry_days=expiry_days)
    return factor


def precompute_buy_signal_factors(etf_data_list, confirm_days=15, expiry_days=35, ma_period=25):
    """批量预计算 BUY_SIGNAL 因子"""
    factors = []
    for i, etf in enumerate(etf_data_list):
        f = compute_buy_signal_for_etf(
            etf['dates'], etf.get('opens',[]), etf.get('highs',[]),
            etf.get('lows',[]), etf['closes'], etf['amounts'],
            confirm_days=confirm_days, expiry_days=expiry_days, ma_period=ma_period)
        factors.append(f)
        if i % 100 == 0:
            sc = sum(1 for v in f.values() if v >= 100)
            st = sum(1 for v in f.values() if v > 30)
            print(f"  BS precompute: {i+1}/{len(etf_data_list)}, peak={sc} signal={st}")
    return factors
    """为所有 ETF 预计算日线 B1 因子"""
    factors = []
    for i, etf in enumerate(etf_data_list):
        f = compute_b1_daily_for_etf(
            etf['dates'], etf.get('opens', []), etf.get('highs', []),
            etf.get('lows', []), etf['closes'], etf['amounts'],
            confirm_days=confirm_days, expiry_days=expiry_days,
            ma_period=ma_period)
        factors.append(f)
        if i % 100 == 0:
            sig_count = sum(1 for v in f.values() if v >= 100)
            sig_total = sum(1 for v in f.values() if v > 30)
            print(f"  B1 daily precompute: {i + 1}/{len(etf_data_list)} ETFs, "
                  f"#{i + 1} has {sig_count} peak days, {sig_total} signal days")
    return factors
