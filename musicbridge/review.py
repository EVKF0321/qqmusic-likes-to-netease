"""Sequential manual review of cached matches; no account/network operations."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
import queue
import threading

from .core import SafetyError, Song, rank
from .diagnostics import describe_error
from .titles import search_title
from .display import Display
from .ui import BG, FONT, Flow, hint, wrap_to_width


class ReviewQueue:
    def __init__(self, store, start_sid=None):
        self.store = store
        matches = store.data.get('matches', {})
        self.sources = {s['id']: s for s in store.data.get('source', [])}
        self.remaining = [s['id'] for s in store.data.get('source', [])
                          if s['id'] in matches and ((not matches[s['id']].get('chosen')
                          and matches[s['id']].get('method') != 'skipped') or s['id'] == start_sid)]
        self.total = len(self.remaining)
        self.confirmed = self.skipped = 0
        self.index = self.remaining.index(start_sid) if start_sid in self.remaining else 0

    def merge_candidates(self, sid, songs):
        if sid != self.current:
            raise SafetyError('歌曲已切换，未将搜索结果添加到其他歌曲')
        match = self.store.data['matches'][sid]
        before = copy.deepcopy(match)
        songs = list(songs)
        original = [Song.load(c['song']) for c in match.get('candidates', [])]
        ranked = rank(Song.load(self.sources[sid]), original + songs)
        previous_plan = self.store.data.pop('plan', None)
        match['candidates'] = ranked['candidates']
        # Manual search only supplements the list. Explicit confirmation is still needed.
        match['manual_candidate_ids'] = list(dict.fromkeys(match.get('manual_candidate_ids', []) + [s.id for s in songs]))
        try:
            self.store.save()
        except Exception:
            self.store.data['matches'][sid] = before
            if previous_plan is not None:
                self.store.data['plan'] = previous_plan
            raise
        return len(match['candidates']) - len(original)

    @property
    def current(self):
        return self.remaining[self.index] if self.remaining else None

    def select(self, sid):
        if sid not in self.remaining:
            raise SafetyError('该歌曲已不在待确认列表中')
        self.index = self.remaining.index(sid)

    def defer(self):
        if self.remaining:
            self.index = (self.index + 1) % len(self.remaining)

    def decide(self, sid, candidate_index=None, skip=False):
        if sid != self.current or sid is None:
            raise SafetyError('当前歌曲已切换，请重新选择候选')
        match = self.store.data['matches'][sid]
        chosen = None
        if not skip:
            candidates = match.get('candidates', [])
            if not isinstance(candidate_index, int) or candidate_index < 0 or candidate_index >= len(candidates):
                raise SafetyError('请先选中一个候选结果')
            chosen = copy.deepcopy(candidates[candidate_index]['song'])
            Song.load(chosen)
        old = copy.deepcopy(match)
        old_plan = self.store.data.pop('plan', None)
        match['chosen'] = chosen
        match['method'] = 'skipped' if skip else 'manual'
        # A manual choice resolves the match decision; keep metadata warnings for audit.
        match['error'] = ''
        try:
            self.store.save()
        except Exception:
            self.store.data['matches'][sid] = old
            if old_plan is not None:
                self.store.data['plan'] = old_plan
            raise
        self.remaining.pop(self.index)
        self.index = self.index % len(self.remaining) if self.remaining else 0
        if skip:
            self.skipped += 1
        else:
            self.confirmed += 1


class ReviewWindow(tk.Toplevel):
    def __init__(self, parent, store, on_close=lambda: None, api=None, start_sid=None):
        super().__init__(parent)
        self.store, self.on_close = store, on_close
        self.queue = ReviewQueue(store, start_sid)
        self.api, self.network_busy, self.closing = api, False, False
        self.events, self.stop_event = queue.Queue(), threading.Event()
        self.more_available, self.query_text, self.query_sid, self.next_offset = False, '', None, 0
        if api is not None:
            api.log = lambda message: self.events.put(('log', message))
            api.stopped = self.stop_event.is_set
        self.rendering = False
        display = Display.for_widget(self)
        self.title('人工确认歌曲')
        self.configure(bg=BG)
        display.configure_window(self, (1100, 740), (650, 510))
        self.transient(parent)
        self.protocol('WM_DELETE_WINDOW', self.close)
        outer = ttk.Frame(self, style='Page.TFrame', padding=display.px(12))
        outer.pack(fill='both', expand=True)
        self.progress = tk.StringVar()
        ttk.Label(outer, textvariable=self.progress, style='Subtitle.TLabel', font=(FONT, 13, 'bold')).pack(anchor='w', pady=(0, 6))
        actions = Flow(outer)
        actions.pack(side='bottom', fill='x', pady=(9, 0))
        panels = ttk.Panedwindow(outer, orient='horizontal')
        panels.pack(fill='both', expand=True)
        left = ttk.Frame(panels, padding=display.px(6))
        right = ttk.Frame(panels, padding=display.dims(7, 6))
        panels.add(left, weight=1)
        panels.add(right, weight=3)
        def fit_sash(event):
            # Keep enough room for the search and link fields when the dialog shrinks.
            limit = max(display.px(115), event.width - display.px(410))
            position = panels.sashpos(0)
            if position > limit or position < display.px(115):
                panels.sashpos(0, min(limit, max(display.px(115), event.width // 4)))
        panels.bind('<Configure>', fit_sash, add='+')
        self.pending = ttk.Treeview(left, columns=('pos', 'title', 'status'), show='headings', selectmode='browse', height=4)
        for key, title, width in [('pos', '序号', 45), ('title', '来源歌曲', 145), ('status', '状态', 70)]:
            self.pending.heading(key, text=title)
            self.pending.column(key, width=display.px(width), minwidth=display.px(33))
        scrollbar = ttk.Scrollbar(left, command=self.pending.yview)
        self.pending.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.pending.pack(fill='both', expand=True)
        self.pending.bind('<<TreeviewSelect>>', self.select_source)
        self.source_text = tk.StringVar()
        source_label = ttk.Label(right, textvariable=self.source_text, justify='left', font=(FONT, 11, 'bold'))
        source_label.pack(fill='x', pady=(0, 5))
        wrap_to_width(source_label)
        self.note = tk.StringVar()
        note_label = ttk.Label(right, textvariable=self.note, style='Warn.TLabel')
        note_label.pack(fill='x', pady=(0, 5))
        wrap_to_width(note_label)
        manual = ttk.LabelFrame(right, text='搜索或粘贴网易云单曲链接', padding=display.px(6))
        manual.pack(fill='x', pady=(0, 6))
        manual.columnconfigure(1, weight=1)
        ttk.Label(manual, text='关键词').grid(row=0, column=0, padx=(0, 6), pady=3)
        self.keywords = tk.StringVar()
        self.keyword_entry = ttk.Entry(manual, textvariable=self.keywords, width=14)
        self.keyword_entry.grid(row=0, column=1, sticky='ew', pady=3)
        self.search_button = ttk.Button(manual, text='自行搜索', command=self.search_custom)
        self.search_button.grid(row=0, column=2, padx=4)
        self.more_button = ttk.Button(manual, text='下一页结果', command=lambda: self.search_custom(more=True))
        self.more_button.grid(row=0, column=3)
        ttk.Label(manual, text='链接 / ID').grid(row=1, column=0, padx=(0, 6), pady=3)
        self.reference = tk.StringVar()
        self.reference_entry = ttk.Entry(manual, textvariable=self.reference, width=14)
        self.reference_entry.grid(row=1, column=1, sticky='ew', pady=3)
        self.reference_button = ttk.Button(manual, text='读取并补充候选', command=self.load_reference)
        self.reference_button.grid(row=1, column=2, columnspan=2, padx=4, sticky='ew')
        self.manual_status = tk.StringVar(value='补充候选后仍需点击确认，不会立即收藏。')
        manual_label = ttk.Label(manual, textvariable=self.manual_status, style='Muted.TLabel')
        manual_label.grid(row=2, column=0, columnspan=4, sticky='ew', pady=3)
        wrap_to_width(manual_label)
        self.keyword_entry.bind('<Return>', lambda _: self.search_custom() or 'break')
        self.reference_entry.bind('<Return>', lambda _: self.load_reference() or 'break')
        candidate_frame = ttk.Frame(right)
        candidate_frame.pack(fill='both', expand=True)
        self.candidates = ttk.Treeview(candidate_frame, columns=('score', 'title', 'artists', 'album', 'seconds', 'why'), show='headings', selectmode='browse', height=4)
        for key, title, width in [('score', '评分', 65), ('title', '候选歌名', 180), ('artists', '歌手', 145), ('album', '专辑', 165), ('seconds', '秒', 65), ('why', '提示', 200)]:
            self.candidates.heading(key, text=title)
            self.candidates.column(key, width=display.px(width), minwidth=display.px(45))
        vbar = ttk.Scrollbar(candidate_frame, command=self.candidates.yview)
        hbar = ttk.Scrollbar(candidate_frame, orient='horizontal', command=self.candidates.xview)
        self.candidates.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        candidate_frame.rowconfigure(0, weight=1)
        candidate_frame.columnconfigure(0, weight=1)
        self.candidates.grid(row=0, column=0, sticky='nsew')
        vbar.grid(row=0, column=1, sticky='ns')
        hbar.grid(row=1, column=0, sticky='ew')
        self.candidates.bind('<Double-1>', lambda _: self.open_candidate())
        self.confirm_button = actions.add(ttk.Button(actions, text='确认选中 → 下一首', command=self.confirm, style='Accent.TButton'))
        hint(self.confirm_button, '保存候选，自动切到下一首。也可按 Enter 确认。')
        self.skip_button = actions.add(ttk.Button(actions, text='不迁移此曲', command=lambda: self.confirm(skip=True)))
        self.defer_button = actions.add(ttk.Button(actions, text='稍后处理', command=self.defer))
        actions.add(ttk.Button(actions, text='打开候选歌曲页', command=self.open_candidate))
        actions.add(ttk.Button(actions, text='保存并关闭', command=self.close))
        self.bind('<Return>', lambda _: self.confirm())
        self.bind('<Escape>', lambda _: self.close())
        self.render()
        self.grab_set()
        self.poll_id = self.after(100, self.poll_network)

    def render(self):
        self.rendering = True
        try:
            self.pending.delete(*self.pending.get_children())
            for sid in self.queue.remaining:
                s = self.queue.sources[sid]
                m = self.store.data['matches'][sid]
                state = '搜索失败' if m.get('error') else '有候选' if m.get('candidates') else '无候选'
                self.pending.insert('', 'end', iid=sid, values=(s['position'], s['title'], state))
            self.progress.set(f'剩余 {len(self.queue.remaining)} 首  ·  本次已确认 {self.queue.confirmed} 首  ·  已跳过 {self.queue.skipped} 首')
            if self.queue.current:
                self.pending.selection_set(self.queue.current)
                self.pending.see(self.queue.current)
            self.show_candidates()
        finally:
            self.rendering = False

    def select_source(self, _=None):
        if self.rendering or not self.pending.selection():
            return
        sid = self.pending.selection()[0]
        if self.network_busy:
            if self.queue.current and sid != self.queue.current:
                self.pending.selection_set(self.queue.current)
            return
        if sid in self.queue.remaining and sid != self.queue.current:
            self.queue.select(sid)
            self.show_candidates()

    def show_candidates(self, reset_manual=True, preferred_id=None):
        self.candidates.delete(*self.candidates.get_children())
        sid = self.queue.current
        if reset_manual:
            self.more_available, self.query_sid = False, None
            self.reference.set('')
            self.manual_status.set('补充候选后仍需点击确认，不会立即收藏。')
        self.search_button.configure(state='normal' if sid and self.api is not None else 'disabled')
        self.reference_button.configure(state='normal' if sid and self.api is not None else 'disabled')
        self.more_button.configure(state='normal' if sid and self.more_available else 'disabled')
        for button in (self.confirm_button, self.skip_button, self.defer_button):
            button.configure(state='normal' if sid else 'disabled')
        if not sid:
            self.source_text.set('待确认歌曲已全部处理完成。')
            self.note.set('所有决定均已保存，可以关闭此窗口。')
            return
        source = self.queue.sources[sid]
        if reset_manual:
            self.keywords.set(search_title(source['title']) + ' ' + ' '.join(source['artists']))
        match = self.store.data['matches'][sid]
        self.source_text.set(f'QQ 原曲 #{source["position"]}：{source["title"]}\n歌手：{" / ".join(source["artists"])}\n专辑：{source["album"] or "未知"}    时长：{source["seconds"] or "未知"} 秒')
        warnings = []
        if match.get('metadata_issues'):
            warnings.append(f'{len(match["metadata_issues"])} 条元数据异常候选已排除，请核对正常候选')
        if match.get('error'):
            warnings.append(match['error'])
        rows = match.get('candidates', [])
        if not rows:
            warnings.append('当前没有候选，可在下方自行搜索或粘贴歌曲链接 / ID')
            self.confirm_button.configure(state='disabled')
        self.note.set('；'.join(warnings) or '候选按评分排序。请对照原曲的歌手、专辑、时长和版本后确认。')
        for i, candidate in enumerate(rows):
            s = candidate['song']
            self.candidates.insert('', 'end', iid=str(i), values=(candidate['score'], s['title'], ' / '.join(s['artists']), s['album'], s['seconds'], '、'.join(candidate.get('reasons', []))))
        if rows:
            chosen = next((str(i) for i, c in enumerate(rows) if c['song']['id'] == preferred_id), '0')
            self.candidates.selection_set(chosen)
            self.candidates.focus(chosen)
            self.candidates.see(chosen)

    def confirm(self, skip=False):
        if self.network_busy:
            return
        sid = self.queue.current
        if sid is None:
            return
        selected = self.candidates.selection()
        if not skip and not selected:
            self.note.set('请先选中一个候选结果，或者选择稍后处理。')
            return
        try:
            self.queue.decide(sid, int(selected[0]) if selected else None, skip)
            self.render()
        except Exception as exc:
            messagebox.showerror('未保存此决定', describe_error(exc, self.store.root), parent=self)

    def defer(self):
        if self.network_busy:
            return
        self.queue.defer()
        self.render()

    def open_candidate(self):
        selected = self.candidates.selection()
        if not self.queue.current or not selected:
            return
        candidate = self.store.data['matches'][self.queue.current]['candidates'][int(selected[0])]['song']
        if candidate.get('url'):
            webbrowser.open(candidate['url'])

    def start_network(self, operation, kind, sid):
        if self.network_busy or self.api is None or not sid:
            return
        self.network_busy = True
        self.stop_event.clear()
        for widget in (self.confirm_button, self.skip_button, self.defer_button, self.search_button, self.more_button, self.reference_button, self.keyword_entry, self.reference_entry):
            widget.configure(state='disabled')
        self.manual_status.set('正在读取网易云结果… 可关闭窗口请求取消。')
        def worker():
            try:
                self.events.put(('done', (sid, kind, operation())))
            except Exception as exc:
                self.events.put(('error', describe_error(exc, self.store.root)))
        threading.Thread(target=worker, daemon=True).start()

    def search_custom(self, more=False):
        if self.network_busy or self.api is None or not self.queue.current:
            return
        if more:
            if not self.more_available or self.query_sid != self.queue.current:
                return
            query, offset = self.query_text, self.next_offset
        else:
            query, offset = self.keywords.get().strip(), 0
            if not query:
                self.manual_status.set('请输入搜索关键词，可自由修改歌名或歌手。')
                return
            self.more_available = False
        self.query_text, self.query_sid = query, self.queue.current
        self.start_network(lambda: self.api.search_keywords(query, offset), 'keywords', self.queue.current)

    def load_reference(self):
        if self.network_busy or self.api is None or not self.queue.current:
            return
        value = self.reference.get().strip()
        if not value:
            self.manual_status.set('请粘贴网易云歌曲链接或纯数字 ID。')
            return
        self.start_network(lambda: [self.api.song_by_reference(value)], 'reference', self.queue.current)

    def poll_network(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    self.manual_status.set(payload)
                    continue
                self.network_busy = False
                if self.closing:
                    self.finish_close()
                    return
                for entry in (self.keyword_entry, self.reference_entry):
                    entry.configure(state='normal')
                if kind == 'error':
                    self.show_candidates(reset_manual=False)
                    self.manual_status.set(payload)
                    continue
                sid, operation, songs = payload
                try:
                    added = self.queue.merge_candidates(sid, songs)
                    if operation == 'keywords':
                        self.more_available, self.next_offset = songs.has_more, songs.next_offset
                    self.show_candidates(reset_manual=False, preferred_id=songs[0].id if songs else None)
                    excluded = len(getattr(songs, 'metadata_issues', []))
                    self.manual_status.set(f'新增 {added} 个候选；本次读取 {len(songs)} 首，排除异常 {excluded} 条。请选择对应歌曲后确认。')
                except Exception as exc:
                    self.show_candidates(reset_manual=False)
                    self.manual_status.set(describe_error(exc, self.store.root))
        except queue.Empty:
            pass
        self.poll_id = self.after(100, self.poll_network)

    def close(self):
        if self.network_busy:
            self.closing = True
            self.stop_event.set()
            self.manual_status.set('正在停止，等待当前网络请求结束后关闭；已有决定均已保存。')
            return
        self.finish_close()

    def finish_close(self):
        if getattr(self, 'poll_id', None):
            self.after_cancel(self.poll_id)
        self.grab_release()
        self.destroy()
        self.on_close()
