"""Shared Tk styling and help for the desktop workflow."""
import tkinter as tk
from tkinter import ttk
from .display import Display


BG = '#f2f4fa'
PAPER = '#ffffff'
INK = '#20263d'
MUTED = '#58617b'
LINE = '#d9deec'
ACCENT = '#5646cb'
SOFT = '#eeebff'
GOOD = '#166451'
WARN = '#8d4a08'
FONT = 'Microsoft YaHei UI'


def setup_style(root, display):
    px, dims = display.px, display.dims
    root.configure(bg=BG)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=(FONT, 10), foreground=INK, background=PAPER)
    style.configure('Page.TFrame', background=BG)
    style.configure('Banner.TFrame', background=SOFT)
    style.configure('Title.TLabel', font=(FONT, 22, 'bold'), background=BG)
    style.configure('Subtitle.TLabel', font=(FONT, 10), background=BG, foreground=MUTED)
    style.configure('PageMuted.TLabel', background=BG, foreground=MUTED)
    style.configure('Strong.TLabel', font=(FONT, 10, 'bold'))
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('Good.TLabel', foreground=GOOD)
    style.configure('Warn.TLabel', foreground=WARN)
    style.configure('Step.TLabel', background=SOFT, foreground='#382e93', font=(FONT, 10, 'bold'))
    style.configure('TButton', width=0, padding=dims(8, 4), borderwidth=1, relief='flat',
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, background='#edf0f7')
    style.map('TButton', background=[('disabled', '#f2f3f7'), ('pressed', '#dadfec'), ('active', '#e1e6f2')],
              foreground=[('disabled', '#72798b')], bordercolor=[('focus', ACCENT)])
    style.configure('Compact.TButton', padding=dims(5, 3))
    style.configure('Accent.TButton', background=ACCENT, foreground='white',
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
    style.map('Accent.TButton', background=[('disabled', '#e1def6'), ('pressed', '#393099'), ('active', '#493bb3')],
              foreground=[('disabled', '#68617f'), ('!disabled', 'white')], bordercolor=[('focus', '#231765')])
    style.configure('TRadiobutton', padding=dims(4, 3))
    style.configure('TNotebook', borderwidth=0, tabmargins=(0, 0, 0, 0), background=BG,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
    style.configure('TNotebook.Tab', padding=dims(13, 6), background='#e6eaf4', bordercolor=LINE)
    style.map('TNotebook.Tab', background=[('selected', PAPER)], foreground=[('selected', ACCENT)])
    style.configure('Treeview', rowheight=px(30), background=PAPER, fieldbackground=PAPER, borderwidth=0,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
    style.configure('Treeview.Heading', font=(FONT, 10, 'bold'), padding=dims(4, 4), background='#eef1f8', relief='flat')
    style.map('Treeview', background=[('selected', SOFT)], foreground=[('selected', '#2a207b')])
    style.configure('Horizontal.TProgressbar', troughcolor='#e0e4f2', background=ACCENT,
                    bordercolor='#e0e4f2', lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(9))
    style.configure('TScrollbar', background='#e0e5f0', troughcolor='#f3f5fa',
                    bordercolor=LINE, lightcolor='#e0e5f0', darkcolor='#e0e5f0',
                    arrowcolor=MUTED, arrowsize=px(11), gripcount=0)


class Flow(ttk.Frame):
    """Wrap compact control groups instead of clipping buttons in a narrow window."""
    def __init__(self, parent):
        super().__init__(parent)
        self.items = []
        self.gap, self.line_gap = Display.for_widget(parent).dims(6, 3)
        self.layout = None
        self.pending_layout = None
        self.bind('<Configure>', lambda _: self.reflow())
        self.bind('<Destroy>', self.cancel_layout, add='+')

    def add(self, widget):
        self.items.append(widget)
        self.schedule_layout()
        return widget

    def schedule_layout(self):
        if self.pending_layout is None:
            self.pending_layout = self.after_idle(self.finish_layout)

    def finish_layout(self):
        self.pending_layout = None
        self.reflow()

    def cancel_layout(self, _):
        if self.pending_layout is not None:
            self.after_cancel(self.pending_layout)
            self.pending_layout = None

    def reflow(self):
        width = self.winfo_width()
        if width < 2 or not self.items:
            return
        x, y, row_height = 0, 0, 0
        placement = []
        for widget in self.items:
            w, h = widget.winfo_reqwidth(), widget.winfo_reqheight()
            if x and x + w > width:
                x, y, row_height = 0, y + row_height + self.line_gap, 0
            placement.append((widget, x, y, w, h))
            x += w + self.gap
            row_height = max(row_height, h)
        if placement == self.layout:
            return
        self.layout = placement
        self.configure(height=y + row_height + self.line_gap)
        for widget, x, y, _, _ in placement:
            widget.place(x=x, y=y)


def wrap_to_width(label, inset=4):
    inset = Display.for_widget(label).px(inset)
    def resize(event):
        width = max(1, event.width - inset)
        if int(label.cget('wraplength') or 0) != width:
            label.configure(wraplength=width)
    label.bind('<Configure>', resize, add='+')


def hint(widget, text):
    pending, popup = None, None

    def show():
        nonlocal pending, popup
        pending = None
        popup = tk.Toplevel(widget)
        popup.overrideredirect(True)
        scale = Display.for_widget(widget)
        ttk.Label(popup, text=text, padding=scale.dims(10, 7), wraplength=scale.px(300), relief='solid',
                  background=INK, foreground=PAPER).pack()
        popup.update_idletasks()
        x = min(widget.winfo_rootx(), popup.winfo_screenwidth() - popup.winfo_reqwidth() - 8)
        y = widget.winfo_rooty() + widget.winfo_height() + 5
        if y + popup.winfo_reqheight() >= popup.winfo_screenheight():
            y = widget.winfo_rooty() - popup.winfo_reqheight() - 5
        popup.geometry(f'+{max(0, x)}+{max(0, y)}')

    def hide(_=None):
        nonlocal pending, popup
        if pending is not None:
            widget.after_cancel(pending)
            pending = None
        if popup is not None:
            popup.destroy()
            popup = None

    def enter(_):
        nonlocal pending
        hide()
        pending = widget.after(550, show)

    widget.bind('<Enter>', enter, add='+')
    widget.bind('<Leave>', hide, add='+')
    widget.bind('<ButtonPress>', hide, add='+')
    widget.bind('<Destroy>', hide, add='+')


GUIDE = (
    ('01  连接账号', '分别点击「QQ 扫码登录」「网易云扫码登录」，用对应手机 App 扫码。'
     '再次启动会核验已保存的授权；显示已连接即可继续。「检查连接」可手动重查。「清除授权」仅删除本机保存的登录。'),
    ('02  读取并确认顺序', '点「读取并备份两端」读取两边的收藏。对照 QQ 音乐「我喜欢」列表的首尾和中间几首。'
     '若刚好倒过来，点「反转源序列」再核对。对得上就点「确认源顺序」，填写 QQ 当前的排序方式。'),
    ('03  匹配和人工核对', '点「搜索匹配 / 续查」；可信匹配会自动选定，其余在「连续确认」中逐首核对。'
     '要改某一首，选中它再点「人工选候选」。可以自行搜索，也可以粘贴网易云单曲链接或 ID 补充候选。'
     '「不迁移此曲」会保存决定；「稍后处理」仍会留在待确认列表里。灰色行标记已在网易云或已决定不迁移的歌曲。'),
    ('04  首次迁移：实测、预览、确认', '建议选「安全合并」，保留现有收藏。首次点「排序实测」，会在你确认后真实收藏两首测试歌。'
     '「严格同步」还需通过重排实测，且目标没有额外收藏。点「迁移预览」检查本次新增曲目，再点「确认迁移 / 续传」执行。'),
    ('05  查看进度与报告', '进度条只计入已读回确认的歌曲。「暂停」会等当前请求结束，进度保留。'
     '同一任务续传不用再次排序实测：点「迁移预览」→「确认迁移 / 续传」。'
     '完成后在「预览与报告」看核验结果，用「打开备份与报告」查看文件。'),
)

FAQ = (
    ('迁移时有一首报错 500，怎么继续？',
     '程序会先读回核验。若没写进去，就停在出错曲目。回到「歌曲与匹配」，选中出错行（也可点击「定位出错歌曲」），'
     '点击「人工选候选」。到网易云音乐自行搜索正确版本并复制单曲链接；粘贴到「链接 / ID」，点「读取并补充候选」，'
     '选中正确歌曲，点「确认选中 → 下一首」保存，再点「保存并关闭」。回主窗口点「迁移预览」→「确认迁移 / 续传」。'
     '续传不用再次排序实测。'),
    ('关闭软件或点击暂停后，怎样续传？',
     '重开软件后核对两端连接状态，保持原账号、来源和迁移模式。直接点「迁移预览」再点「确认迁移 / 续传」。'
     '预览会重新读取当前红心，自动排除已收藏的歌曲；同一任务不用再次排序实测。换账号、来源或模式时按预览提示操作。'),
    ('遇到 405 / 429，或者一直在等待？',
     '平台暂时限制了访问。状态栏会显示等待时间；等待中不用重复点击。若最终仍受限，请过一会在官方 App 确认账号可用，'
     '再回来重新预览并续传。可以点「暂停」结束等待。'),
    ('重开后显示未连接或暂未核验？',
     '已连接表示平台返回了有效账号。若显示「暂未核验」，先检查网络再点「检查连接」，无需立刻重新扫码。'
     '显示「授权失效」时再扫码登录。清除本地授权不会取消任何喜欢。'),
    ('显示迁移结束，为什么顺序或数量仍有差异？',
     '「安全合并」保留原有红心的位置，核验新增歌曲的相对顺序；全列表可能与 QQ 不同。'
     '跳过、未匹配或找不到正确版本的歌曲也会造成数量差异。可在「预览与报告」查看核验摘要和完整报告。'),
)


def show_help(parent, title, sections):
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    scale = Display.for_widget(win)
    scale.configure_window(win, (670, 510), (400, 310))
    win.configure(bg=BG)
    box = ttk.Frame(win, padding=scale.px(14))
    box.pack(fill='both', expand=True, padx=scale.px(12), pady=scale.px(12))
    ttk.Label(box, text=title, font=(FONT, 16, 'bold')).pack(anchor='w', pady=(0, 10))
    ttk.Button(box, text='关闭', command=win.destroy).pack(side='bottom', anchor='e', pady=(8, 0))
    pane = ttk.Frame(box)
    pane.pack(fill='both', expand=True)
    bar = ttk.Scrollbar(pane)
    bar.pack(side='right', fill='y')
    content = tk.Text(pane, wrap='word', relief='flat', bg=PAPER, fg=INK, font=(FONT, 11),
                      padx=8, spacing2=4, yscrollcommand=bar.set)
    bar.configure(command=content.yview)
    content.pack(fill='both', expand=True)
    content.tag_configure('heading', font=(FONT, 12, 'bold'), foreground=ACCENT, spacing1=9, spacing3=4)
    for heading, body in sections:
        content.insert('end', heading + '\n', 'heading')
        content.insert('end', body + '\n\n')
    content.configure(state='disabled')
    win.bind('<Escape>', lambda _: win.destroy())
    return win
