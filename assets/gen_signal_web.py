# -*- coding: utf-8 -*-
"""AH策略 · 盘中信号（本地 Web 版，打包成 exe 双击即用）

双击启动 → 自动打开浏览器 → 点「开始计算」→ 拉当天实时价 + 计算信号 → 展示结果。
不依赖 tkinter，用标准库 http.server + 浏览器前端。
"""
import os, sys, json, time, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gen_signal as gs  # gen_signal 内部会 os.chdir 到应用根目录

PORT = 8899

# 全局状态
_state = {
    'data_ready': False,
    'loading': False,
    'etf_data': None,
    'ah_selected': None,
    'result': None,
    'error': None,
}


def _preload():
    try:
        _state['ah_selected'], _state['etf_data'] = gs.load_data()
        _state['data_ready'] = True
    except Exception as e:
        _state['error'] = f'数据加载失败：{e}'
        _state['data_ready'] = False


def _run_signal():
    """增量补收盘日线 + 拉实时价 + 回放 + 取最后一天信号"""
    # 1. 增量更新日线（补到昨天收盘，过滤今天盘中快照，防止数据不累积）
    n = gs.update_daily(_state['ah_selected'])
    if n > 0:
        # .day 变了，重新加载（前复权 + 均线 + b1 因子）
        _state['ah_selected'], _state['etf_data'] = gs.load_data()

    # 2. 拉当天实时价（仅内存，不落盘）
    injected, quotes, today = gs.inject_quotes(_state['ah_selected'], _state['etf_data'])
    last = {'holdings': [], 'candidates': [], 'gate_failures': {}}

    def save_snapshot(date_int, holdings, candidates, gate_failures=None):
        last['date'] = date_int
        last['holdings'] = holdings
        last['candidates'] = candidates
        last['gate_failures'] = gate_failures or {}

    r = getattr(gs, gs.ENGINE_FUNC)(_state['etf_data'], gs.WEIGHTS,
                                    snapshot_callback=save_snapshot, **gs.PARAMS)
    trades = r['trades']
    buys = [t for t in trades if t['buy_date'] == today]
    sells = [t for t in trades if t['sell_date'] == today and '强制平仓' not in (t.get('reason') or '')]
    return {
        'date': gs.fmt_date(last.get('date', today)),
        'injected': injected,
        'total': len(_state['etf_data']),
        'updated': n,
        'holdings': last['holdings'],
        'buys': buys,
        'sells': sells,
        'candidates': last['candidates'][:10],
    }


HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AH策略 · 盘中信号</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f7fa;color:#1f2937;line-height:1.6}
.wrap{max-width:720px;margin:0 auto;padding:24px 16px 40px}
header{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-radius:14px;padding:22px 22px;margin-bottom:16px}
header h1{font-size:1.3rem}header .sub{font-size:.78rem;opacity:.85;margin-top:6px}
.bar{display:flex;align-items:center;gap:12px;margin-bottom:14px}
#btnStart{background:#2563eb;color:#fff;border:none;padding:11px 26px;border-radius:9px;font-size:.95rem;cursor:pointer;font-weight:600}
#btnStart:disabled{background:#c3cbd6;cursor:not-allowed}
#status{font-size:.82rem;color:#6b7280}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:16px 16px;margin-bottom:12px}
.card h3{font-size:.95rem;font-weight:700;margin-bottom:10px;border-left:3px solid #2563eb;padding-left:9px}
.card h3.buy{border-left-color:#dc2626}.card h3.sell{border-left-color:#059669}
.item{display:flex;justify-content:space-between;align-items:center;padding:7px 2px;border-top:1px solid #f1f4f8;font-size:.84rem}
.item:first-of-type{border-top:none}
.pos{color:#dc2626;font-weight:600}.neg{color:#059669;font-weight:600}
.tag{display:inline-block;font-size:.66rem;padding:1px 6px;border-radius:3px;font-weight:600}
.slow{background:#e6f7f5;color:#0f766e}.fast{background:#fff3e0;color:#d97706}
.empty{color:#9ca3af;font-size:.8rem;padding:10px 0;text-align:center}
.note{font-size:.72rem;color:#9ca3af;margin-top:8px}
footer{font-size:.72rem;color:#9ca3af;text-align:center;margin-top:18px}
</style>
</head>
<body><div class="wrap">
<header><h1>AH策略 · 盘中信号</h1>
<div class="sub">双轨强势动量 v7（分段评分+利润垫加仓）｜ 拉当天实时价 → 计算今日操作信号</div></header>

<div class="bar">
<button id="btnStart" onclick="start()">开始计算</button>
<span id="status">就绪</span>
</div>

<div id="result"></div>
<footer>仅供研究参考，不构成投资建议。</footer>
</div>
<script>
async function start(){
  const btn=document.getElementById('btnStart'), st=document.getElementById('status');
  btn.disabled=true; st.textContent='计算中（增量补收盘数据 + 拉实时价，约1-2分钟）...';
  document.getElementById('result').innerHTML='';
  try{
    const r=await (await fetch('/signal',{method:'POST'})).json();
    if(r.error){ st.textContent='出错：'+r.error; btn.disabled=false; return; }
    let upd = r.updated ? (' ｜ 已补 '+r.updated+' 条收盘数据') : '';
    st.textContent='完成 ｜ 信号日 '+r.date+(r.injected?'（已拉实时价 '+r.injected+'/'+r.total+'）':'（无实时行情，按最新收盘）')+upd;
    render(r);
  }catch(e){ st.textContent='出错：'+e.message; }
  btn.disabled=false;
}
function tag(t){return t==='slow'?'<span class="tag slow">回踩型</span>':'<span class="tag fast">动量型</span>';}
function items(arr, f){
  if(!arr||!arr.length) return '<div class="empty">（无）</div>';
  return arr.map(f).join('');
}
function render(r){
  const h=r.holdings.map(x=>`<div class="item"><span>${x.name} <small>(${x.code})</small> ${tag(x._track)}</span><span class="${x.ret_pct>=0?'pos':'neg'}">${x.ret_pct.toFixed(1)}%</span></div>`).join('')||'<div class="empty">（无）</div>';
  const b=r.buys.map(t=>`<div class="item"><span>${t.name} <small>(${t.code})</small></span><span class="pos">评分 ${t.score.toFixed(0)}</span></div>`).join('')||'<div class="empty">（无）</div>';
  const s=r.sells.map(t=>`<div class="item"><span>${t.name} <small>(${t.code})</small></span><span class="${t.ret_pct>=0?'pos':'neg'}">${t.ret_pct.toFixed(1)}%</span></div>`).join('')||'<div class="empty">（无）</div>';
  const c=r.candidates.map((x,i)=>`<div class="item"><span>${i+1}. ${x.name} <small>(${x.code})</small> ${tag(x.track==='A'?'slow':'fast')}</span><span class="pos">${x.score.toFixed(1)}</span></div>`).join('')||'<div class="empty">（无）</div>';
  document.getElementById('result').innerHTML=
    `<div class="card"><h3 class="sell">继续持有（${r.holdings.length}只）</h3>${h}</div>
     <div class="card"><h3 class="buy">今日买入（${r.buys.length}只）</h3>${b}</div>
     <div class="card"><h3 class="sell">今日卖出（${r.sells.length}只）</h3>${s}</div>
     <div class="card"><h3>候选 Top10</h3>${c}<div class="note">候选为当前评分排序，非买入指令。</div></div>`;
}
</script>
</body></html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype='application/json; charset=utf-8', code=200):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self._send(HTML, 'text/html; charset=utf-8')
        elif self.path == '/status':
            self._send(json.dumps({'data_ready': _state['data_ready'],
                                   'error': _state['error']}, ensure_ascii=False))
        else:
            self._send('Not Found', code=404)

    def do_POST(self):
        if self.path == '/signal':
            if not _state['data_ready']:
                self._send(json.dumps({'error': '数据还没加载完，请稍候'}, ensure_ascii=False))
                return
            try:
                result = _run_signal()
                self._send(json.dumps(result, ensure_ascii=False, default=str))
            except Exception as e:
                self._send(json.dumps({'error': str(e)}, ensure_ascii=False))
        else:
            self._send('Not Found', code=404)


def main():
    # 后台预加载数据
    threading.Thread(target=_preload, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f"服务已启动: http://127.0.0.1:{PORT}/")
    # 自动打开浏览器
    threading.Timer(0.8, lambda: webbrowser.open(f'http://127.0.0.1:{PORT}/')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
