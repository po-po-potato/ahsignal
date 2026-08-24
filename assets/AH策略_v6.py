# -*- coding: utf-8 -*-
"""AH策略 v6 — zadd合成 + 双轨 + 分轨止损 + 轨B独立权重重调（2026-08-15）

相对 v5 的改进（v5 = zadd合成 + 双轨，+41.6%/DD-7.5%/夏普0.60/Calmar0.96）：
  轨B（动量型）独立权重重调：breakout_score 25→30，spread_change 22→15
  效果：+41.6%→+42.6%（+1.0pp），夏普0.60→0.61，Calmar0.96→0.98
  年度分解：+1.0pp 分散在 2025(+0.7)+2026(+0.2)，2024 持平，稳健非单年运气

同时证伪两个方向（保留记录）：
  - 反向因子重构：候选池(通过gate)9个反转信号全Q5正向，无真反向；overheat反向/删除等价，转正向略降
  - 慢牛轨(轨C)：已实现但证伪（负向），默认 track_c_vol_max=0 关闭

用法：gen 脚本 exec 本文件后，evaluate_weights_v5 即为 v6 引擎（scoring_method='zadd'）
"""
import os, sys, time, statistics
from collections import defaultdict, Counter

# 加载 base 引擎（含反转评分的 calc_factors）
_base_code = open('AH策略_engine.py', encoding='utf-8').read()
_base_funcs = _base_code.split("if __name__ == '__main__':")[0]
_OLD = """                recovery_days = idx - low_day
                if recovery_days < 2:
                    pullback_confirm = 50  # 正在回踩中，中性
                elif closes[idx] > closes[low_day]:
                    # 回踩后反弹确认
                    bounce_pct = (closes[idx] / closes[low_day] - 1) * 100
                    penalty = max(0, current_gap - 5) * 2  # 离MA20太远扣分
                    pullback_confirm = 65 + min(bounce_pct * 4, 35) - penalty
                    pullback_confirm = max(50, min(100, pullback_confirm))
                else:
                    pullback_confirm = 45  # 回踩了但没反弹"""
_NEW = """                recovery_days = idx - low_day
                if recovery_days < 2:
                    pullback_confirm = 50  # 正在回踩中，中性
                elif closes[idx] > closes[low_day]:
                    bounce_pct = (closes[idx] / closes[low_day] - 1) * 100
                    penalty = max(0, current_gap - 5) * 2
                    if recovery_days <= 3:
                        pullback_confirm = 95
                    elif bounce_pct <= 8:
                        pullback_confirm = 65
                    else:
                        pullback_confirm = 45
                    pullback_confirm -= penalty
                    pullback_confirm = max(40, min(100, pullback_confirm))
                else:
                    pullback_confirm = 45"""
assert _OLD in _base_funcs
exec(_base_funcs.replace(_OLD, _NEW))

# ==================== v5 增强 ====================
# 1) calc_factors 加 vol20（20日收益率标准差，慢牛轨低波动因子）
_orig_calc_factors_v5 = calc_factors
def calc_factors(idx, closes, amounts, mas, b1_score=None):
    f = _orig_calc_factors_v5(idx, closes, amounts, mas, b1_score=b1_score)
    if idx >= 20:
        rs = [closes[i] / closes[i - 1] - 1 for i in range(idx - 19, idx + 1)]
        f['vol20'] = statistics.pstdev(rs) * 100 if len(rs) >= 2 else 0.0
    else:
        f['vol20'] = 0.0
    return f

# 2) score_etfs 加 zadd 合成 + vol20 反向因子
_V5_FACTOR_KEYS = ('trend', 'mom60', 'mom20', 'liq', 'dist',
                   'spread_change', 'sharpe_eff', 'pullback_confirm', 'overheat',
                   'breakout_score', 'b1_factor', 'vol20')
_V5_ZADD_SD = 15.0

def _v5_ranks_pct(factor_data, pr_lo, pr_hi):
    fr = {}
    fr['trend']     = pct_rank([f['trend'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['dist']      = pct_rank([f['dist'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['mom60']     = pct_rank([f['mom60'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['mom20']     = pct_rank([f['mom20'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['liq']       = pct_rank([f['liq'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['spread_change']  = pct_rank([f['spread_change'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['sharpe_eff']     = pct_rank([f['sharpe_eff'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['pullback_confirm'] = pct_rank([f['pullback_confirm'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['overheat']  = pct_rank([f['overheat'] for f in factor_data], inverse=True, lo=pr_lo, hi=pr_hi)
    fr['breakout_score'] = pct_rank([f['breakout_score'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['b1_factor'] = pct_rank([f['b1_factor'] for f in factor_data], lo=pr_lo, hi=pr_hi)
    fr['vol20']     = pct_rank([f.get('vol20') for f in factor_data], inverse=True, lo=pr_lo, hi=pr_hi)
    return fr

def _v5_ranks_wz(factor_data):
    fr = {}
    fr['trend']     = winsorized_zscore([f['trend'] for f in factor_data])
    fr['dist']      = winsorized_zscore([f['dist'] for f in factor_data])
    fr['mom60']     = winsorized_zscore([f['mom60'] for f in factor_data])
    fr['mom20']     = winsorized_zscore([f['mom20'] for f in factor_data])
    fr['liq']       = winsorized_zscore([f['liq'] for f in factor_data])
    fr['spread_change']  = winsorized_zscore([f['spread_change'] for f in factor_data])
    fr['sharpe_eff']     = winsorized_zscore([f['sharpe_eff'] for f in factor_data])
    fr['pullback_confirm'] = winsorized_zscore([f['pullback_confirm'] for f in factor_data])
    ov = [f['overheat'] for f in factor_data]
    fr['overheat']  = winsorized_zscore([-v if v is not None else None for v in ov])
    fr['breakout_score'] = winsorized_zscore([f['breakout_score'] for f in factor_data])
    fr['b1_factor'] = winsorized_zscore([f['b1_factor'] for f in factor_data])
    vols = [f.get('vol20') for f in factor_data]
    fr['vol20']     = winsorized_zscore([-v if v is not None else None for v in vols])
    return fr

def score_etfs(factor_data, weights, method='winsor_z',
               expected_return_table=None, absolute_score_map=None,
               pr_lo=0, pr_hi=100, score_mult_max=1.4):
    n = len(factor_data)
    if n == 0:
        return []
    w = weights
    wsum = sum(w.get(k, 0) for k in _V5_FACTOR_KEYS) or 1.0

    if method in ('pct_rank', 'add', 'zadd'):
        fr = _v5_ranks_pct(factor_data, pr_lo, pr_hi)
    else:
        fr = _v5_ranks_wz(factor_data)

    if method == 'zadd':
        composites = []
        for i in range(n):
            total = 0.0
            for fn in _V5_FACTOR_KEYS:
                w_norm = w.get(fn, 0) / wsum
                if w_norm < 0.001:
                    continue
                total += fr[fn][i] * w_norm
            composites.append(total)
        mu = sum(composites) / n
        sd = (sum((x - mu) ** 2 for x in composites) / n) ** 0.5
        if sd > 1e-9:
            scores = [max(0, min(100, 50 + (x - mu) / sd * _V5_ZADD_SD)) for x in composites]
        else:
            scores = [50.0] * n
        return [round(s, 1) for s in scores]

    if method == 'add':
        composites = []
        for i in range(n):
            total = 0.0
            for fn in _V5_FACTOR_KEYS:
                w_norm = w.get(fn, 0) / wsum
                if w_norm < 0.001:
                    continue
                total += fr[fn][i] * w_norm
            composites.append(total)
        return [round(c, 1) for c in composites]

    composites = []
    for i in range(n):
        composite = 1.0
        for fn in _V5_FACTOR_KEYS:
            score_0_100 = fr[fn][i]
            w_norm = w.get(fn, 0) / wsum
            if w_norm < 0.001:
                continue
            m = 0.6 + (score_mult_max - 0.6) * score_0_100 / 100.0
            composite *= m ** w_norm
        composites.append(composite)
    return [max(0, min(100, round((c - 1.0) * 100 + 50, 1))) for c in composites]



def evaluate_weights_v5(etf_data_list, weights, gate=True, max_hold=10, daily_buy_max=3,
                        max_gap=1.08, sell_confirm_days=2, sell_gap_pct=5, above_ma20_min=7,
                        min_hold_days=0, buy_cooldown=0, adx_min=0, eff_min=0.2,
                        m20ratio=1.02, vol_ratio_lo=0.6, vol_ratio_hi=2.5, rally_lo=0,
                        rally_hi=999, scoring_method='winsor_z', use_holding_layer=False,
                        expected_return_table=None, absolute_score_map=None,
                        buy_min_score=0, snapshot_callback=None,
                        ma20_tolerance_pct=0, breakeven_trigger_pct=0, max_cumul_gain_pct=0,
                        use_oamv_gate=False, use_ad_state=False,
                        pr_lo=0, pr_hi=100, score_mult_max=1.4,
                        vol_break_high=1.5, vol_break_low=0.7,
                        vol_high_confirm_adj=2, vol_low_confirm_adj=2,
                        apply_fee=True, stop_loss_pct=-10,
                        trend_use_ma30=False, pullback_ma30=False,
                        gate_ma_n=0, crossover_gap=0, bounce_cap_pct=0,
                        crash_day_pct=0, crash_liquidate_type='',
                        bull_buy_mult=1.0, bull_score_threshold=65, bull_min_candidates=5,
                        ad_defense_dn=-2.3, ad_ma_consec=0, ad_ma_period=10, ad_attack_up=4.0,
                        score_relax_threshold=0, dual_track=False, track_b_weights=None,
                        bull_top_avg=0, hitech_above_min=0, hitech_gap=0,
                        b1_gate=False, b1_gate_threshold=80, b1_fast_exit=3,
                        b1_bonus=0, b1_bonus_threshold=78,
                        # 新功能
                        add_on_hscore_days=0, profit_trail_tiers=None,
                        consecutive_bonus=0, consecutive_top_n=0,
                        consecutive_max_days=0, consecutive_decay=None,
                        track_b_stop_loss=None, track_b_breakeven=None,
                        track_b_profit_trail=None, track_b_sell_gap=None,
                        track_b_sell_confirm=None,
                        track_a_stop_loss=None, track_a_breakeven=None,
                        track_a_profit_trail=None, track_a_sell_gap=None,
                        track_a_sell_confirm=None,
                        track_a_weights=None,
                        track_c_weights=None, track_c_vol_max=0, track_c_score_adj=8,
                        track_b_score_adj=8, disable_track_a=False, track_a_score_adj=0,
                        track_a_max_hold=0):
    """重构版策略引擎。

    架构改进：
    - positions 用 list[{ei,pid,...}] 替代 dict，天然支持同ETF多仓位
    - 卖出规则全部用 if not sell 守卫，保证每天只触发一条
    - 加仓和主仓位同循环处理，无差异化逻辑

    补全原版边界：
    - expected_return_table / absolute_score_map 传参
    - B轨/track_a_codes 已持仓检查
    - B1 bonus isinstance 安全检查
    """
    global _TREND_USE_MA30, _PULLBACK_MA30, _BOUNCE_CAP_PCT
    _TREND_USE_MA30 = trend_use_ma30
    _PULLBACK_MA30 = pullback_ma30
    _BOUNCE_CAP_PCT = bounce_cap_pct

    _HITECH_KW = ('半导体', '芯片', '科创')
    def _is_hitech(track):
        return bool(track) and any(k in track for k in _HITECH_KW)

    cooldown_days = 10
    BASE_JUMP = 75
    BASE_JUMP_HS_MAX = 55
    BASE_MAX_HOLD = max_hold
    jump_attempts = 0
    jump_success = 0

    _, regime_map = get_oamv_regime()
    regime_stats = Counter()
    USE_OAMV_GATE = use_oamv_gate
    USE_AD_STATE = use_ad_state

    prev_ad = None
    if USE_AD_STATE:
        ad_state = get_ad_state(up_days=2, defense_dn=ad_defense_dn,
                                oamv_up=ad_attack_up,
                                ma_consec_days=ad_ma_consec, ma_period=ad_ma_period)
    else:
        ad_state = {}

    trades = []
    daily_log = []

    # ─── positions: list of dict ───
    positions = []
    _pid_counter = [0]
    cooldown_map = {}
    consecutive_tracker = {}
    prev_day_candidates = set()

    def _total_slots():
        return len(positions)

    def _ei_in_positions(ei):
        return any(p['ei'] == ei for p in positions)

    def _ei_pos_count(ei):
        return sum(1 for p in positions if p['ei'] == ei)

    # date -> idx 映射
    date_to_idx = []
    for etf in etf_data_list:
        d2i = {}
        for i, d in enumerate(etf['dates']):
            d2i[d] = i
        date_to_idx.append(d2i)

    for etf in etf_data_list:
        etf['_amount_ma20'] = sma(etf['amounts'], 20)

    # 交易日
    date_counts = Counter()
    for etf in etf_data_list:
        for d in etf['dates']:
            if BACKTEST_START <= d <= BACKTEST_END:
                date_counts[d] += 1
    trading_days = sorted(d for d, c in date_counts.items()
                          if c >= 100 and BACKTEST_START <= d <= BACKTEST_END)

    last_buy_di = -99999

    # 崩盘检测
    _market_ret = {}
    if crash_day_pct < 0:
        _all_rets = {}
        for etf in etf_data_list:
            for i in range(1, len(etf['dates'])):
                if etf['closes'][i-1] > 0:
                    d = etf['dates'][i]
                    _all_rets.setdefault(d, []).append(
                        (etf['closes'][i]/etf['closes'][i-1]-1)*100)
        for d in _all_rets:
            if len(_all_rets[d]) >= 10:
                _market_ret[d] = statistics.median(_all_rets[d])
    _prev_mr = None

    # ─── 每日循环 ───
    for di, date_int in enumerate(trading_days):
        regime = regime_map.get(date_int, 'transitional')
        rp = get_regime_params(regime)
        effective_max_hold = max_hold
        effective_jump = rp['jump_threshold']
        effective_buy_max = daily_buy_max
        regime_stats[regime] += 1

        if USE_AD_STATE:
            prev_ad = ad_state.get(date_int, 'attack')

        idx_map = {}
        for ei in range(len(etf_data_list)):
            idx = date_to_idx[ei].get(date_int)
            if idx is not None and idx >= 60:
                idx_map[ei] = idx
        if len(idx_map) < 20:
            continue

        # ═══ 卖出检查 ═══
        to_close_pids = []
        for pos in positions[:]:
            ei = pos['ei']
            etf = etf_data_list[ei]
            idx = idx_map.get(ei)
            if idx is None:
                continue

            buy_idx = pos['orig_buy_idx']
            entry_price = pos['entry_price']
            max_p = pos['max_price']

            # ─── 分轨止盈止损：快牛(fast)可独立覆盖 ───
            _ts = pos.get('_track', 'slow')
            _eff_stop = stop_loss_pct
            _eff_bev = breakeven_trigger_pct
            _eff_trail = profit_trail_tiers
            _eff_sgap = sell_gap_pct
            _eff_sconf = sell_confirm_days
            if _ts == 'fast':
                if track_b_stop_loss is not None: _eff_stop = track_b_stop_loss
                if track_b_breakeven is not None: _eff_bev = track_b_breakeven
                if track_b_profit_trail is not None: _eff_trail = track_b_profit_trail
                if track_b_sell_gap is not None: _eff_sgap = track_b_sell_gap
                if track_b_sell_confirm is not None: _eff_sconf = track_b_sell_confirm
            else:
                if track_a_stop_loss is not None: _eff_stop = track_a_stop_loss
                if track_a_breakeven is not None: _eff_bev = track_a_breakeven
                if track_a_profit_trail is not None: _eff_trail = track_a_profit_trail
                if track_a_sell_gap is not None: _eff_sgap = track_a_sell_gap
                if track_a_sell_confirm is not None: _eff_sconf = track_a_sell_confirm

            for di_check in range(pos['check_from'], idx + 1):
                close = etf['closes'][di_check]
                high = etf['highs'][di_check]
                if high > max_p:
                    max_p = high
                pos['max_price'] = max_p

                ma20 = etf['mas'][20][di_check] if di_check < len(etf['mas'][20]) else None
                loss_pct = (close / entry_price - 1) * 100
                if loss_pct > pos.get('max_profit_pct', 0):
                    pos['max_profit_pct'] = loss_pct
                below_ma20 = ma20 is not None and close < ma20 * (1 - ma20_tolerance_pct/100)
                below_ma20_deep = ma20 is not None and close < ma20 * (1 - _eff_sgap/100)

                # MA20 连续跌破 + 量能感知
                if below_ma20:
                    pos['below_ma20_days'] = pos.get('below_ma20_days', 0) + 1
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
                in_min_hold = (date_int - pos['buy_date']) < min_hold_days

                # 1. 硬止损（仅普通仓位；部分止盈仓位用移动止盈）
                if not pos.get('partial_exit_done'):
                    effective_stop = _eff_stop
                    max_profit_pct = pos.get('max_profit_pct', 0)
                    if _eff_bev > 0 and max_profit_pct >= _eff_bev:
                        effective_stop = 0
                    if loss_pct <= effective_stop:
                        sell = True
                        reason = f'止损+{_eff_bev}%保本' if effective_stop == 0 else f'止损{effective_stop}%'

                # 2. 利润分段止盈
                if not sell and _eff_trail and not in_min_hold:
                    max_profit = pos.get('max_profit_pct', 0)
                    for tier_profit, tier_trail in sorted(_eff_trail, reverse=True):
                        if max_profit >= tier_profit:
                            peak_p = pos['max_price']
                            dd_from_peak = (close / peak_p - 1) * 100
                            if dd_from_peak <= -tier_trail:
                                sell = True
                                reason = f'利润保护{tier_profit}%→回撤{tier_trail}%(峰{peak_p/entry_price:.2f}x)'
                            break

                # 3. MA20 深破
                if not sell and below_ma20_deep and not in_min_hold:
                    sell = True
                    reason = f'跌破MA20-{_eff_sgap}%'

                # 4. MA20 连续跌破（含量能感知 + 快速退出）
                effective_confirm = _eff_sconf
                if pos['below_ma20_days'] >= 1:
                    if di_check - buy_idx <= b1_fast_exit:
                        effective_confirm = 1
                        pos['_fast_exit'] = True
                vbt = pos.get('_vol_break_type', 'normal')
                if vbt == 'high':
                    effective_confirm = max(1, _eff_sconf - vol_high_confirm_adj)
                elif vbt == 'low':
                    effective_confirm = _eff_sconf + vol_low_confirm_adj
                if not sell and pos['below_ma20_days'] >= effective_confirm and not in_min_hold:
                    sell = True
                    vt_label = '放量' if vbt == 'high' else ('缩量' if vbt == 'low' else '')
                    reason = f'跌破MA20({vt_label}连续{effective_confirm}日)'

                # 5. 持有分低（双轨阈值）
                if not sell and use_holding_layer and not in_min_hold:
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
                        'etf_idx': ei, 'code': etf['code'], 'name': etf['name'],
                        'track': etf.get('track', ''),
                        '_track': pos.get('_track', 'slow'),
                        'buy_date': pos['buy_date'],
                        'sell_date': etf['dates'][di_check],
                        'entry': round(entry_price, 3), 'exit': round(close, 3),
                        'ret_pct': round(ret_pct, 2), 'max_pct': round(max_pct, 2),
                        'hold_days': hold_days, 'reason': reason,
                        'score': pos['score'], 'regime': pos.get('regime', '?'),
                    })
                    to_close_pids.append(pos['pid'])
                    cooldown_map[ei] = di + cooldown_days
                    break

            if pos['pid'] not in to_close_pids:
                pos['check_from'] = idx + 1

        positions = [p for p in positions if p['pid'] not in to_close_pids]

        # ═══ 收集候选 ═══
        gate_failures = {}
        factor_data = []
        for ei, idx in idx_map.items():
            if disable_track_a:
                break
            cool_end = cooldown_map.get(ei, -1)
            if di <= cool_end:
                gate_failures[etf_data_list[ei]['code']] = f'冷却{cool_end - di + 1}日'
                continue
            if _ei_in_positions(ei):
                gate_failures[etf_data_list[ei]['code']] = '已持仓'
                continue

            etf = etf_data_list[ei]
            closes = etf['closes']
            mas = etf['mas']

            if gate:
                c = closes[idx]; m20 = mas[20][idx]; m60 = mas[60][idx]
                if m20 is None or m60 is None or c <= m60:
                    gate_failures[etf['code']] = 'C<=MA60'
                    continue
                if m20 / m60 < m20ratio:
                    gate_failures[etf['code']] = f'M20/M60过低({m20/m60*100:.0f}%)'
                    continue
                if gate_ma_n > 0:
                    if gate_ma_n in mas and mas[gate_ma_n][idx] is not None and c <= mas[gate_ma_n][idx]:
                        gate_failures[etf['code']] = f'C<=MA{gate_ma_n}'
                        continue
                if m20 < mas[20][idx - 1]:
                    gate_failures[etf['code']] = 'MA20↓'
                    continue
                _eff_am = above_ma20_min
                if hitech_above_min > 0 and hitech_above_min < _eff_am:
                    if _is_hitech(etf.get('track', '')):
                        _eff_am = hitech_above_min
                above_days = 0; dck = idx
                while dck >= 0 and mas[20][dck] is not None and closes[dck] >= mas[20][dck]:
                    above_days += 1; dck -= 1
                if above_days < _eff_am:
                    gate_failures[etf['code']] = f'站上MA20仅{above_days}日(需≥{_eff_am})'
                    continue

            # 量比/ADX/涨幅过滤
            if eff_min > 0 and etf.get('trend_eff') and idx < len(etf['trend_eff']):
                if etf['trend_eff'][idx] is not None and etf['trend_eff'][idx] < eff_min:
                    continue
            if vol_ratio_lo > 0 and etf.get('vol_ratios') and idx < len(etf['vol_ratios']):
                vr = etf['vol_ratios'][idx]
                if vr is not None and (vr < vol_ratio_lo or vr > vol_ratio_hi):
                    continue
            if adx_min > 0 and etf.get('adx') and idx < len(etf['adx']):
                if etf['adx'][idx] is not None and etf['adx'][idx] < adx_min:
                    continue
            if rally_lo > 0 or rally_hi < 999:
                lb = min(60, idx); low_idx = idx - lb
                for j in range(idx - lb, idx):
                    if closes[j] < closes[low_idx]: low_idx = j
                rally_pct = (closes[idx] / closes[low_idx] - 1) * 100 if closes[low_idx] > 0 else 0
                if rally_pct < rally_lo or rally_pct > rally_hi:
                    continue
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
            if b1_gate and b1_val < b1_gate_threshold:
                gate_failures[etf['code']] = f'B1<{b1_gate_threshold}'
                continue

            f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val)
            if f['ma20'] is None or f['close'] <= f['ma20']:
                gate_failures[etf['code']] = 'C<=MA20'
                continue
            gap = f['close'] / f['ma20']
            _effective_max_gap = max_gap
            if crossover_gap > 0 and f['ma60']:
                if f['ma20'] < f['ma60']:
                    for n in range(1, 4):
                        old_sum = sum(closes[idx-19:idx-19+n])
                        ma20_proj = f['ma20'] + (n * closes[idx] - old_sum) / 20
                        if ma20_proj >= f['ma60']:
                            _effective_max_gap = max(max_gap, crossover_gap)
                            break
                elif f['ma20'] >= f['ma60']:
                    for lb in range(1, 6):
                        prev_idx = idx - lb
                        if prev_idx >= 0 and mas[20][prev_idx] and mas[60][prev_idx]:
                            if mas[20][prev_idx] < mas[60][prev_idx]:
                                _effective_max_gap = max(max_gap, crossover_gap)
                                break
            if hitech_gap > 0 and _is_hitech(etf.get('track', '')):
                _effective_max_gap = max(_effective_max_gap, hitech_gap)
            if gap > _effective_max_gap:
                gate_failures[etf['code']] = f'乖离{gap*100-100:.1f}%>{_effective_max_gap*100-100:.0f}%'
                continue

            if dual_track:
                factor_data.append((ei, f, 'A'))
            else:
                factor_data.append((ei, f))

        # ═══ 双轨制 ═══
        if dual_track and gate:
            factor_data_b = []
            if track_b_weights is None:
                track_b_weights = dict(weights)
                for k in ['breakout_score', 'spread_change', 'trend', 'sharpe_eff', 'pullback_confirm']:
                    pass  # weights already defined by caller
                track_b_weights['breakout_score'] = 30
                track_b_weights['spread_change'] = 15
                track_b_weights['trend'] = 15
                track_b_weights['sharpe_eff'] = 15
                track_b_weights['pullback_confirm'] = 10
                track_b_weights['overheat'] = 1

            track_a_codes = {etf_data_list[ei]['code']: ei for ei, idx in idx_map.items()
                           if not _ei_in_positions(ei)
                           and di > cooldown_map.get(ei, -1)
                           and etf_data_list[ei]['code'] in
                           {etf_data_list[fd[0]]['code'] for fd in factor_data}}

            for ei, idx in idx_map.items():
                cool_end = cooldown_map.get(ei, -1)
                if di <= cool_end:
                    continue
                if _ei_in_positions(ei):
                    continue  # 已在持仓
                etf = etf_data_list[ei]
                if etf['code'] in track_a_codes:
                    continue
                closes = etf['closes']; mas = etf['mas']
                if mas[20][idx] is None or mas[60][idx] is None or closes[idx] <= mas[60][idx]:
                    continue
                if mas[20][idx] < mas[60][idx] * m20ratio:
                    continue
                if gate_ma_n > 0 and gate_ma_n in mas and mas[gate_ma_n][idx] is not None and closes[idx] <= mas[gate_ma_n][idx]:
                    continue
                if mas[20][idx] < mas[20][idx-1]:
                    continue
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
                if f['close'] / f['ma20'] > 1.12:
                    continue
                if f['breakout_score'] < 75:
                    continue
                factor_data_b.append((ei, f, 'B'))

            # ═══ 轨C：慢牛型（低波动+趋势向上+非回踩非动量）═══
            if track_c_weights is None:
                track_c_weights = {'trend': 50, 'sharpe_eff': 30, 'vol20': 20}
            factor_data_c = []
            if track_c_vol_max > 0:
                _c_existing = {etf_data_list[fd[0]]['code'] for fd in factor_data}
                _c_existing |= {etf_data_list[fd[0]]['code'] for fd in factor_data_b}
                for ei, idx in idx_map.items():
                    cool_end = cooldown_map.get(ei, -1)
                    if di <= cool_end:
                        continue
                    if _ei_in_positions(ei):
                        continue
                    etf = etf_data_list[ei]
                    if etf['code'] in _c_existing:
                        continue
                    closes = etf['closes']; mas = etf['mas']
                    if mas[20][idx] is None or mas[60][idx] is None or closes[idx] <= mas[60][idx]:
                        continue
                    if mas[20][idx] < mas[60][idx] * m20ratio:
                        continue
                    if mas[20][idx] < mas[20][idx - 1]:
                        continue
                    abd = 0; dck = idx
                    while dck >= 0 and mas[20][dck] is not None and closes[dck] >= mas[20][dck]:
                        abd += 1; dck -= 1
                    if abd < 7:
                        continue
                    b1_map_c = etf.get('b1_factor', {})
                    b1_val_c = b1_map_c.get(date_int, 50.0)
                    f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val_c)
                    if f['ma20'] is None or f['close'] <= f['ma20']:
                        continue
                    if f.get('vol20') is None or f['vol20'] >= track_c_vol_max:
                        continue
                    if f['mom60'] is None or f['mom60'] <= 0:
                        continue
                    if f['pullback_confirm'] >= 65:
                        continue
                    if f['spread_change'] >= 1.0 or f['breakout_score'] >= 75:
                        continue
                    factor_data_c.append((ei, f, 'C'))

            if factor_data_b or factor_data_c:
                scores_a = score_etfs([fd[1] for fd in factor_data], track_a_weights or weights,
                                      method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi,
                                      score_mult_max=score_mult_max,
                                      expected_return_table=expected_return_table,
                                      absolute_score_map=absolute_score_map)
                scores_b = score_etfs([fd[1] for fd in factor_data_b], track_b_weights,
                                      method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi,
                                      score_mult_max=score_mult_max,
                                      expected_return_table=expected_return_table,
                                      absolute_score_map=absolute_score_map) if factor_data_b else []
                scores_c = score_etfs([fd[1] for fd in factor_data_c], track_c_weights,
                                      method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi,
                                      score_mult_max=score_mult_max,
                                      expected_return_table=expected_return_table,
                                      absolute_score_map=absolute_score_map) if factor_data_c else []
                new_factor_data = []
                new_scores = []
                for i, (ei, f, _) in enumerate(factor_data):
                    new_factor_data.append((ei, f, 'A'))
                    new_scores.append(max(0, scores_a[i] - track_a_score_adj))
                for i, (ei, f, _) in enumerate(factor_data_b):
                    new_factor_data.append((ei, f, 'B'))
                    new_scores.append(max(0, scores_b[i] - track_b_score_adj))
                for i, (ei, f, _) in enumerate(factor_data_c):
                    new_factor_data.append((ei, f, 'C'))
                    new_scores.append(max(0, scores_c[i] - track_c_score_adj))
                factor_data = new_factor_data
                _scored_dual_track = True
                _dual_scores = new_scores
            else:
                factor_data = [(ei, f, 'A') for ei, f, _ in factor_data]
                _scored_dual_track = False
        else:
            _scored_dual_track = False

        if not factor_data:
            if snapshot_callback is not None:
                _emit_snapshot(snapshot_callback, date_int, positions, etf_data_list, idx_map, [], gate_failures)
            continue

        if not _scored_dual_track:
            scores = score_etfs([fd[1] for fd in factor_data], weights,
                                method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi,
                                score_mult_max=score_mult_max,
                                expected_return_table=expected_return_table,
                                absolute_score_map=absolute_score_map)
        else:
            scores = _dual_scores

        # B1 bonus
        if b1_bonus > 0 and b1_bonus_threshold > 0:
            for fi in range(len(factor_data)):
                fd_ei = factor_data[fi][0] if isinstance(factor_data[fi], (list, tuple)) else factor_data[fi]['ei']
                b1_val = etf_data_list[fd_ei].get('b1_factor', {}).get(date_int, 30)
                if b1_val >= b1_bonus_threshold:
                    scores[fi] += b1_bonus
        ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)

        # ═══ 高分放宽 Gate ═══
        if score_relax_threshold > 0 and gate:
            relax_factor_data = []
            for ei, idx in idx_map.items():
                cool_end = cooldown_map.get(ei, -1)
                if di <= cool_end:
                    continue
                if _ei_in_positions(ei):
                    continue
                if ei in {fd[0] for fd in factor_data}:
                    continue
                etf = etf_data_list[ei]
                closes = etf['closes']; mas = etf['mas']
                if mas[20][idx] is None or mas[60][idx] is None or closes[idx] <= mas[60][idx]:
                    continue
                if mas[20][idx] < mas[60][idx] * m20ratio:
                    continue
                if gate_ma_n > 0 and gate_ma_n in mas and mas[gate_ma_n][idx] is not None and closes[idx] <= mas[gate_ma_n][idx]:
                    continue
                if mas[20][idx] < mas[20][idx-1]:
                    continue
                abd = 0; dck = idx
                while dck >= 0 and mas[20][dck] is not None and closes[dck] >= mas[20][dck]:
                    abd += 1; dck -= 1
                if abd < 3:
                    continue
                b1_map_c = etf_data_list[ei].get('b1_factor', {})
                b1_val_c = b1_map_c.get(date_int, 50.0)
                f = calc_factors(idx, closes, etf['amounts'], mas, b1_score=b1_val_c)
                if f['ma20'] is None or f['close'] <= f['ma20']:
                    continue
                if f['close'] / f['ma20'] > 1.12:
                    continue
                relax_factor_data.append((ei, f))

            if relax_factor_data:
                full_factor_data = factor_data + relax_factor_data
                full_scores = score_etfs([f for _, f in full_factor_data], weights,
                                         method=scoring_method, pr_lo=pr_lo, pr_hi=pr_hi,
                                         score_mult_max=score_mult_max,
                                         expected_return_table=expected_return_table,
                                         absolute_score_map=absolute_score_map)
                new_factor_data = []; new_scores = []
                for i, (ei_rd, f_rd) in enumerate(full_factor_data):
                    sc = full_scores[i]
                    if i < len(factor_data):
                        new_factor_data.append((ei_rd, f_rd))
                        new_scores.append(sc)
                    elif sc >= score_relax_threshold:
                        new_factor_data.append((ei_rd, f_rd))
                        new_scores.append(sc)
                factor_data = new_factor_data
                scores = new_scores
                ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)

        # ═══ 连续上榜奖励 ═══
        if consecutive_bonus > 0:
            _top_n = consecutive_top_n if consecutive_top_n > 0 else len(ranked)
            _top_eis = set()
            for _ri, _sc in ranked[:_top_n]:
                _fd = factor_data[_ri]
                _top_eis.add(_fd[0] if isinstance(_fd, (list, tuple)) else _fd['ei'])
            for _ei in _top_eis:
                if _ei in prev_day_candidates:
                    consecutive_tracker[_ei] = consecutive_tracker.get(_ei, 0) + 1
                else:
                    consecutive_tracker[_ei] = 1
            for _ei in prev_day_candidates - _top_eis:
                consecutive_tracker.pop(_ei, None)
            prev_day_candidates = _top_eis
            for _i in range(len(factor_data)):
                _fd = factor_data[_i]
                _ei = _fd[0] if isinstance(_fd, (list, tuple)) else _fd['ei']
                _cd = consecutive_tracker.get(_ei, 1)
                if _cd <= 1:
                    continue
                _peak = consecutive_max_days if consecutive_max_days > 0 else (_cd - 1)
                if consecutive_decay is not None and consecutive_decay > 0:
                    if (_cd - 1) <= _peak:
                        _bonus = (_cd - 1) * consecutive_bonus
                    else:
                        _bonus = _peak * consecutive_bonus * (consecutive_decay ** (_cd - 1 - _peak))
                else:
                    _bonus = min(_cd - 1, _peak) * consecutive_bonus if _peak > 0 else (_cd - 1) * consecutive_bonus
                scores[_i] = scores[_i] * (1 + _bonus)
            ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)

        # ═══ 持仓持有评分 ═══
        if use_holding_layer:
            for pos in positions:
                etf_p = etf_data_list[pos['ei']]
                pidx = idx_map.get(pos['ei'])
                if pidx is None:
                    continue
                h_score, _ = calc_holding_score(etf_p, pos['orig_buy_idx'], pidx, pos['entry_price'])
                pos['holding_score'] = h_score
                # 持有分历史（加仓触发用）
                if add_on_hscore_days > 0:
                    if '_hs_hist' not in pos:
                        pos['_hs_hist'] = []
                    pos['_hs_hist'].append(h_score)
                    if len(pos['_hs_hist']) > add_on_hscore_days + 1:
                        pos['_hs_hist'] = pos['_hs_hist'][-(add_on_hscore_days + 1):]

            pos_scores = [(p['pid'], p.get('holding_score', 50), p['score'], {}, p.get('_track', 'slow'), p)
                          for p in positions]
            pos_scores.sort(key=lambda x: x[1])
        else:
            pos_scores = [(p['pid'], p['score'], p['score'], {}, 'slow', p) for p in positions]
            pos_scores.sort(key=lambda x: x[1])

        # ═══ 崩盘检测 ═══
        _mr_today = _market_ret.get(date_int) if crash_day_pct < 0 else None
        _is_crash = _mr_today is not None and _mr_today <= crash_day_pct
        _prev_crash = _prev_mr is not None and crash_day_pct < 0 and _prev_mr <= crash_day_pct

        if _is_crash and crash_liquidate_type == 'consecutive' and _prev_crash:
            for pos in list(positions):
                ei = pos['ei']; etf = etf_data_list[ei]
                idx = idx_map.get(ei)
                if idx is not None:
                    close = etf['closes'][idx]
                    ret_pct = (close / pos['entry_price'] - 1) * 100
                    max_pct = (pos['max_price'] / pos['entry_price'] - 1) * 100
                    hold_days = idx - pos['orig_buy_idx']
                    trades.append({
                        'etf_idx': ei, 'code': etf['code'], 'name': etf['name'],
                        '_track': pos.get('_track', 'slow'),
                        'buy_date': pos['buy_date'], 'sell_date': date_int,
                        'entry': round(pos['entry_price'], 3), 'exit': round(close, 3),
                        'ret_pct': round(ret_pct, 2), 'max_pct': round(max_pct, 2),
                        'hold_days': hold_days, 'reason': '崩盘清仓(连续-2.3%)',
                        'score': pos['score'], 'regime': pos.get('regime', '?'),
                    })
                    cooldown_map[ei] = di + cooldown_days
            positions.clear()

        elif _is_crash and crash_liquidate_type == 'cumulative' and _prev_mr is not None and _mr_today + _prev_mr <= crash_day_pct * 2:
            for pos in list(positions):
                ei = pos['ei']; etf = etf_data_list[ei]
                idx = idx_map.get(ei)
                if idx is not None:
                    close = etf['closes'][idx]
                    ret_pct = (close / pos['entry_price'] - 1) * 100
                    max_pct = (pos['max_price'] / pos['entry_price'] - 1) * 100
                    hold_days = idx - pos['orig_buy_idx']
                    trades.append({
                        'etf_idx': ei, 'code': etf['code'], 'name': etf['name'],
                        '_track': pos.get('_track', 'slow'),
                        'buy_date': pos['buy_date'], 'sell_date': date_int,
                        'entry': round(pos['entry_price'], 3), 'exit': round(close, 3),
                        'ret_pct': round(ret_pct, 2), 'max_pct': round(max_pct, 2),
                        'hold_days': hold_days,
                        'reason': f'崩盘清仓(累计{_mr_today+_prev_mr:.1f}%)',
                        'score': pos['score'], 'regime': pos.get('regime', '?'),
                    })
                    cooldown_map[ei] = di + cooldown_days
            positions.clear()

        # ═══ 买入 ═══
        bought = 0
        total_slots = _total_slots()

        # 牛市加仓
        if bull_buy_mult > 1.0:
            top_scores = sorted([s for _, s in ranked], reverse=True)[:10]
            top_avg = sum(top_scores) / len(top_scores) if top_scores else 0
            if bull_top_avg <= 0 or top_avg >= bull_top_avg:
                effective_buy_max = min(effective_max_hold, int(daily_buy_max * bull_buy_mult))
                daily_log.append((date_int, 'bull_on', top_avg, len(positions)))

        for rank_i, (fi, score) in enumerate(ranked):
            if USE_OAMV_GATE and regime == 'peaking':
                break
            if USE_AD_STATE and prev_ad == 'defense':
                break
            if _is_crash:
                break
            if bought >= effective_buy_max:
                break
            if buy_cooldown > 0 and di != last_buy_di and di - last_buy_di < buy_cooldown:
                break

            fd_item = factor_data[fi]
            ei, f = fd_item[0], fd_item[1]
            idx = idx_map[ei]
            _track_src = fd_item[2] if isinstance(fd_item, (list, tuple)) and len(fd_item) > 2 else 'A'
            if track_a_max_hold > 0 and _track_src == 'A':
                _a_cnt = sum(1 for p in positions if p.get('_src') == 'A')
                if _a_cnt >= track_a_max_hold:
                    continue
            if buy_min_score > 0 and score < buy_min_score:
                continue

            available_slots = effective_max_hold - total_slots

            if available_slots <= 0:
                if not pos_scores:
                    break
                worst_pid, worst_hscore, worst_buy_score, h_detail, worst_track, worst_pos = pos_scores[0]

                # 插队（修复：新候选分数必须高于被换持仓的买入分，避免"低分换高分"）
                if score >= effective_jump and worst_hscore < BASE_JUMP_HS_MAX and score > worst_pos['score']:
                    jump_attempts += 1
                    etf_worst = etf_data_list[worst_pos['ei']]
                    sell_idx = idx_map.get(worst_pos['ei'])
                    if sell_idx is not None:
                        sell_close = etf_worst['closes'][sell_idx]
                        ret_pct = (sell_close / worst_pos['entry_price'] - 1) * 100
                        max_pct = (worst_pos['max_price'] / worst_pos['entry_price'] - 1) * 100
                        hold_days = sell_idx - worst_pos['orig_buy_idx']
                        trades.append({
                            'etf_idx': worst_pos['ei'], 'code': etf_worst['code'],
                            'name': etf_worst['name'], 'track': etf_worst.get('track', ''),
                            '_track': worst_pos.get('_track', 'slow'),
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
                        cooldown_map[worst_pos['ei']] = di + cooldown_days
                        positions = [p for p in positions if p['pid'] != worst_pid]
                        total_slots = _total_slots()
                        jump_success += 1

                        _pid_counter[0] += 1
                        style = ('slow' if (isinstance(factor_data[fi], (list, tuple)) and len(factor_data[fi]) > 2 and factor_data[fi][2] == 'C') else classify_track(f))
                        positions.append({
                            'ei': ei, 'pid': _pid_counter[0],
                            'buy_date': date_int, 'orig_buy_idx': idx,
                            'check_from': idx + 1, 'entry_price': f['close'],
                            'max_price': f['close'], 'score': round(score, 1),
                            'below_ma20_days': 0, 'regime': regime, '_track': style, '_src': _track_src,
                            'holding_score': 60, 'max_profit_pct': 0,
                        })
                        bought += 1; last_buy_di = di; total_slots = _total_slots()
                        pos_scores = [(p['pid'], p.get('holding_score', 50), p['score'], {}, p.get('_track', 'slow'), p)
                                      for p in positions]
                        pos_scores.sort(key=lambda x: x[1])

                # 常规替换
                elif buy_cooldown <= 0 or di - last_buy_di >= buy_cooldown:
                    if use_holding_layer:
                        if worst_hscore <= 30: dyn_threshold = 1.0
                        elif worst_hscore <= 50: dyn_threshold = 2.0
                        else: dyn_threshold = 4.0
                    else:
                        dyn_threshold = 3.0

                    if score >= worst_hscore + dyn_threshold:
                        etf_worst = etf_data_list[worst_pos['ei']]
                        sell_idx = idx_map.get(worst_pos['ei'])
                        if sell_idx is not None:
                            sell_close = etf_worst['closes'][sell_idx]
                            ret_pct = (sell_close / worst_pos['entry_price'] - 1) * 100
                            max_pct = (worst_pos['max_price'] / worst_pos['entry_price'] - 1) * 100
                            hold_days = sell_idx - worst_pos['orig_buy_idx']
                            trades.append({
                                'etf_idx': worst_pos['ei'], 'code': etf_worst['code'],
                                'name': etf_worst['name'], 'track': etf_worst.get('track', ''),
                                '_track': worst_pos.get('_track', 'slow'),
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
                            cooldown_map[worst_pos['ei']] = di + cooldown_days
                            positions = [p for p in positions if p['pid'] != worst_pid]
                            total_slots = _total_slots()

                            _pid_counter[0] += 1
                            style = ('slow' if (isinstance(factor_data[fi], (list, tuple)) and len(factor_data[fi]) > 2 and factor_data[fi][2] == 'C') else classify_track(f))
                            positions.append({
                                'ei': ei, 'pid': _pid_counter[0],
                                'buy_date': date_int, 'orig_buy_idx': idx,
                                'check_from': idx + 1, 'entry_price': f['close'],
                                'max_price': f['close'], 'score': round(score, 1),
                                'below_ma20_days': 0, 'regime': regime, '_track': style, '_src': _track_src,
                                'holding_score': 60, 'max_profit_pct': 0,
                            })
                            bought += 1; last_buy_di = di; total_slots = _total_slots()
                            pos_scores = [(p['pid'], p.get('holding_score', 50), p['score'], {}, p.get('_track', 'slow'), p)
                                          for p in positions]
                            pos_scores.sort(key=lambda x: x[1])
            else:
                # 正常买入
                _pid_counter[0] += 1
                style = ('slow' if (isinstance(factor_data[fi], (list, tuple)) and len(factor_data[fi]) > 2 and factor_data[fi][2] == 'C') else classify_track(f))
                positions.append({
                    'ei': ei, 'pid': _pid_counter[0],
                    'buy_date': date_int, 'orig_buy_idx': idx,
                    'check_from': idx + 1, 'entry_price': f['close'],
                    'max_price': f['close'], 'score': round(score, 1),
                    'below_ma20_days': 0, 'regime': regime, '_track': style,
                    'holding_score': 60, 'max_profit_pct': 0,
                })
                bought += 1; last_buy_di = di; total_slots = _total_slots()

            if available_slots <= 0 and total_slots >= effective_max_hold and not pos_scores:
                break

        # ═══ 加仓：持有分连续上升 → 同一标的再买1份 ═══
        if add_on_hscore_days > 0 and total_slots < effective_max_hold and bought < effective_buy_max:
            if not (USE_OAMV_GATE and regime == 'peaking') and not (USE_AD_STATE and prev_ad == 'defense') and not _is_crash:
                if buy_cooldown <= 0 or di - last_buy_di >= buy_cooldown:
                    for pos in positions[:]:
                        if _ei_pos_count(pos['ei']) - 1 >= 1:
                            continue  # 最多加仓1次
                        hs_hist = pos.get('_hs_hist', [])
                        if len(hs_hist) < add_on_hscore_days:
                            continue
                        recent = hs_hist[-add_on_hscore_days:]
                        if not all(recent[i] < recent[i+1] for i in range(len(recent)-1)):
                            continue
                        if total_slots >= effective_max_hold:
                            break
                        ei = pos['ei']
                        if ei not in idx_map:
                            continue
                        idx = idx_map[ei]
                        _pid_counter[0] += 1
                        positions.append({
                            'ei': ei, 'pid': _pid_counter[0],
                            'buy_date': date_int, 'orig_buy_idx': idx,
                            'check_from': idx + 1,
                            'entry_price': etf_data_list[ei]['closes'][idx],
                            'max_price': etf_data_list[ei]['closes'][idx],
                            'score': pos['score'], 'max_profit_pct': 0,
                            'holding_score': 60,
                            'below_ma20_days': 0, 'regime': regime,
                            '_track': pos.get('_track', 'slow'),
                            '_add_on': True,
                        })
                        bought += 1; last_buy_di = di; total_slots = _total_slots()

        # ═══ 快照 ═══
        if snapshot_callback is not None:
            # 计算各因子 pct_rank（候选池因子列展示）
            _all_f = [fd[1] for fd in factor_data]
            _fr = _v5_ranks_pct(_all_f, pr_lo, pr_hi)
            day_candidates = []
            for fi_idx, _ in ranked:
                ei_c, f_c = factor_data[fi_idx][0], factor_data[fi_idx][1]
                etf_c = etf_data_list[ei_c]
                fd_c = factor_data[fi_idx]
                track_c = fd_c[2] if isinstance(fd_c, (list, tuple)) and len(fd_c) > 2 else 'A'
                day_candidates.append({
                    'code': etf_c['code'], 'name': etf_c['name'],
                    'score': round(scores[fi_idx], 1),
                    'close': round(f_c['close'], 3),
                    'ma20': round(f_c.get('ma20', 0) or 0, 3),
                    'breakout_score': round(f_c.get('breakout_score', 0) or 0, 0),
                    'track': track_c,  # 轨标签：A=回踩型(主gate) B=动量型(快牛)
                    'trend_score': round(_fr['trend'][fi_idx], 0),
                    'dist_score': round(_fr['dist'][fi_idx], 0),
                    'mom60_score': round(_fr['mom60'][fi_idx], 0),
                    'mom20_score': round(_fr['mom20'][fi_idx], 0),
                    'liq_score': round(_fr['liq'][fi_idx], 0),
                    'spread_change_score': round(_fr['spread_change'][fi_idx], 0),
                    'sharpe_eff_score': round(_fr['sharpe_eff'][fi_idx], 0),
                    'pullback_confirm_score': round(_fr['pullback_confirm'][fi_idx], 0),
                    'overheat_score': round(_fr['overheat'][fi_idx], 0),
                })
            _emit_snapshot(snapshot_callback, date_int, positions, etf_data_list, idx_map, day_candidates, gate_failures)

        if crash_day_pct < 0:
            _prev_mr = _mr_today

    # ═══ 强制平仓 ═══
    for pos in list(positions):
        ei = pos['ei']; etf = etf_data_list[ei]
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
                    'etf_idx': ei, 'code': etf['code'], 'name': etf['name'],
                    'track': etf.get('track', ''),
                    '_track': pos.get('_track', 'slow'),
                    'buy_date': pos['buy_date'], 'sell_date': etf['dates'][di_check],
                    'entry': round(pos['entry_price'], 3), 'exit': round(close, 3),
                    'ret_pct': round(ret_pct, 2), 'max_pct': round(max_pct, 2),
                    'hold_days': hold_days,
                    'reason': '止损-8%' if loss_pct <= -8 else '跌破MA20',
                    'score': pos['score'], 'regime': pos.get('regime', '?'),
                })
                break
        else:
            close = etf['closes'][last_idx]
            ret_pct = (close / pos['entry_price'] - 1) * 100
            max_pct = (max_p / pos['entry_price'] - 1) * 100
            hold_days = last_idx - pos['orig_buy_idx']
            trades.append({
                'etf_idx': ei, 'code': etf['code'], 'name': etf['name'],
                'track': etf.get('track', ''),
                '_track': pos.get('_track', 'slow'),
                'buy_date': pos['buy_date'], 'sell_date': etf['dates'][last_idx],
                'entry': round(pos['entry_price'], 3), 'exit': round(close, 3),
                'ret_pct': round(ret_pct, 2), 'max_pct': round(max_pct, 2),
                'hold_days': hold_days, 'reason': '强制平仓(期末)',
                'score': pos['score'], 'regime': pos.get('regime', '?'),
            })

    if not trades:
        return None

    # ═══ 权益曲线 ═══
    all_dates = sorted(d for d in set(trading_days) if BACKTEST_START <= d <= BACKTEST_END)
    dc = [{} for _ in etf_data_list]
    for ei, etf in enumerate(etf_data_list):
        for i, d in enumerate(etf['dates']):
            dc[ei][d] = etf['closes'][i]

    for t in trades:
        if 'etf_idx' not in t:
            for ei, etf in enumerate(etf_data_list):
                if etf['code'] == t['code']:
                    t['etf_idx'] = ei
                    break

    events = []
    for t in trades:
        events.append(('buy', t['buy_date'], t))
        events.append(('sell', t['sell_date'], t))
    events.sort(key=lambda x: x[1])

    cash = 300000; slot = 300000 / BASE_MAX_HOLD
    pos_map = {}; _code_cnt = {}
    evt_idx = 0; peak = 300000; max_dd = 0.0; eq_net = []; total_fee = 0.0

    for d in all_dates:
        while evt_idx < len(events) and events[evt_idx][1] == d:
            typ, _, t = events[evt_idx]
            ei = t.get('etf_idx'); code = t.get('code', '')
            if typ == 'buy':
                n = _code_cnt.get(code, 0); _code_cnt[code] = n + 1
                key = (code, n)
                entry_p = t['entry']
                if apply_fee:
                    cost_per = entry_p * (1 + BUY_FEE_RATE)
                    total_fee += slot * BUY_FEE_RATE
                else:
                    cost_per = entry_p
                pos_map[key] = slot / cost_per
                cash -= slot
            else:  # sell
                exit_p = t['exit']
                for k in list(pos_map.keys()):
                    if k[0] == code:
                        shares = pos_map.pop(k)
                        if apply_fee:
                            fee_rate = get_fee_rate(t['buy_date'], t['sell_date'])
                            net_exit = exit_p * (1 - fee_rate)
                            total_fee += shares * exit_p * fee_rate
                        else:
                            net_exit = exit_p
                        cash += shares * net_exit
                        break
            evt_idx += 1

        hval = 0
        for (code, _n), shares in pos_map.items():
            pc = None
            for ei, etf in enumerate(etf_data_list):
                if etf['code'] == code:
                    pc = dc[ei].get(d)
                    break
            if pc:
                hval += shares * pc
        eq = cash + hval
        eq_net.append(eq)
        if eq > peak: peak = eq
        dd = (eq / peak - 1) * 100 if peak > 0 else 0
        if dd < max_dd: max_dd = dd

    rets = [t['ret_pct'] for t in trades]
    wins = [r for r in rets if r > 0]
    hold_days_arr = [t['hold_days'] for t in trades]
    avg_ret = sum(rets) / len(rets) if rets else 0
    win_rate = len(wins) / len(rets) * 100 if rets else 0
    gross_return = round((eq_net[-1] / 300000 - 1) * 100, 1) if eq_net else 0
    # 费后收益 = gross - fee（apply_fee=True时自动扣除）
    final_capital = round(eq_net[-1], 0) if eq_net else 300000

    # 夏普/Calmar
    sharpe = calmar = 0.0
    if len(eq_net) >= 2:
        daily_rets = []
        for i in range(1, len(eq_net)):
            if eq_net[i-1] > 0:
                daily_rets.append(eq_net[i] / eq_net[i-1] - 1)
        if len(daily_rets) >= 10:
            avg_d = sum(daily_rets) / len(daily_rets)
            var_d = sum((r - avg_d)**2 for r in daily_rets) / (len(daily_rets) - 1)
            std_d = var_d ** 0.5 if var_d > 1e-12 else 0
            if std_d > 1e-10:
                ann_ret = avg_d * 244; ann_vol = std_d * (244 ** 0.5)
                sharpe = round((ann_ret - 0.025) / ann_vol, 2)
                if abs(max_dd) > 0.1:
                    calmar = round(ann_ret / abs(max_dd/100), 2)

    return {
        'trades_count': len(trades), 'trades': trades, 'daily_log': daily_log,
        'avg_ret_pct': round(avg_ret, 2), 'win_rate': round(win_rate, 1),
        'profit_factor': round(abs(sum(wins)/sum(r for r in rets if r <= 0)), 2) if sum(r for r in rets if r <= 0) != 0 else float('inf'),
        'max_dd': round(max_dd, 2),
        'total_return_pct': gross_return, 'total_return_pct_gross': gross_return + round(total_fee/300000*100, 1),
        'final_capital': final_capital,
        'avg_hold_days': round(sum(hold_days_arr)/len(hold_days_arr), 0) if hold_days_arr else 0,
        'reasons': {r: sum(1 for t in trades if t['reason'] == r) for r in set(t['reason'] for t in trades)},
        'jump_stats': {'attempts': jump_attempts, 'success': jump_success},
        'regime_stats': dict(regime_stats),
        'fee_pct': round(total_fee / 300000 * 100, 2),
        'sharpe': sharpe, 'calmar': calmar,
        'ann_return_pct': round(ann_ret * 100, 1) if 'ann_ret' in dir() else 0.0,
        'ann_vol_pct': round(ann_vol * 100, 1) if 'ann_vol' in dir() else 0.0,
        # 费后权益曲线（apply_fee=True 时 eq_net 已扣费；供分享页展示）
        'eq_net': eq_net,
        'eq_net_dates': all_dates,
    }


def _emit_snapshot(cb, date_int, positions, etf_data_list, idx_map, candidates, gate_failures):
    day_holdings = []
    for pos in positions:
        etf_p = etf_data_list[pos['ei']]
        pidx = idx_map.get(pos['ei'], len(etf_p['closes']) - 1)
        cur_price = etf_p['closes'][pidx] if pidx < len(etf_p['closes']) else pos['entry_price']
        ret_pct = round((cur_price / pos['entry_price'] - 1) * 100, 2)
        h_score = pos.get('holding_score', pos['score'])
        day_holdings.append({
            'code': etf_p['code'], 'name': etf_p['name'],
            'buy_date': pos['buy_date'], 'buy_score': pos['score'],
            'holding_score': round(h_score, 1),
            'entry_price': round(pos['entry_price'], 3),
            'cur_price': round(cur_price, 3), 'ret_pct': ret_pct,
            'add_on': pos.get('_add_on', False),
            '_track': pos.get('_track', 'slow'),  # 形态：slow=回踩型 fast=动量型
        })
    cb(date_int, day_holdings, candidates, gate_failures)
