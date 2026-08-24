# -*- coding: utf-8 -*-
"""AH策略 v7 · Android APK 入口（Kivy）

点【开始计算】→ 增量补数据 → 拉实时价 → 引擎回放 → 显示今日信号。
数据/代码首次运行自动复制到应用私有目录（不依赖外部文件）。
"""
import os
import sys
import shutil
import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')

_gs = None       # gen_signal 模块（懒加载）
_WORKDIR = None  # 可写工作目录


def _ensure_files(app):
    """首次运行：把 assets 里的策略文件复制到应用私有目录（APK 内只读）"""
    dst = app.user_data_dir
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(ASSETS_DIR):
        for fn in os.listdir(ASSETS_DIR):
            s = os.path.join(ASSETS_DIR, fn)
            d = os.path.join(dst, fn)
            if os.path.isfile(s) and (not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d)):
                try:
                    shutil.copy2(s, d)
                except Exception:
                    pass
    return dst


def _load_engine(workdir):
    """从工作目录加载 gen_signal（其内部 os.chdir 到工作目录，数据读写均在私有目录）"""
    global _gs
    if _gs is not None:
        return _gs
    if workdir not in sys.path:
        sys.path.insert(0, workdir)
    os.chdir(workdir)
    import gen_signal as gs
    _gs = gs
    return gs


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation='vertical', **kw)
        self.padding = 8
        self.spacing = 6

        self.btn = Button(text='开始计算', size_hint=(1, None), height=56, font_size=18)
        self.btn.bind(on_press=self.on_calc)
        self.add_widget(self.btn)

        self.status = Label(text='就绪', size_hint=(1, None), height=30,
                            font_size=14, color=(0.6, 0.6, 0.6, 1))
        self.add_widget(self.status)

        self.output = TextInput(text='', readonly=True, multiline=True,
                                font_size=13, background_color=(1, 1, 1, 1))
        self.add_widget(self.output)

        self._busy = False

    def set_status(self, msg):
        Clock.schedule_once(lambda dt: setattr(self.status, 'text', msg))

    def append_out(self, msg):
        Clock.schedule_once(lambda dt: setattr(self.output, 'text', self.output.text + msg + '\n'))

    def on_calc(self, _btn):
        if self._busy:
            return
        self._busy = True
        self.btn.disabled = True
        self.output.text = ''
        self.set_status('计算中…（约 1-3 分钟，手机性能比电脑慢）')
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        app = App.get_running_app()
        try:
            workdir = _ensure_files(app)
            gs = _load_engine(workdir)
            self.append_out('加载 ETF 池 + 本地日线…')
            ah_selected, etf_data_list = gs.load_data()
            self.append_out(f'  加载 {len(etf_data_list)} 只')

            # 首次无数据 → 下载 300 天历史（仅第一次）
            if not etf_data_list:
                self.append_out('首次使用：下载 300 天历史数据（约 3-8 分钟）…')
                gs.setup_data()
                ah_selected, etf_data_list = gs.load_data()

            # 增量补收盘数据
            self.append_out('增量更新日线…')
            try:
                n = gs.update_daily(ah_selected)
                if n > 0:
                    self.append_out(f'  补了 {n} 条，重新加载')
                    ah_selected, etf_data_list = gs.load_data()
                else:
                    self.append_out('  数据已是最新')
            except Exception as e:
                self.append_out(f'  日线更新失败（{e}），用现有数据')

            # 拉实时价并注入
            self.append_out('拉取实时价…')
            injected, quotes, today = gs.inject_quotes(ah_selected, etf_data_list)
            if injected:
                self.append_out(f'  注入实时价 {injected}/{len(etf_data_list)} 只')
            else:
                self.append_out('  无实时行情（按最新收盘价回放）')

            # 引擎回放取最后一天
            last = {'holdings': [], 'candidates': [], 'gate_failures': {}}

            def save_snapshot(date_int, holdings, candidates, gate_failures=None):
                last['date'] = date_int
                last['holdings'] = holdings
                last['candidates'] = candidates
                last['gate_failures'] = gate_failures or {}

            self.append_out('引擎回放中…')
            r = getattr(gs, gs.ENGINE_FUNC)(etf_data_list, gs.WEIGHTS,
                                            snapshot_callback=save_snapshot, **gs.PARAMS)
            trades = r['trades']
            buys = [t for t in trades if t['buy_date'] == today]
            sells = [t for t in trades if t['sell_date'] == today and '强制平仓' not in (t.get('reason') or '')]

            # 渲染
            lines = ['【AH策略 v7 · 今日信号】',
                     f'时间：{gs.fmt_date(last.get("date", today))}' +
                     ('（盘中实时价）' if injected else '（收盘价）'), '']

            hs = last.get('holdings', [])
            lines.append(f'◆ 继续持有（{len(hs)}只）')
            for h in hs:
                tk = '回踩' if h.get('_track') == 'slow' else '动量'
                lines.append(f'  {h.get("name","?")}({h["code"]}) [{tk}] 浮盈{h.get("ret_pct",0):+.1f}%')
            if not hs:
                lines.append('  （无）')

            lines.append('')
            lines.append(f'◆ 今日买入（{len(buys)}只）')
            for t in buys:
                lines.append(f'  {t.get("name","?")}({t["code"]}) 评分{t.get("score",0):.0f}')
            if not buys:
                lines.append('  （无）')

            lines.append('')
            lines.append(f'◆ 今日卖出（{len(sells)}只）')
            for t in sells:
                lines.append(f'  {t.get("name","?")}({t["code"]}) {t.get("ret_pct",0):+.1f}% [{t.get("reason","")}]')
            if not sells:
                lines.append('  （无）')

            cs = last.get('candidates', [])[:5]
            lines.append('')
            lines.append('◆ 候选关注 Top5')
            for c in cs:
                tk = '回踩' if c.get('track') == 'A' else '动量'
                lines.append(f'  {c.get("name","?")}({c["code"]}) [{tk}] 评分{c.get("score",0):.1f}')
            if not cs:
                lines.append('  （无）')

            lines.append('')
            lines.append('（仅供研究参考，不构成投资建议）')
            self.append_out('\n'.join(lines))
            self.set_status('完成')
        except Exception as e:
            self.set_status('出错')
            self.append_out(f'\n[错误] {e}')
        finally:
            self._busy = False
            Clock.schedule_once(lambda dt: setattr(self.btn, 'disabled', False))


class AHSignalApp(App):
    def build(self):
        self.title = 'AH策略 v7'
        return Root()


if __name__ == '__main__':
    AHSignalApp().run()
