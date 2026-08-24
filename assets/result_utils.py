# -*- coding: utf-8 -*-
"""
回测结果后处理公共模块（2026-08-13 提取）
消除 gen_*.py 里重复的「权益回放 + 年度结算 + summary」逻辑。
三个策略（全球/AH/WB）共用，改一处生效多处。
"""


def replay_equity_fifo(all_trades, date_close, all_dates, slot_size, min_date=None):
    """FIFO 权益回放（支持同 code 多仓位，即加仓场景）。

    参数:
        all_trades: 交易列表，每笔含 buy_date/sell_date/code/entry/exit
        date_close: dict[(code, date_int)] -> close
        all_dates: 排序后的日期列表
        slot_size: 每仓位资金（= 300000 / max_hold）
        min_date: 回撤统计起点（快照起始日，之前不计回撤）

    返回: (date_equity: dict, max_dd: float)
    """
    date_equity = {}
    cash = 300000
    pos_map = {}  # (code, n) -> shares
    code_counter = {}
    event_idx = 0

    events = []
    for t in all_trades:
        events.append(('buy', t['buy_date'], t['code'], t['entry']))
        events.append(('sell', t['sell_date'], t['code'], t['exit']))
    events.sort(key=lambda x: x[1])

    peak = 300000
    max_dd = 0
    for d in all_dates:
        while event_idx < len(events) and events[event_idx][1] == d:
            typ, _, code, price = events[event_idx]
            if typ == 'buy':
                n = code_counter.get(code, 0)
                code_counter[code] = n + 1
                pos_map[(code, n)] = slot_size / price
                cash -= slot_size
            else:  # sell: FIFO
                for key in list(pos_map.keys()):
                    if key[0] == code:
                        cash += pos_map.pop(key) * price
                        break
            event_idx += 1

        hval = 0
        for (code, _n), shares in pos_map.items():
            pc = date_close.get((code, d))
            if pc:
                hval += shares * pc
        eq = cash + hval
        date_equity[d] = eq
        if eq > peak:
            peak = eq
        dd = (eq / peak - 1) * 100
        if dd < max_dd and (min_date is None or d >= min_date):
            max_dd = dd

    return date_equity, max_dd


def build_yearly(all_trades, date_equity):
    """年度结算，返回 (yearly_list, years_list)。"""
    year_trades = {}
    for t in all_trades:
        y = int(str(t['buy_date'])[:4])
        year_trades.setdefault(y, []).append(t)

    yearly = {}
    for y in sorted(year_trades.keys()):
        trs = year_trades[y]
        wins = sum(1 for t in trs if t['ret_pct'] > 0)
        y_start = y * 10000 + 101
        y_end = y * 10000 + 1231
        y_curve = [(d, eq) for d, eq in sorted(date_equity.items()) if y_start <= d <= y_end]
        if not y_curve:
            continue
        start_eq = y_curve[0][1]
        end_eq = y_curve[-1][1]
        peak_y = start_eq
        max_dd_y = 0
        for _, eq in y_curve:
            if eq > peak_y:
                peak_y = eq
            dd = (eq / peak_y - 1) * 100
            if dd < max_dd_y:
                max_dd_y = dd
        yearly[y] = {
            'year': str(y),
            'trades': len(trs),
            'return_pct': round((end_eq / start_eq - 1) * 100, 2),
            'max_dd': round(max_dd_y, 2),
            'win_rate': round(wins / len(trs) * 100, 1) if trs else 0,
            'end_equity': round(end_eq, 0),
            'start_equity': round(start_eq, 0),
        }

    years_sorted = sorted(yearly.keys())
    return [yearly[y] for y in years_sorted], [str(y) for y in years_sorted]


def build_summary(all_trades, date_equity, max_dd):
    """从权益曲线汇总 summary dict。"""
    final_date = sorted(date_equity.keys())[-1] if date_equity else None
    final_eq = date_equity.get(final_date, 300000) if final_date else 300000
    wins = sum(1 for t in all_trades if t['ret_pct'] > 0)
    return {
        'trades': len(all_trades),
        'return': round((final_eq / 300000 - 1) * 100, 1),
        'max_dd': round(max_dd, 1),
        'win_rate': round(wins / len(all_trades) * 100, 1) if all_trades else 0,
        'final_capital': round(final_eq, 0),
    }
