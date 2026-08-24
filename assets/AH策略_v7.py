# -*- coding: utf-8 -*-
"""AH策略 v7 — 分段评分 + 利润垫加仓（2026-08-21）

相对 v6（zadd合成 + 双轨 + 分轨止损，费后+42.6%）的改进：
  ① 分段评分（stage_cfg）：按距金叉天数 days_since_cross 分初/中/末段乘系数
     v3 最优参数：初段≤5 ×1.10 / 中段6-20 ×1.0 / 末段>20 ×0.60 / 无金叉999 ×1.0
     效果：+42.6%→+47.2%（费后），Calmar 1.28→1.39，胜率 44.3→47.3%
  ② 利润垫加仓（addon_min_hold）：持有≥N交易日 + 当前浮盈>0 + 持有分>买入分 → 加仓1份
     修复 v6 加仓死代码（bought<effective_buy_max 与 buy_cooldown 双重锁死，原机制从未触发）
     效果：分段+加仓20 → 费后+49.7% / DD-6.3% / 夏普0.83 / Calmar1.45 / 胜率46.4%
  机制：分段=买入侧避开追高（末段重罚），加仓=持仓侧对赢家下注（初段结构才有利润垫）
  注：加仓2次(pf2x)证伪（-1.7%追高亏损）；v2系数(1.0/1.15/0.85)与加仓负交互（加仓均-3.7%）

用法：gen 脚本 exec 本文件后，evaluate_weights_v5 即为 v7 引擎（scoring_method='zadd'）
新参数：sell_cooldown_days=10（卖出冷却，v6 写死）、stage_cfg=None（分段系数）、addon_min_hold=0（利润垫加仓门槛，0=关闭）
"""
import os

# 加载 v6 引擎源码（含 base 引擎 exec + pullback 修改 + calc_factors v5 + score_etfs + evaluate_weights_v5）
_v6_code = open('AH策略_v6.py', encoding='utf-8').read()

# ═══════ ① 冷却参数化（v6 写死 cooldown_days=10）═══════
assert '    cooldown_days = 10' in _v6_code
_v6_code = _v6_code.replace('    cooldown_days = 10', '    cooldown_days = sell_cooldown_days')

# ═══════ ② 分段评分注入（首次 ranked 排序前）═══════
_HOOK_STAGE = """        if stage_cfg is not None:
            for _i in range(len(factor_data)):
                _ei = factor_data[_i][0]
                _etf = etf_data_list[_ei]
                _ix = idx_map[_ei]
                _mas = _etf['mas']
                _dsc = 999
                for _j in range(_ix, max(0, _ix - 120), -1):
                    _a, _b = _mas[20][_j], _mas[60][_j]
                    _ap, _bp = _mas[20][_j-1], _mas[60][_j-1]
                    if (_a and _b and _ap and _bp and _a >= _b and _ap < _bp):
                        _dsc = _ix - _j
                        break
                _b0, _b1 = stage_cfg['bounds']
                if _dsc == 999:
                    _c = stage_cfg.get('c999', 1.0)
                elif _dsc <= _b0:
                    _c = stage_cfg['c0']
                elif _dsc <= _b1:
                    _c = stage_cfg['c1']
                else:
                    _c = stage_cfg['c2']
                scores[_i] = scores[_i] * _c
        ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)
"""
_anchor_rank = "        ranked = sorted(zip(range(len(factor_data)), scores), key=lambda x: x[1], reverse=True)\n"
assert _anchor_rank in _v6_code
_v6_code = _v6_code.replace(_anchor_rank, _HOOK_STAGE, 1)

# ═══════ ③ 利润垫加仓注入 ═══════
# a) 外层条件：去掉 bought < effective_buy_max 锁死（买入满额后永远 False）
_anchor_outer = "        if add_on_hscore_days > 0 and total_slots < effective_max_hold and bought < effective_buy_max:\n"
assert _anchor_outer in _v6_code
_v6_code = _v6_code.replace(_anchor_outer,
    "        if add_on_hscore_days > 0 and total_slots < effective_max_hold:\n")
# b) 冷却跳过：profit 模式（addon_min_hold>0）不受全局买入冷却限制
_anchor_cd = ("                if buy_cooldown <= 0 or di - last_buy_di >= buy_cooldown:\n"
              "                    for pos in positions[:]:\n")
assert _anchor_cd in _v6_code
_v6_code = _v6_code.replace(_anchor_cd,
    "                if addon_min_hold > 0 or buy_cooldown <= 0 or di - last_buy_di >= buy_cooldown:\n"
    "                    for pos in positions[:]:\n")
# c) 触发条件：addon_min_hold>0 用利润垫条件，否则用原连续上升条件
_anchor_cond = (
    "                        hs_hist = pos.get('_hs_hist', [])\n"
    "                        if len(hs_hist) < add_on_hscore_days:\n"
    "                            continue\n"
    "                        recent = hs_hist[-add_on_hscore_days:]\n"
    "                        if not all(recent[i] < recent[i+1] for i in range(len(recent)-1)):\n"
    "                            continue\n")
assert _anchor_cond in _v6_code
_cond_else = "".join('    ' + ln if ln.strip() else ln for ln in _anchor_cond.splitlines(True))
_HOOK_COND = (
    "                        if addon_min_hold > 0:\n"
    "                            # 利润垫加仓：持有≥N交易日 + 当前浮盈>0 + 持有分>买入分\n"
    "                            _ei_p = pos['ei']\n"
    "                            _idx_p = idx_map.get(_ei_p)\n"
    "                            if _idx_p is None:\n"
    "                                continue\n"
    "                            if (_idx_p - pos['orig_buy_idx']) < addon_min_hold:\n"
    "                                continue\n"
    "                            _cur_ret = (etf_data_list[_ei_p]['closes'][_idx_p] / pos['entry_price'] - 1) * 100\n"
    "                            if _cur_ret <= 0:\n"
    "                                continue\n"
    "                            if pos.get('holding_score', 60) <= pos['score']:\n"
    "                                continue\n"
    "                        else:\n"
    + _cond_else)
_engine_src = _v6_code.replace(_anchor_cond, _HOOK_COND)

# ═══════ ④ 函数签名加新参数 ═══════
_anchor_sig = "def evaluate_weights_v5(etf_data_list, weights, gate=True, max_hold=10, daily_buy_max=3,\n"
assert _anchor_sig in _engine_src
_engine_src = _engine_src.replace(_anchor_sig,
    "def evaluate_weights_v5(etf_data_list, weights, gate=True, max_hold=10, daily_buy_max=3,\n"
    "                        sell_cooldown_days=10, stage_cfg=None, addon_min_hold=0,\n")

exec(_engine_src)
