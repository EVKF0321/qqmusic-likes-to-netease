from __future__ import annotations
import asyncio
from dataclasses import asdict
import io
import os
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from .core import Engine, Workspace, Song, SafetyError, RequestFailure, MIGRATION_WRITE_INTERVAL, backup, digest, ids
from .providers import QQ, Netease
from .vault import Vault
from .searching import match_pending, reconfirm_cached
from . import __version__
from .diagnostics import describe_error
from .review import ReviewWindow
from .display import Display
from .ui import BG, PAPER, INK, ACCENT, MUTED, FONT, Flow, FAQ, GUIDE, hint, setup_style, show_help, wrap_to_width


def app_root():
    return Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


class App:
    def __init__(self, root, data_dir=None):
        self.root = root
        self.base = app_root()
        self.store = Workspace(data_dir or self.base / 'data')
        automatically_confirmed = reconfirm_cached(self.store)
        self.vault = Vault(self.store.root / 'authorization.dpapi')
        auth_error = False
        try:
            auth = self.vault.read()
        except Exception:
            auth = {}
            auth_error = True
        self.qq = QQ(auth.get('qq'), str(self.store.root / 'qq-device.json'))
        self.ne = Netease(self.base / 'bridge', auth.get('netease', ''), str(self.base / 'runtime' / 'node.exe') if (self.base / 'runtime' / 'node.exe').exists() else 'node')
        self.events, self.busy, self.cancel = queue.Queue(), False, threading.Event()
        self.ne.log, self.ne.stopped = self.log, self.cancel.is_set
        self.ne.diagnostic_path = self.store.root / 'diagnostics.jsonl'
        self.buttons = []
        self.running_migration = False
        self.account_ids = {'qq': None, 'netease': None}
        self.account_labels = {}
        self.blocked_source = None
        self.build()
        self.refresh()
        if auth_error:
            self.status.set('无法读取本机保存的授权，请重新扫码连接。')
        elif automatically_confirmed:
            self.status.set(f'已按原有评分设置自动确认 {automatically_confirmed} 首候选。')
        self.pump_id = root.after(100, self.pump)
        self.startup_id = root.after_idle(self.startup_check)
        root.bind('<Destroy>', self.cancel_timers, add='+')
        root.protocol('WM_DELETE_WINDOW', self.close)

    def build(self):
        root = self.root
        display = self.display = Display.for_widget(root)
        root.title(f'喜欢搬家 · {__version__}')
        icon = Path(__file__).resolve().parents[1] / 'assets' / 'musicbridge.ico'
        if os.name == 'nt' and icon.is_file():
            root.iconbitmap(default=str(icon))
            root.iconbitmap(str(icon))
        display.configure_window(root, (1120, 760), (650, 540))
        setup_style(root, display)
        outer = ttk.Frame(root, style='Page.TFrame', padding=display.dims(14, 10))
        outer.grid(sticky='nsew')
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1, minsize=display.px(80))

        header = ttk.Frame(outer, style='Page.TFrame')
        header.grid(row=0, sticky='ew', pady=(0, 9))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text='喜欢搬家', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(header, text='QQ 音乐  →  网易云音乐', style='Subtitle.TLabel').grid(row=0, column=1, sticky='w', padx=(12, 4))
        ttk.Button(header, text='使用指南', command=lambda: show_help(root, '使用指南', GUIDE)).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(header, text='常见问题', command=lambda: show_help(root, '常见问题', FAQ)).grid(row=0, column=3)

        accounts = ttk.Frame(outer, style='Page.TFrame')
        accounts.grid(row=1, sticky='ew', pady=(0, 8))
        for col, key, name, text, command in (
            (0, 'qq', 'QQ 音乐', 'QQ 扫码登录', self.login_qq),
            (1, 'netease', '网易云音乐', '网易云扫码登录', self.login_ne),
        ):
            accounts.columnconfigure(col, weight=1, uniform='accounts')
            card = ttk.Frame(accounts, padding=display.dims(9, 6))
            card.grid(row=0, column=col, sticky='nsew', padx=(0, 4) if col == 0 else (4, 0))
            card.columnconfigure(0, weight=1)
            ttk.Label(card, text=name, style='Strong.TLabel').grid(row=0, column=0, sticky='w')
            label = ttk.Label(card, text='未连接', style='Muted.TLabel')
            label.grid(row=1, column=0, sticky='ew')
            wrap_to_width(label)
            self.account_labels[key] = label
            button = self.button(card, text, command, '向对应平台请求二维码，用手机 App 扫码授权。')
            button.grid(row=0, column=1, rowspan=2, sticky='e', padx=(4, 0))
        tools = ttk.Frame(accounts, style='Page.TFrame')
        tools.grid(row=0, column=2, sticky='ns', padx=(8, 0))
        self.button(tools, '检查连接', self.check_connections, '重新核验保存的授权，只查询登录状态。').pack(side='left')
        self.button(tools, '清除授权', self.clear_auth, '只清除本机授权；保留备份和报告。').pack(side='left', padx=(5, 0))

        banner = ttk.Frame(outer, style='Banner.TFrame', padding=display.dims(10, 7))
        banner.grid(row=2, sticky='ew', pady=(0, 8))
        self.step = tk.StringVar()
        step = ttk.Label(banner, textvariable=self.step, style='Step.TLabel')
        step.pack(fill='x')
        wrap_to_width(step)

        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=3, sticky='nsew')
        listing = ttk.Frame(self.tabs, padding=display.dims(9, 7))
        self.tabs.add(listing, text='歌曲与匹配')
        actions = self.song_actions = Flow(listing)
        actions.pack(fill='x', pady=(0, 3))
        self.button(actions, '读取并备份两端', self.read, '读取 QQ「我喜欢」和网易云红心列表，保存本地备份。')
        self.button(actions, '反转源序列', self.reverse, '倒转 QQ 歌曲列表。仅当界面顺序与工具刚好相反时使用。')
        self.button(actions, '确认源顺序', self.confirm_source, '对照 QQ 列表确认顺序后，记录当前排序方式。')
        self.button(actions, '搜索匹配 / 续查', self.match, '搜索网易云候选；中断后继续未完成的搜索。')
        self.button(actions, '人工选候选', self.choose, '为选中的歌曲改选候选，或用网易云单曲链接手动补歌。')
        self.review_button = self.button(actions, '连续确认（0）', self.review_all, '打开待确认队列，确认一首自动切换到下一首。')
        summary = ttk.Frame(listing)
        summary.pack(fill='x', pady=(0, 4))
        self.counts = tk.StringVar()
        count_label = ttk.Label(summary, textvariable=self.counts, style='Muted.TLabel')
        count_label.pack(side='left', fill='x', expand=True)
        wrap_to_width(count_label)
        self.locate_button = self.button(summary, '定位出错歌曲', self.locate_blocked, '选中上次暂停的曲目，然后可点击「人工选候选」。')
        self.locate_button.pack(side='right', padx=(5, 0))
        table = ttk.Frame(listing)
        table.pack(fill='both', expand=True)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=('pos', 'title', 'artist', 'album', 'match'), show='headings', selectmode='browse', height=5)
        for name, title, width, minimum in [('pos', '序号', 48, 48), ('title', 'QQ 歌曲', 210, 125), ('artist', '歌手', 137, 80), ('album', '专辑', 137, 80), ('match', '状态 / 网易云匹配', 315, 215)]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=display.px(width), minwidth=display.px(minimum), stretch=name != 'pos')
        self.tree.tag_configure('stripe', background='#f6f7fc')
        self.tree.tag_configure('muted', foreground='#596176', background='#eef0f4')
        self.tree.tag_configure('blocked', foreground='#844411', background='#fff0dd')
        vbar = ttk.Scrollbar(table, command=self.tree.yview)
        hbar = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vbar.grid(row=0, column=1, sticky='ns')
        hbar.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<Double-1>', lambda e: self.guard(self.choose) if self.tree.identify_row(e.y) else None)
        details = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(details, text='预览与报告')
        bar = ttk.Scrollbar(details)
        bar.pack(side='right', fill='y')
        self.details = tk.Text(details, wrap='word', font=(FONT, 10), bg=PAPER, fg=INK, relief='flat',
                               padx=10, pady=8, spacing2=3, yscrollcommand=bar.set, state='disabled')
        bar.configure(command=self.details.yview)
        self.details.pack(fill='both', expand=True)
        self.details.tag_configure('heading', foreground=ACCENT, font=(FONT, 14, 'bold'), spacing3=8)
        self.set_details('预览与报告', '点击「迁移预览」查看将要执行的变更。迁移结束后，这里会显示核验结果。', select=False)

        settings = ttk.Frame(outer, padding=display.dims(9, 7))
        settings.grid(row=4, sticky='ew', pady=(8, 0))
        controls = Flow(settings)
        controls.pack(fill='x')
        self.mode = tk.StringVar(value='merge')
        self.merge_radio = controls.add(ttk.Radiobutton(controls, text='安全合并', variable=self.mode, value='merge', command=self.update_step))
        self.sync_radio = controls.add(ttk.Radiobutton(controls, text='严格同步', variable=self.mode, value='sync', command=self.update_step))
        hint(self.merge_radio, '保留原有收藏的位置，按顺序新增缺少的歌曲。')
        hint(self.sync_radio, '要求通过重排实测且目标没有额外收藏，按 QQ 顺序重排目标。')
        self.buttons.extend([self.merge_radio, self.sync_radio])
        self.button(controls, '排序实测', self.probe, '首次迁移前核验置顶或置底规则。会先请求授权再真实新增两首；原任务续传不必重测。')
        self.button(controls, '迁移预览', self.preview, '实时读取目标，列出本轮将新增的歌曲；预览不写入。')
        self.button(controls, '确认迁移 / 续传', self.migrate, '核对预览并授权后开始写入。续传前先重新预览。', accent=True)

        footer = ttk.Frame(settings)
        footer.pack(fill='x', pady=(2, 0))
        footer.columnconfigure(1, weight=1)
        self.progress_text = tk.StringVar(value='迁移进度 · 尚未开始')
        ttk.Label(footer, textvariable=self.progress_text, style='Strong.TLabel').grid(row=0, column=0, sticky='w', padx=(0, 9))
        self.progress = ttk.Progressbar(footer, mode='determinate', maximum=1, value=0)
        self.progress.grid(row=0, column=1, sticky='ew')
        self.pause_button = ttk.Button(footer, text='暂停', command=self.pause, state='disabled')
        self.pause_button.grid(row=0, column=2, padx=(8, 5))
        hint(self.pause_button, '当前请求结束后暂停。已核验的进度会保留。')
        report_button = ttk.Button(footer, text='打开备份与报告', command=lambda: os.startfile(str(self.store.root)))
        report_button.grid(row=0, column=3)
        hint(report_button, '打开本机的数据目录，查看 backups 和 reports。')
        self.status = tk.StringVar(value='就绪。')
        status = ttk.Label(outer, textvariable=self.status, style='PageMuted.TLabel')
        status.grid(row=5, sticky='ew', pady=(5, 0))
        wrap_to_width(status)

    def button(self, frame, text, command, help_text, *, accent=False):
        style = 'Accent.TButton' if accent else 'TButton'
        if frame is getattr(self, 'song_actions', None):
            style = 'Compact.TButton'
        b = ttk.Button(frame, text=text, command=lambda: self.guard(command), style=style)
        hint(b, help_text)
        if isinstance(frame, Flow):
            frame.add(b)
        self.buttons.append(b)
        return b

    def guard(self, command):
        if self.busy:
            return
        try:
            command()
        except Exception as exc:
            messagebox.showerror('操作未执行', describe_error(exc, self.store.root), parent=self.root)

    def log(self, msg):
        self.events.put(('log', msg))

    def task(self, fn, done=lambda _: None, *, migration=False, starting='正在处理…'):
        if self.busy:
            return
        self.busy = True
        self.running_migration = migration
        self.cancel.clear()
        for b in self.buttons:
            b.configure(state='disabled')
        self.pause_button.configure(state='normal')
        self.status.set(starting)
        def worker():
            try:
                value = fn()
                self.events.put(('done', (done, value)))
            except Exception as exc:
                # SDK exceptions can contain sensitive request configs; show only safe errors.
                msg = describe_error(exc, self.store.root)
                self.events.put(('error', msg))
        threading.Thread(target=worker, daemon=True).start()

    def pause(self):
        self.cancel.set()
        self.pause_button.configure(state='disabled')
        self.status.set('正在暂停，等待当前请求结束…')

    def pump(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.status.set(value)
                elif kind == 'qr':
                    self.show_qr(value)
                elif kind == 'connection':
                    self.set_connection(*value)
                elif kind == 'migration':
                    self.render_progress(value)
                else:
                    was_migrating = self.running_migration
                    self.busy = False
                    self.running_migration = False
                    self.pause_button.configure(state='disabled')
                    for b in self.buttons:
                        b.configure(state='normal')
                    self.refresh()
                    if kind == 'error':
                        if was_migrating:
                            self.progress_text.set('本轮未完成 · 请重新预览核对')
                        self.status.set('已停止：' + value)
                        messagebox.showerror('操作已停止', value, parent=self.root)
                    else:
                        callback, result = value
                        self.status.set('操作完成。')
                        self.guard(lambda: callback(result))
        except queue.Empty:
            pass
        self.pump_id = self.root.after(100, self.pump)

    def startup_check(self):
        self.startup_id = None
        self.check_connections()

    def cancel_timers(self, event):
        if event.widget is self.root:
            for timer in (self.startup_id, self.pump_id):
                if timer:
                    self.root.after_cancel(timer)

    def set_connection(self, key, state, account=None):
        self.account_ids[key] = account
        labels = {'connected': ('已连接', 'Good'), 'checking': ('正在核验…', 'Muted'),
                  'missing': ('未连接', 'Muted'), 'auth': ('授权失效，请扫码', 'Warn'),
                  'unknown': ('暂未核验，可重查', 'Warn')}
        text, style = labels[state]
        self.account_labels[key].configure(text=text, style=f'{style}.TLabel')
        if not self.busy:
            self.refresh()

    def check_connections(self):
        checks = []
        if self.qq.credential:
            checks.append(('qq', lambda: asyncio.run(self.qq.check_account())))
        if self.ne.cookie:
            checks.append(('netease', lambda: self.ne.check_account(quick=True)))
        if not checks:
            return
        def work():
            for key, check in checks:
                if self.cancel.is_set():
                    self.events.put(('connection', (key, 'unknown')))
                    continue
                try:
                    result = ('connected', check())
                except RequestFailure as exc:
                    result = ('auth' if exc.kind == 'auth' else 'unknown',)
                except Exception:
                    result = ('unknown',)
                self.events.put(('connection', (key, *result)))
        self.task(work, lambda _: self.status.set('已检查保存的授权。'), starting='正在检查保存的授权…')
        for key, _ in checks:
            self.set_connection(key, 'checking')

    def connect_ne(self):
        try:
            account = self.ne.connect()
        except RequestFailure as exc:
            if exc.kind == 'auth':
                self.events.put(('connection', ('netease', 'auth')))
            raise
        self.events.put(('connection', ('netease', 'connected', account)))
        return account

    def show_qr(self, data):
        from PIL import Image, ImageTk
        window = tk.Toplevel(self.root)
        window.title('扫码授权 · 关闭窗口取消等待')
        image = Image.open(io.BytesIO(data)).convert('RGB').resize(self.display.dims(300, 300), Image.Resampling.NEAREST)
        window.photo = ImageTk.PhotoImage(image)
        ttk.Label(window, image=window.photo).pack(padx=20, pady=20)
        ttk.Label(window, text='请使用对应平台的手机应用扫码并自行完成安全验证').pack(padx=15, pady=10)
        window.protocol('WM_DELETE_WINDOW', lambda: (self.cancel.set(), window.destroy()))
        def watch():
            if not self.busy:
                window.destroy()
            elif window.winfo_exists():
                window.after(300, watch)
        window.after(300, watch)

    def save_auth(self):
        self.vault.save({'qq': self.qq.credential, 'netease': self.ne.cookie})

    def login_qq(self):
        if not messagebox.askokcancel('QQ 音乐登录授权', '将请求 QQ 登录二维码。由你扫码授权；不会写入歌曲收藏。是否继续？'):
            return
        def work():
            credential = asyncio.run(self.qq.login(lambda b: self.events.put(('qr', b)), self.cancel.is_set))
            self.save_auth()
            return str(credential['musicid'])
        self.task(work, lambda account: self.set_connection('qq', 'connected', account))

    def login_ne(self):
        if not messagebox.askokcancel('网易云登录授权', '将请求网易云登录二维码。由你扫码授权；不会写入歌曲收藏。是否继续？'):
            return
        def work():
            import qrcode
            key = self.ne.call('login_qr_key')['data']['unikey']
            url = self.ne.call('login_qr_create', key=key)['data']['qrurl']
            buf = io.BytesIO()
            qrcode.make(url).save(buf, format='PNG')
            self.events.put(('qr', buf.getvalue()))
            for _ in range(90):
                if self.cancel.is_set():
                    raise SafetyError('已取消登录')
                time.sleep(2)
                data = self.ne.call('login_qr_check', key=key)
                if data['code'] == 803:
                    self.ne.cookie = data['cookie']
                    self.ne.account = self.ne.playlist = ''
                    account = self.ne.check_account()
                    self.save_auth()
                    return account
                if data['code'] == 800:
                    raise SafetyError('二维码过期，请重试')
            raise SafetyError('扫码超时')
        self.task(work, lambda account: self.set_connection('netease', 'connected', account))

    def clear_auth(self):
        if messagebox.askokcancel('清除本地授权', '删除此工具保存的加密授权。备份、报告保留；不会取消平台收藏，也不会撤销其他设备登录。继续？'):
            self.vault.clear()
            self.qq.credential, self.ne.cookie = None, ''
            self.ne.account = self.ne.playlist = ''
            for key in ('evidence', 'plan', 'resume_order'):
                self.store.data.pop(key, None)
            self.store.save()
            for key in self.account_labels:
                self.set_connection(key, 'missing')
            self.status.set('两端授权已清除。')

    def read(self):
        def work():
            source = asyncio.run(self.qq.read(self.log, self.cancel.is_set))
            self.events.put(('connection', ('qq', 'connected', str(self.qq.credential.get('musicid')))))
            raw_source = [asdict(s) for s in source]
            changed = digest(raw_source) != digest(self.store.data.get('source', [])) or str(self.qq.credential.get('musicid')) != self.store.data.get('source_account')
            if changed:
                self.store.data.update(source=raw_source, source_confirmed=False,
                                       source_order='接口原序（两遍一致，尚需界面对照）', matches={}, source_account=str(self.qq.credential.get('musicid')))
                self.store.data.pop('plan', None)
            self.store.save()
            # Source backup remains available even when the target is unavailable.
            backup(self.store.root / 'backups' / ('source-' + str(time.time_ns())), 'qq', source,
                   order=self.store.data.get('source_order'), complete=True)
            self.connect_ne()
            target = self.ne.read_likes()
            self.store.data['target'] = [asdict(s) for s in target]
            self.store.record_target(self.ne.account, ids(target))
            self.store.save()
            self.store.snapshots(source, target, self.ne.account)
            return len(source), len(target)
        self.task(work, lambda counts: self.status.set(f'已读取并备份：QQ {counts[0]} 首，网易云 {counts[1]} 首。'))

    def reverse(self):
        source = self.store.data.get('source', [])
        if not source:
            raise SafetyError('请先读取来源')
        source.reverse()
        for i, s in enumerate(source):
            s['position'] = i + 1
        self.store.data['source_confirmed'] = False
        self.store.data['source_order'] = '反转接口原序，尚需界面对照'
        self.store.data.pop('plan', None)
        self.store.save()
        self.refresh()

    def confirm_source(self):
        source = self.store.data.get('source', [])
        if not source:
            raise SafetyError('请先读取来源')
        if not messagebox.askokcancel('对照 QQ 当前界面', f'共 {len(source)} 首。\n首曲：{source[0]["title"]}\n末曲：{source[-1]["title"]}\n\n请对照 QQ 的实际列表检查首尾和中间各段。若不同，请勿确认。'):
            return
        rule = simpledialog.askstring('记录界面排序', '请输入已在 QQ 界面确认的排序规则，例如：默认收藏顺序 / 添加时间倒序')
        if not rule:
            return
        self.store.data.update(source_confirmed=True, source_order=rule)
        self.store.save()
        backup(self.store.root / 'confirmed-source', 'qq', [Song.load(s) for s in source], order=rule)
        self.refresh()

    def match(self):
        if not self.store.data.get('source_confirmed'):
            raise SafetyError('请先确认来源顺序')
        def work():
            self.connect_ne()
            return match_pending(self.ne, self.store, self.log, self.cancel.is_set)
        self.task(work)

    def refresh(self):
        selected, view = self.tree.selection(), self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        data = self.store.data
        source, matches = data.get('source', []), data.get('matches', {})
        account = self.account_ids['netease']
        state = data.get('target_state', {})
        report = data.get('report', {})
        report_plan = report.get('plan', {})
        live, migrated = set(), set()
        if account and state.get('account') == account:
            live, migrated = set(state['ids']), set(state['migrated'])
        elif account and report_plan.get('account') == account and report.get('final_verified'):
            # Older versions recorded verification, but not a separate target snapshot.
            live = set(report_plan.get('desired', [])) - set(report['verification']['missing'])
            migrated = live.intersection(report_plan.get('operations', []))
        blocked = None
        if account and report_plan.get('account') == account and report.get('status') in ('paused', 'unverified'):
            blocked = report.get('blocked_song') or (report.get('pending_write') or {}).get('id')
        self.blocked_source, self.target_rows = None, {}
        skipped = pending = chosen_count = unsearched = done = 0
        for i, s in enumerate(source):
            m = matches.get(s['id'], {})
            chosen = m.get('chosen')
            tag = 'stripe' if i % 2 else ''
            if chosen:
                chosen_count += 1
                target = chosen['id']
                self.target_rows.setdefault(target, []).append(s['id'])
                if target in live:
                    prefix, tag = ('已迁移' if target in migrated else '已在网易云'), 'muted'
                    done += 1
                elif target == blocked:
                    prefix, tag = ('写入失败 500' if report.get('blocked_song') else '写入待核对'), 'blocked'
                    self.blocked_source = s['id']
                else:
                    prefix = '自动确认' if m.get('method') == 'auto' else '人工确认'
                label = f'{prefix} · {chosen["title"]} / {"、".join(chosen["artists"])}'
            elif m.get('method') == 'skipped':
                label, tag = '不迁移', 'muted'
                skipped += 1
            elif m:
                label = '搜索失败' if m.get('error') else '待确认'
                pending += 1
            else:
                label = '待搜索'
                unsearched += 1
            self.tree.insert('', 'end', iid=s['id'], tags=(tag,) if tag else (),
                             values=(s['position'], s['title'], '、'.join(s['artists']), s['album'], label))
        self.tree.yview_moveto(view[0] if view else 0)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.review_button.configure(text=f'连续确认（{pending}）')
        self.song_actions.schedule_layout()
        self.counts.set(f'共 {len(source)} 首 · 已匹配 {chosen_count} · 待确认 {pending} · 待搜索 {unsearched} · 不迁移 {skipped} · 已在网易云 {done}')
        self.locate_button.configure(state='normal' if self.blocked_source and not self.busy else 'disabled')
        self.update_step()

    def locate_blocked(self):
        if not self.blocked_source:
            return
        self.tabs.select(0)
        self.tree.selection_set(self.blocked_source)
        self.tree.focus(self.blocked_source)
        self.tree.see(self.blocked_source)
        self.tree.focus_set()

    def update_step(self):
        data = self.store.data
        source = data.get('source', [])
        matches = data.get('matches', {})
        account = self.account_ids['netease']
        evidence = data.get('evidence', {})
        report = data.get('report', {})
        mode = self.mode.get()
        if not self.account_ids['qq'] or not account:
            label = '01  连接账号   ·   扫码或检查已保存的授权'
        elif not source:
            label = '02  读取收藏   ·   点击「读取并备份两端」'
        elif not data.get('source_confirmed'):
            label = '02  确认顺序   ·   对照 QQ 列表，点击「确认源顺序」'
        elif self.blocked_source:
            label = '05  处理暂停曲目   ·   定位出错歌曲，核对候选后重新预览续传'
        elif self.engine().can_resume(mode, account=account):
            label = '05  继续迁移   ·   直接「迁移预览」→「确认迁移 / 续传」'
        elif any(s['id'] not in matches for s in source):
            label = '03  搜索匹配   ·   点击「搜索匹配 / 续查」'
        elif any(not m.get('chosen') and m.get('method') != 'skipped' for m in matches.values()):
            label = '03  核对匹配   ·   用「连续确认」检查剩余候选'
        elif (report.get('status') == 'finished' and report.get('final_verified')
              and report.get('plan', {}).get('account') == account
              and report['plan'].get('source_hash') == digest(source)
              and report['plan'].get('mapping_hash') == digest(matches)
              and report['plan'].get('mode') == mode):
            label = '05  查看核验结果   ·   在「预览与报告」检查数量和顺序'
        elif (evidence.get('account') != account or time.time() - evidence.get('time', 0) > 86400
              or (mode == 'sync' and not evidence.get('reorder'))):
            label = '04  首次排序实测   ·   验证新增收藏如何排列，再做迁移预览'
        else:
            label = '04  预览并迁移   ·   先核对本次变更，再确认执行'
        self.step.set(label)

    def render_progress(self, update):
        self.progress.configure(maximum=max(1, update.total), value=update.confirmed)
        count = f'已确认 {update.confirmed} / {update.total} 首'
        if update.phase == 'writing':
            self.progress_text.set(f'第 {update.confirmed + 1} / {update.total} 首 · {count}')
            self.status.set('正在写入并核对当前歌曲…')
        elif update.phase == 'checking':
            self.progress_text.set(f'检查目标列表 · {count}')
        elif update.phase == 'verifying':
            self.progress_text.set(f'最终核验 · {count}')
            self.status.set('正在核验最终红心列表和顺序…')
        else:
            self.progress_text.set(f'迁移中 · {count}')
        if update.verified_song:
            for source_id in self.target_rows.get(update.verified_song, []):
                values = list(self.tree.item(source_id, 'values'))
                values[-1] = '已迁移 · ' + values[-1].partition(' · ')[2]
                self.tree.item(source_id, values=values, tags=('muted',))

    def choose(self):
        if self.busy:
            return
        selected = self.tree.selection()
        if not selected:
            raise SafetyError('请先选中一首来源歌曲')
        sid = selected[0]
        m = self.store.data.get('matches', {}).get(sid)
        if not m:
            raise SafetyError('请先搜索匹配')
        self.review_all(start_sid=sid)

    def review_all(self, start_sid=None):
        if self.busy:
            return
        window = getattr(self, 'review_window', None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        api = Netease(self.ne.bridge_dir, self.ne.cookie, self.ne.node,
                      diagnostic_path=self.store.root / 'diagnostics.jsonl')
        self.review_window = ReviewWindow(self.root, self.store, self.refresh, api=api, start_sid=start_sid)

    def engine(self):
        return Engine(self.ne, self.store, self.log, self.cancel.is_set,
                      on_progress=lambda update: self.events.put(('migration', update)))

    def probe(self):
        state = self.store.data.get('target_state', {})
        account = self.account_ids['netease']
        existing = set(state['ids']) if account and state.get('account') == account else {
            x['id'] for x in self.store.data.get('target', [])}
        candidates = list({s.id: s for s in self.engine().selected() if s.id not in existing}.values())[:2]
        if len(candidates) != 2:
            raise SafetyError('请先读取两端，并确认至少两首尚未喜欢的匹配歌曲')
        text = '\n'.join(f'{s.title} / {"、".join(s.artists)}（{s.id}）' for s in candidates)
        reorder = messagebox.askyesnocancel('排序实测方式', '是否同时测试系统喜欢列表重排？\n\n是：新增两曲后交换这两曲位置（其余顺序保持）。\n否：仅测试新增置顶/置底。\n取消：不写入。')
        if reorder is None:
            return
        if not messagebox.askokcancel('授权首次真实写入', f'将实时重读并备份，然后真实喜欢以下两曲：\n{text}\n\n歌曲会保留在喜欢列表中；不会取消原有喜欢。' + ('\n还会调用重排接口交换这两曲。' if reorder else '') + '\n是否授权？'):
            return
        def work():
            self.connect_ne()
            # Bind consent to exactly the previewed pair.
            live = self.ne.read_likes()
            actual = list(dict.fromkeys(s.id for s in self.engine().selected() if s.id not in {x.id for x in live}))[:2]
            if actual != [s.id for s in candidates]:
                raise SafetyError('实测歌曲与授权预览不一致，请重读后重新选择')
            result = self.engine().probe(True, reorder, [s.id for s in candidates])
            self.store.data['target'] = [asdict(s) for s in self.ne.read_likes()]
            self.store.save()
            return result
        self.task(work, self.show_probe)

    def set_details(self, heading, text, *, select=True):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('end', heading + '\n', 'heading')
        self.details.insert('end', text)
        self.details.configure(state='disabled')
        self.details.yview_moveto(0)
        if select:
            self.tabs.select(1)

    def show_probe(self, evidence):
        direction = '置顶' if evidence['direction'] == 'prepend' else '置底'
        reorder = '已验证' if evidence['reorder'] else '未测试'
        self.set_details('排序实测完成', f'新增喜欢会{direction}。系统列表重排：{reorder}。\n'
                         '实测的两首歌已加入网易云喜欢。下一步点击「迁移预览」。')

    def show_preview(self, plan):
        ops = plan['operations']
        mode = '安全合并' if plan['mode'] == 'merge' else '严格同步'
        already = len(plan['desired']) - len(ops)
        lines = [f'网易云账号：{plan["account"]}    模式：{mode}',
                 f'已匹配 {len(plan["desired"])} 首 · 已在网易云 {already} 首 · 本轮新增 {len(ops)} 首',
                 '预览不写入。核对下面的匹配后，点击「确认迁移 / 续传」。\n']
        order = {sid: i for i, sid in enumerate(ops, 1)}
        for row in plan['preview_songs']:
            target = row['target']
            action = f'本轮第 {order[target["id"]]} 首' if target['id'] in order else '已喜欢，保留'
            lines.append(f'{row["source_position"]:>3}.  {row["source_title"]}  →  {target["title"]} / {"、".join(target["artists"])}  ·  {action}')
        self.set_details('迁移预览', '\n'.join(lines))

    def preview(self):
        self.check_source_account()
        mode = self.mode.get()
        def work():
            engine = self.engine()
            engine.read_with_cooldown(self.connect_ne)
            return engine.plan(mode)
        self.task(work, self.show_preview)

    def check_source_account(self):
        current = self.account_ids['qq']
        previous = self.store.data.get('source_account')
        if current and previous and current != previous:
            raise SafetyError('QQ 账号已改变，请先「读取并备份两端」并重新确认来源。')

    def migrate(self):
        self.check_source_account()
        p = self.store.data.get('plan')
        if not p:
            raise SafetyError('请先生成迁移预览')
        if p['mode'] != self.mode.get():
            raise SafetyError('迁移模式改变，请重新预览')
        mode = '安全合并' if p['mode'] == 'merge' else '严格同步'
        if not messagebox.askokcancel('确认迁移', f'网易云账号：{p["account"]}\n模式：{mode}\n现有 {len(p["before"])} 首，新增 {len(p["operations"])} 首\n已匹配 {len(p["desired"])} / 来源 {len(self.store.data["source"])} 首\n取消喜欢：0 首\n重排：{"完整目标列表" if p["mode"] == "sync" else "不重排旧歌曲"}\n\n每次新增至少间隔 {MIGRATION_WRITE_INTERVAL} 秒。写入失败时会在该曲暂停。\n确认执行？', parent=self.root):
            return
        def work():
            engine = self.engine()
            engine.read_with_cooldown(self.connect_ne)
            return engine.run(True)
        self.progress.configure(maximum=max(1, len(p['operations'])), value=0)
        self.progress_text.set(f'准备迁移 · 已确认 0 / {len(p["operations"])} 首')
        self.task(work, self.show_migration_report, migration=True, starting='正在核对网易云账号与当前收藏…')

    def show_migration_report(self, report):
        planned = len(report['plan']['operations'])
        count = report['newly_confirmed']
        status = {'finished': '本轮结束', 'paused': '已暂停', 'unverified': '待核验'}.get(report['status'], '本轮结束')
        self.progress.configure(maximum=max(1, planned), value=count)
        self.progress_text.set(f'{status} · 已确认 {count} / {planned} 首')
        verified = '已完成' if report['final_verified'] else '待完成'
        details = [f'本轮确认新增 {count} / {planned} 首。', f'最终列表核验：{verified}。']
        if report['final_verified']:
            check = report['verification']
            details.append(f'匹配结果：网易云找到 {check["confirmed_liked"]} / {report["matched"]} 首。')
            details.append(f'全部匹配歌曲的相对顺序：{"通过" if check["relative_order_ok"] else "有差异"}。')
            details.append(f'本轮新增歌曲的相对顺序：{"通过" if report["new_songs_order"]["relative_order_ok"] else "有差异"}。')
            if report['unmatched']:
                details.append(f'未迁移或未匹配：{len(report["unmatched"])} 首。')
        skipped = report.get('skipped_songs', [])
        if skipped:
            details.append(f'有 {len(skipped)} 首曾暂时跳过；曲目和最终状态见报告。')
        blocked = report.get('blocked_song')
        if blocked:
            title = next((row['source_title'] for row in report['plan']['preview_songs']
                          if row['target']['id'] == blocked), blocked)
            details.append(f'\n暂停曲目：《{title}》（网易云 ID {blocked}）。')
            details.append('在「歌曲与匹配」定位出错歌曲，点「人工选候选」更换或补充链接。具体步骤见「常见问题」。')
        if report['pending_write']:
            details.append('有一首待核对，重新预览时会检查其实际喜欢状态。')
        if report['errors']:
            details.append(report['errors'][-1])
        if report['status'] != 'finished':
            details.append('\n继续同一任务：点「迁移预览」→「确认迁移 / 续传」，不用再次排序实测。')
        details.append('\n可通过「打开备份与报告」查看完整报告。')
        self.set_details('迁移核验结果', '\n'.join(details))
        self.status.set('已保存本轮报告。' if report['status'] == 'finished'
                        else '已暂停并保存进度。处理问题后重新预览，再确认续传。')

    def close(self):
        if self.busy:
            self.cancel.set()
            messagebox.showinfo('正在安全停止', '已请求暂停。请等待当前网络操作完成后再关闭窗口。')
            return
        self.root.destroy()


def main(data_dir=None):
    root = tk.Tk()
    # Windows mutex prevents concurrent writers; the OS releases it after crashes.
    handle = None
    if os.name == 'nt':
        import ctypes
        from .core import digest
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel.CreateMutexW(None, False, 'Local\\MusicBridge-' + digest(str(app_root())))
        if kernel.GetLastError() == 183:
            messagebox.showerror('工具已运行', '此目录已打开一个迁移窗口，请使用原窗口。')
            root.destroy()
            return
    App(root, data_dir)
    root.mainloop()
    if handle:
        ctypes.windll.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.CloseHandle(handle)
