# -*- coding: utf-8 -*-
"""盘中信号引擎（便携版核心）

流程：加载本地日线 → pytdx 拉当天实时价 → 注入为当天最新价 → 回放 → 取最后一天信号
输出：今日操作建议（HTML + 纯文本摘要）

用法：
  python gen_signal.py            # 正常拉实时价 + 回放
  python gen_signal.py --dry      # 不拉实时价，只看最后收盘状态（调试用）
"""
import os, json, time, sys
from datetime import datetime
import result_utils as ru


def _app_dir():
    """应用根目录：打包后=exe所在目录，开发时=脚本目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


os.chdir(_app_dir())

DRY = '--dry' in sys.argv
OPEN_SIGNAL = '--open' in sys.argv   # 跑完后自动用系统默认应用打开今日信号 txt

# ===================== 策略配置（与 gen_AH策略_snapshots.py 保持一致）=====================
ENGINE_FILE = 'AH策略_v7.py'   # v7 = v6 + 分段评分 + 利润垫加仓（2026-08-21 主配置）
ENGINE_FUNC = 'evaluate_weights_v5'
MAX_HOLD = 12
PARAMS = dict(
    gate=True, max_hold=MAX_HOLD, daily_buy_max=3,
    min_hold_days=10, buy_cooldown=5, scoring_method='zadd',
    sell_confirm_days=3, sell_gap_pct=12, breakeven_trigger_pct=5,
    use_holding_layer=True,
    score_mult_max=1.6, use_ad_state=True,
    vol_break_high=1.2, vol_low_confirm_adj=1,
    trend_use_ma30=True, gate_ma_n=250,
    max_gap=1.12, crossover_gap=1.12,
    bull_buy_mult=1.0, bull_score_threshold=65, bull_min_candidates=5,
    bull_top_avg=80,
    crash_day_pct=-2.0, crash_liquidate_type='cumulative',
    ad_defense_dn=-2.0, ad_ma_consec=5, ad_ma_period=10,
    ad_attack_up=2.5,
    b1_gate=True, b1_gate_threshold=50,
    add_on_hscore_days=3,
    profit_trail_tiers=[(20, 5), (30, 5), (40, 4)],
    dual_track=True,
    consecutive_bonus=0.05, consecutive_top_n=20,
    consecutive_max_days=5, consecutive_decay=None,
    track_b_stop_loss=-5, track_b_breakeven=3,
    track_b_profit_trail=[(15, 5), (25, 5), (35, 4)],
    track_c_vol_max=0,
    above_ma20_min=7, eff_min=0, m20ratio=0.99,
    rally_lo=0, rally_hi=999, vol_ratio_lo=0, vol_ratio_hi=999,
    # v7 新增：分段评分（初≤5×1.10 / 中6-20×1.0 / 末>20×0.60 / 999×1.0）+ 利润垫加仓（持有≥20交易日）
    stage_cfg={'bounds': (5, 20), 'c0': 1.10, 'c1': 1.0, 'c2': 0.60, 'c999': 1.0},
    addon_min_hold=20,
)
WEIGHTS = {'trend': 12, 'mom60': 8, 'mom20': 5, 'liq': 5, 'dist': 6,
           'spread_change': 18, 'sharpe_eff': 15,
           'pullback_confirm': 18, 'overheat': 2, 'breakout_score': 10,
           'b1_factor': 0}
# ======================================================================================

# ===== 推送配置（push_config.json 可覆盖；clawbot = pushplus 微信 ClawBot 渠道）=====
_DEFAULT_PUSH = {'enabled': False, 'pushplus_token': '', 'channel': 'clawbot', 'title': 'AH策略 · 今日信号'}
PUSH_CONF_PATH = 'push_config.json'

# 资源路径（PyInstaller 打包兼容：优先 sys._MEIPASS，兜底脚本目录）
def _res_path(name):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    if os.path.exists(p):
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# 加载引擎
_base = open(_res_path('AH策略_engine.py'), encoding='utf-8').read()
exec(_base.split("if __name__ == '__main__':")[0])

# v6/v7 引擎内部用相对路径 open('AH策略_engine.py') / open('AH策略_v6.py')，打包后需确保这些文件在当前目录
import shutil as _shutil
for _dep in ['AH策略_engine.py', 'AH策略_v6.py']:
    _dep_res = _res_path(_dep)
    _dep_cwd = os.path.join(_app_dir(), _dep)
    if os.path.abspath(_dep_res) != os.path.abspath(_dep_cwd):
        _shutil.copy2(_dep_res, _dep_cwd)

exec(open(_res_path(ENGINE_FILE), encoding='utf-8').read())
from b1_factor import precompute_b1_factors
import day_updater as du

# ===== 便携化：名称优先读 name_map.json（自包含，不依赖通达信 .tnf 目录）=====
_NAME_MAP = None
if os.path.exists(_res_path('name_map.json')):
    with open(_res_path('name_map.json'), encoding='utf-8') as _f:
        _NAME_MAP = json.load(_f)
_engine_load_names = load_names  # 保存原函数（读通达信 .tnf，作兜底）


def load_names():
    if _NAME_MAP:
        return dict(_NAME_MAP)
    return _engine_load_names()


# ===== 便携化：数据目录优先项目内 data/，兜底 C:/zd_zsone（本机既有数据）=====
_BASE_DIR = _app_dir()


def code_market(code):
    """按代码前缀推断真实市场（etf_pool 历史 bug：全标 'sh'，导致深市拉不到数据）。
    15/16 开头 → sz（深市基金），其余 → sh（沪市）。"""
    return 'sz' if code[:2] in ('15', '16') else 'sh'


def day_path(market, code):
    # 1. 项目内 data/vipdoc（公司电脑下载到这里的便携数据 / 手机 Termux 数据）
    for mk in [market, 'sh', 'sz', 'bj']:
        pp = os.path.join(_BASE_DIR, 'data', 'vipdoc', mk, 'lday', f'{mk}{code}.day')
        if os.path.exists(pp):
            return pp
    # 2. 兜底 C:/zd_zsone（仅 Windows 本机通达信数据；Android/Linux 无此路径直接跳过）
    if os.name == 'nt':
        for mk in [market, 'sh', 'sz', 'bj']:
            pp = f"C:/zd_zsone/vipdoc/{mk}/lday/{mk}{code}.day"
            if os.path.exists(pp):
                return pp
    return None


def setup_data():
    """下载 AH 池 614 只 ETF 历史日线到项目内 data/vipdoc（首次部署跑一次）
    数据源：腾讯 HTTPS（443，公司网络不拦）→ pytdx（回退）。"""
    pool = json.load(open(_res_path('etf_pool.json'), encoding='utf-8'))
    targets = [(code_market(e['code']), e['code']) for e in pool
               if not any(k in e['name'] for k in EXCLUDE_KW)]
    print(f"下载 {len(targets)} 只 ETF 最近 300 天日线...")
    t0 = time.time()
    done = 0
    for i, (mkt, code) in enumerate(targets):
        rows = []
        try:
            rows = du.tx_fetch_daily(mkt, code, 300)
        except Exception:
            pass
        if not rows:
            try:
                rows = du.fetch_tdx_daily(mkt, code, 300)
            except Exception:
                continue
        if not rows:
            continue
        out_dir = os.path.join(_BASE_DIR, 'data', 'vipdoc', mkt, 'lday')
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f'{mkt}{code}.day')
        existing = set()
        if os.path.exists(out_path):
            try:
                dates, *_ = read_day(out_path, code)
                existing = set(dates)
            except Exception:
                pass
        written = 0
        for row in sorted(rows, key=lambda r: r['date']):
            if row['date'] in existing:
                continue
            du.write_day_record(out_path, code, row['date'], row['open'], row['high'],
                                row['low'], row['close'], row['amount'])
            written += 1
        done += 1
        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(targets)}] 已下载 {done} 只, 耗时 {time.time()-t0:.0f}s")
    print(f"完成：{done} 只，耗时 {time.time()-t0:.0f}s → data/vipdoc/")


EXCLUDE_KW = ['纳指', '纳斯达克', '标普', '日经', '日本', '德国', '法国', '韩国', '印度', '越南', '巴西',
              '道琼斯', 'MSCI', '海外', '全球', '东南亚', '亚太', '中韩',
              '黄金', '金ETF', '上海金', '白银', '石油', '原油', '油气', '豆粕', '商品', '大宗商品',
              '沙特', '债',
              '湖北', '上海国企', '浙江国资', '大湾区', '长三角']  # 地区/区域主题ETF


def fmt_date(d):
    s = str(int(d))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def load_data():
    names = load_names()
    pool = json.load(open(_res_path('etf_pool.json'), encoding='utf-8'))
    ah_selected = []
    for e in pool:
        if any(k in e['name'] for k in EXCLUDE_KW):
            continue
        mk = code_market(e['code'])   # 修正 etf_pool 历史 bug（全标 'sh'）
        p = day_path(mk, e['code'])
        if p and os.path.exists(p):
            ah_selected.append((mk, e['code']))

    etf_data_list = []
    for prefix, code in ah_selected:
        p = day_path(prefix, code)
        if not p:
            continue
        try:
            dates, opens, highs, lows, closes, amounts = read_day(p, code)
        except Exception:
            continue
        if len(closes) < 120:
            continue
        closes, opens, highs, lows = forward_adjust(closes, opens, highs, lows)
        mas = {n: sma(closes, n) for n in NEED_MA}
        etf_data_list.append({
            'code': code, 'name': names.get(code, code),
            'dates': dates, 'closes': closes, 'mas': mas,
            'highs': highs, 'lows': lows, 'amounts': amounts, 'opens': opens,
            'track': extract_track(names.get(code, code))})

    b1 = precompute_b1_factors(etf_data_list)
    for i, etf in enumerate(etf_data_list):
        etf['b1_factor'] = b1[i]
    return ah_selected, etf_data_list


def inject_quotes(ah_selected, etf_data_list):
    """拉当天实时价并注入为最新一天。返回 (注入数量, 实时价dict, today)

    数据源顺序：腾讯 HTTPS（443，公司网络不拦）→ pytdx（回退）。
    """
    today = int(datetime.now().strftime('%Y%m%d'))
    if DRY:
        return 0, {}, today
    # 修正市场（etf_pool 历史 bug：全标 'sh'，深市基金必须用 sz 才拉得到）
    targets = [(code_market(c), c) for _, c in ah_selected]
    quotes = {}
    try:
        quotes = du.tx_fetch_quotes(targets)
    except Exception:
        quotes = {}
    if not quotes:
        # 腾讯源失败 → 回退 pytdx
        try:
            quotes = du.fetch_quotes(targets)
        except Exception:
            quotes = {}
    injected = 0
    for etf in etf_data_list:
        code = etf['code']
        if code not in quotes:
            continue
        q = quotes[code]
        if etf['dates'] and etf['dates'][-1] == today:
            etf['closes'][-1] = q['price']
        else:
            etf['dates'].append(today)
            etf['closes'].append(q['price'])
            etf['highs'].append(q['price'])
            etf['lows'].append(q['price'])
            etf['opens'].append(q['last_close'] if q['last_close'] > 0 else q['price'])
            etf['amounts'].append(q['amount'])
        etf['mas'] = {n: sma(etf['closes'], n) for n in NEED_MA}
        injected += 1
    return injected, quotes, today


def update_daily(ah_selected):
    """增量更新日线（补到最新收盘）。只写「昨天及之前」的收盘日线，过滤当天盘中快照。
    数据源：腾讯 HTTPS（443，公司网络不拦）→ pytdx（回退）。
    写入到 day_path 找到的现有数据文件。返回写入条数。"""
    today = int(datetime.now().strftime('%Y%m%d'))
    # 收盘判定：15:00 后视为已收盘，当天日线是完整收盘数据可落盘；
    # 盘中(<15:00)拉到的当天日线是未收盘快照，绝不落盘（防脏数据）
    is_market_closed = datetime.now().hour >= 15

    def _write_rows(rows):
        n = 0
        for mkt, code in ah_selected:
            path = day_path(mkt, code)
            existing = set()
            if path:
                try:
                    dates, *_ = read_day(path, code)
                    existing = set(dates)
                except Exception:
                    pass
            if path is None:
                out_dir = os.path.join(_BASE_DIR, 'data', 'vipdoc', mkt, 'lday')
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f'{mkt}{code}.day')
            for row in rows.get(code, []):
                d = row['date']
                if d > today:
                    continue
                if d == today and not is_market_closed:
                    continue  # 盘中：过滤当天未收盘快照
                if d in existing:
                    continue
                du.write_day_record(path, code, d, row['open'], row['high'],
                                    row['low'], row['close'], row['amount'])
                existing.add(d)
                n += 1
        return n

    # 1) 腾讯 HTTPS 源（每只拉 10 根即可覆盖增量；首装 300 天走 setup_data）
    try:
        rows_tx = {}
        ok_tx = True
        for mkt, code in ah_selected:
            r = du.tx_fetch_daily(mkt, code, count=10)
            if r:
                rows_tx[code] = r
        if rows_tx:
            n = _write_rows(rows_tx)
            if n > 0:
                return n
    except Exception:
        pass

    # 2) 回退 pytdx
    from pytdx.hq import TdxHq_API
    api = None
    for host, port in [('180.153.18.170', 7709), ('123.125.108.14', 7709), ('180.153.39.51', 7709)]:
        try:
            a = TdxHq_API()
            if a.connect(host, port, time_out=5):
                api = a
                break
        except Exception:
            continue
    if api is None:
        return 0
    mc_map = {'sh': 1, 'sz': 0, 'bj': 2}
    written = 0
    try:
        for mkt, code in ah_selected:
            path = day_path(mkt, code)
            existing = set()
            if path:
                try:
                    dates, *_ = read_day(path, code)
                    existing = set(dates)
                except Exception:
                    pass
            if path is None:
                out_dir = os.path.join(_BASE_DIR, 'data', 'vipdoc', mkt, 'lday')
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f'{mkt}{code}.day')
            try:
                bars = api.get_security_bars(9, mc_map.get(code_market(code), 1), code, 0, 10)
            except Exception:
                continue
            if not bars:
                continue
            for bar in bars:
                d = bar['year'] * 10000 + bar['month'] * 100 + bar['day']
                if d > today:
                    continue
                if d == today and not is_market_closed:
                    continue  # 盘中：过滤当天未收盘快照
                if d in existing:
                    continue
                du.write_day_record(path, code, d, bar['open'], bar['high'],
                                    bar['low'], bar['close'], bar['amount'])
                existing.add(d)
                written += 1
    finally:
        api.disconnect()
    return written


def _push_conf():
    """读取推送配置：优先 exe/脚本目录（用户可改），兜底打包临时目录的默认值"""
    cfg = dict(_DEFAULT_PUSH)
    for p in [os.path.join(_app_dir(), PUSH_CONF_PATH), _res_path(PUSH_CONF_PATH)]:
        try:
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    cfg = {**cfg, **json.load(f)}
                break
        except Exception:
            continue
    return cfg


def cfg_is_enabled():
    """推送配置是否已启用（用于决定跳过时如何提示）"""
    return _push_conf().get('enabled', False)


def push_wechat(txt, title=None):
    """通过 pushplus 的 clawbot 渠道推送到个人微信（无需企业微信）。

    配置：push_config.json = {"enabled": true, "pushplus_token": "xxx", "channel": "clawbot"}
    返回 (ok, msg)
    """
    cfg = _push_conf()
    if not cfg.get('enabled'):
        return False, '推送未启用（push_config.json enabled=false）'
    if not cfg.get('pushplus_token'):
        return False, '未配置 pushplus_token'
    import urllib.request
    body = json.dumps({
        'title': title or cfg.get('title', 'AH策略 · 今日信号'),
        'content': txt,
        'channel': cfg.get('channel', 'clawbot'),
        'template': 'txt',
    }, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        f'http://www.pushplus.plus/send/{cfg["pushplus_token"]}',
        data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode('utf-8'))
        ok = resp.get('code') == 200
        return ok, resp.get('msg', '')
    except Exception as e:
        return False, str(e)


def main():
    if '--setup' in sys.argv:
        setup_data()
        return

    print("加载 ETF 池 + 本地日线...")
    t0 = time.time()
    ah_selected, etf_data_list = load_data()
    print(f"  加载 {len(etf_data_list)} 只")

    # 增量更新日线（补到昨天收盘，过滤今天盘中快照，防止数据不累积）
    print("增量更新日线（复用连接，补到最新收盘）...")
    try:
        n = update_daily(ah_selected)
        if n > 0:
            print(f"  补了 {n} 条收盘数据，重新加载")
            ah_selected, etf_data_list = load_data()
        else:
            print("  数据已是最新")
    except Exception as e:
        print(f"  日线更新失败（{e}），用现有数据继续")

    print("拉取实时价并注入...")
    injected, quotes, today = inject_quotes(ah_selected, etf_data_list)
    if injected:
        print(f"  注入实时价 {injected}/{len(etf_data_list)} 只")
    else:
        print("  今日无实时行情（休市/未开盘/连接失败），按最新收盘价回放")

    # 快照回调：只记录最后一天
    last = {'holdings': [], 'candidates': [], 'gate_failures': {}}

    def save_snapshot(date_int, holdings, candidates, gate_failures=None):
        last['date'] = date_int
        last['holdings'] = holdings
        last['candidates'] = candidates
        last['gate_failures'] = gate_failures or {}

    print("回放中（复用 v7 引擎）...")
    r = globals()[ENGINE_FUNC](etf_data_list, WEIGHTS, snapshot_callback=save_snapshot, **PARAMS)
    trades = r['trades']

    # 取「今天」的买入 / 卖出（非强制平仓）
    buys_today = [t for t in trades if t['buy_date'] == today]
    sells_today = [t for t in trades if t['sell_date'] == today and '强制平仓' not in (t.get('reason') or '')]

    # 输出
    out_html = render_html(last, buys_today, sells_today, today, injected, len(etf_data_list))
    out_txt = render_text(last, buys_today, sells_today, today, injected, len(etf_data_list))

    html_path = 'AH策略_今日信号.html'
    txt_path = 'AH策略_今日信号.txt'
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(out_html)
    with open(txt_path, 'w', encoding='utf-8') as f:
        f.write(out_txt)

    print(f"\n耗时 {time.time()-t0:.1f}s")
    print(f"信号日: {fmt_date(last.get('date', today))}")
    print(f"  继续持有 {len(last['holdings'])} 只 | 今日买入 {len(buys_today)} 只 | 今日卖出 {len(sells_today)} 只")
    print(f"已生成: {html_path} / {txt_path}")
    print("\n========== 今日信号（文本） ==========")
    print(out_txt)

    # 推送（可选）：push_config.json 启用后发到微信 ClawBot
    ok, msg = push_wechat(out_txt)
    if ok:
        print(f"\n[推送] 已发送到微信 ClawBot: {msg}")
    elif cfg_is_enabled():
        print(f"\n[推送] 失败: {msg}")
    else:
        print(f"\n[推送] 跳过（{msg}；配置 push_config.json 可启用）")

    # 自动弹出（可选）：--open 时用系统默认应用打开今日信号（Windows: os.startfile；Android/Linux: subprocess）
    if OPEN_SIGNAL:
        try:
            if hasattr(os, 'startfile'):
                os.startfile(txt_path)
            else:
                import subprocess
                subprocess.Popen(['termux-open', txt_path] if os.path.exists('/data/data/com.termux') else ['xdg-open', txt_path])
            print(f"[弹出] 已打开 {txt_path}")
        except Exception as e:
            print(f"[弹出] 打开失败: {e}")


def render_text(last, buys, sells, today, injected, total):
    lines = []
    lines.append("【AH策略 · 今日盘中信号】")
    lines.append(f"时间：{fmt_date(last.get('date', today))}" + ("（盘中实时价）" if injected else "（收盘价）"))
    lines.append("")
    # 继续持有
    hs = last.get('holdings', [])
    lines.append(f"◆ 继续持有（{len(hs)}只）")
    if hs:
        for h in hs:
            tk = '回踩型' if h.get('_track') == 'slow' else '动量型'
            lines.append(f"  {h.get('name','?')}({h['code']}) [{tk}] 浮盈{h.get('ret_pct',0):+.1f}%")
    else:
        lines.append("  （无）")
    # 今日买入
    lines.append("")
    lines.append(f"◆ 今日买入（{len(buys)}只）")
    if buys:
        for t in buys:
            lines.append(f"  {t.get('name','?')}({t['code']}) 评分{t.get('score',0):.0f}")
    else:
        lines.append("  （无）")
    # 今日卖出
    lines.append("")
    lines.append(f"◆ 今日卖出（{len(sells)}只）")
    if sells:
        for t in sells:
            lines.append(f"  {t.get('name','?')}({t['code']}) {t.get('ret_pct',0):+.1f}% [{t.get('reason','')}]")
    else:
        lines.append("  （无）")
    # 候选 Top5
    cs = last.get('candidates', [])[:5]
    lines.append("")
    lines.append("◆ 候选关注 Top5")
    if cs:
        for c in cs:
            tk = '回踩' if c.get('track') == 'A' else '动量'
            lines.append(f"  {c.get('name','?')}({c['code']}) [{tk}] 评分{c.get('score',0):.1f}")
    else:
        lines.append("  （无）")
    lines.append("")
    lines.append("（仅供研究参考，不构成投资建议）")
    return "\n".join(lines)


CSS = '''
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2937;line-height:1.6}
.wrap{max-width:680px;margin:0 auto;padding:20px 14px 40px}
header{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:12px;padding:18px 18px;margin-bottom:14px}
header h1{font-size:1.15rem}
header .sub{font-size:.75rem;opacity:.85;margin-top:4px}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px 14px;margin-bottom:12px}
.card h3{font-size:.95rem;font-weight:700;margin-bottom:8px;border-left:3px solid #2563eb;padding-left:9px}
.card h3.buy{border-left-color:#dc2626}
.card h3.sell{border-left-color:#059669}
.item{display:flex;justify-content:space-between;align-items:center;padding:7px 2px;border-top:1px solid #f1f4f8;font-size:.82rem}
.item:first-of-type{border-top:none}
.pos{color:#dc2626;font-weight:600}
.neg{color:#059669;font-weight:600}
.tag{display:inline-block;font-size:.66rem;padding:1px 6px;border-radius:3px;font-weight:600}
.track-slow{background:#e6f7f5;color:#0f766e}
.track-fast{background:#fff3e0;color:#d97706}
.empty{color:#9ca3af;font-size:.8rem;padding:10px 0;text-align:center}
.note{font-size:.72rem;color:#9ca3af;margin-top:8px}
footer{font-size:.72rem;color:#9ca3af;text-align:center;margin-top:18px}
'''


def render_html(last, buys, sells, today, injected, total):
    def track_tag(t):
        return '<span class="tag track-slow">回踩型</span>' if t == 'slow' else '<span class="tag track-fast">动量型</span>'

    hs = last.get('holdings', [])
    h_html = ''.join(
        f'<div class="item"><span>{h.get("name","?")} <small>({h["code"]})</small> {track_tag(h.get("_track"))}</span>'
        f'<span class="{"pos" if (h.get("ret_pct",0) or 0)>=0 else "neg"}">{h.get("ret_pct",0):+.1f}%</span></div>'
        for h in hs) or '<div class="empty">无持仓</div>'

    b_html = ''.join(
        f'<div class="item"><span>{t.get("name","?")} <small>({t["code"]})</small></span>'
        f'<span class="pos">评分 {t.get("score",0):.0f}</span></div>'
        for t in buys) or '<div class="empty">今日无买入</div>'

    s_html = ''.join(
        f'<div class="item"><span>{t.get("name","?")} <small>({t["code"]})</small></span>'
        f'<span class="{"pos" if (t.get("ret_pct",0) or 0)>=0 else "neg"}">{t.get("ret_pct",0):+.1f}%</span></div>'
        for t in sells) or '<div class="empty">今日无卖出</div>'

    cs = last.get('candidates', [])[:10]
    c_html = ''.join(
        f'<div class="item"><span>{c.get("name","?")} <small>({c["code"]})</small> '
        f'{track_tag("slow" if c.get("track")=="A" else "fast")}</span>'
        f'<span class="pos">{c.get("score",0):.1f}</span></div>'
        for c in cs) or '<div class="empty">无候选</div>'

    stamp = f"{fmt_date(last.get('date', today))}" + ("（盘中实时价）" if injected else "（收盘价）")

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AH策略 · 今日信号</title><style>{CSS}</style></head>
<body><div class="wrap">
<header><h1>AH策略 · 今日盘中信号</h1><div class="sub">{stamp} ｜ 已注入实时价 {injected}/{total} 只</div></header>

<div class="card"><h3 class="sell">继续持有（{len(hs)}只）</h3>{h_html}</div>
<div class="card"><h3 class="buy">今日买入（{len(buys)}只）</h3>{b_html}</div>
<div class="card"><h3 class="sell">今日卖出（{len(sells)}只）</h3>{s_html}</div>
<div class="card"><h3>候选关注 Top10</h3>{c_html}
<div class="note">注：候选为当前评分排序，非买入指令；买入仍受每日上限与资金约束。</div></div>

<footer>由量化策略自动生成，仅供研究参考，不构成投资建议。</footer>
</div></body></html>'''


if __name__ == '__main__':
    main()
