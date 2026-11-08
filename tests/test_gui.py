import gc
import tempfile
import time
import tkinter as tk
import unittest
from dataclasses import asdict
from unittest.mock import patch

from musicbridge.core import MigrationProgress, RequestFailure, Song
from musicbridge.gui import App
from musicbridge.providers import Netease, QQ
from musicbridge.ui import FAQ, show_help
from musicbridge.vault import Vault


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f'Tk desktop unavailable: {exc}')
        self.addCleanup(self.dispose_root)
        self.root.geometry('+9000+9000')
        self.root.report_callback_exception = lambda _t, exc, tb: self.fail(str(exc.with_traceback(tb)))

    def dispose_root(self):
        root, self.root = self.root, None
        root.destroy()
        del root
        # Destroy Tk objects on this thread before a later test starts a worker.
        gc.collect()

    def wait_for_startup(self, app):
        deadline = time.monotonic() + 3
        self.root.update()
        while app.busy and time.monotonic() < deadline:
            time.sleep(.01)
            self.root.update()
        self.assertFalse(app.busy)

    def assert_controls_fit(self, window, widgets):
        left, top = window.winfo_rootx(), window.winfo_rooty()
        right, bottom = left + window.winfo_width(), top + window.winfo_height()
        for widget in widgets:
            label = widget.cget('text')
            self.assertTrue(widget.winfo_viewable(), label)
            self.assertGreaterEqual(widget.winfo_width(), widget.winfo_reqwidth(), label)
            self.assertGreaterEqual(widget.winfo_rootx(), left, label)
            self.assertGreaterEqual(widget.winfo_rooty(), top, label)
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right, label)
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom, label)

    def check_small_window(self, dpi):
        self.root.tk.call('tk', 'scaling', dpi / 72)
        app = App(self.root, self.temp.name)
        self.assertAlmostEqual(app.display.dpi, dpi, delta=1)
        q = asdict(Song('qq1', '模拟曲目', ('模拟歌手',), '模拟专辑', 200, 1))
        ne = asdict(Song('ne1', '模拟曲目', ('模拟歌手',), '模拟专辑', 200))
        app.store.data.update(source=[q], matches={'qq1': {'method': 'pending', 'chosen': None,
                              'candidates': [{'song': ne, 'score': .8, 'reasons': []}]}})
        app.refresh()
        compact = self.root.minsize()
        for width, height in (compact, app.display.dims(920, 620), compact):
            self.root.geometry(f'{width}x{height}')
            self.root.update()
            self.assert_controls_fit(self.root, app.buttons + [app.pause_button])
            self.assertGreater(app.tree.winfo_height(), app.display.px(45))
        app.review_all()
        window = app.review_window
        width, height = window.minsize()
        window.geometry(f'{width}x{height}')
        self.root.update()
        self.assert_controls_fit(window, [window.confirm_button, window.skip_button, window.defer_button,
                                          window.search_button, window.more_button, window.reference_button])
        self.assertGreater(window.candidates.winfo_height(), app.display.px(45))
        window.close()
        help_window = show_help(self.root, '常见问题', FAQ)
        self.root.update()
        self.assertTrue(help_window.winfo_viewable())
        help_window.destroy()

    def test_compact_layout_at_100_percent(self):
        self.check_small_window(96)

    def test_compact_layout_at_125_percent(self):
        self.check_small_window(120)

    def test_compact_layout_at_150_percent(self):
        self.check_small_window(144)

    def test_startup_uses_saved_authorizations_and_keeps_platform_statuses_independent(self):
        saved = {'qq': {'musicid': 123}, 'netease': 'test-only-cookie'}
        with (patch.object(Vault, 'read', return_value=saved),
              patch.object(QQ, 'check_account', return_value='123') as qq,
              patch.object(Netease, 'check_account', return_value='456') as ne):
            app = App(self.root, self.temp.name)
            self.wait_for_startup(app)
            self.assertEqual(app.account_ids, {'qq': '123', 'netease': '456'})
            self.assertEqual([label.cget('text') for label in app.account_labels.values()], ['已连接', '已连接'])
            qq.assert_awaited_once()
            ne.assert_called_once_with(quick=True)

            qq.side_effect = RequestFailure('timeout', 'timeout')
            ne.side_effect = RequestFailure('expired', 'auth')
            app.check_connections()
            self.wait_for_startup(app)
            self.assertIn('暂未核验', app.account_labels['qq'].cget('text'))
            self.assertIn('授权失效', app.account_labels['netease'].cget('text'))
            self.assertEqual(app.ne.cookie, saved['netease'])

    def test_progress_marks_verified_and_skipped_rows_but_not_unverified_rows(self):
        app = App(self.root, self.temp.name)
        source = [asdict(Song(sid, sid, ('歌手',), '专辑', 200, index))
                  for index, sid in enumerate(('done', 'skip', 'next'), 1)]
        app.store.data.update(source=source, matches={
            'done': {'method': 'auto', 'chosen': dict(source[0], id='101')},
            'skip': {'method': 'skipped', 'chosen': None},
            'next': {'method': 'auto', 'chosen': dict(source[2], id='102')},
        })
        app.store.record_target('42', ['101'], ['101'])
        app.set_connection('netease', 'connected', '42')
        app.render_progress(MigrationProgress('writing', 2, 0, current='102'))
        self.assertIn('第 1 / 2 首', app.progress_text.get())
        self.assertEqual(app.progress['value'], 0)
        self.assertIn('muted', app.tree.item('done', 'tags'))
        self.assertIn('muted', app.tree.item('skip', 'tags'))
        self.assertNotIn('muted', app.tree.item('next', 'tags'))
        app.render_progress(MigrationProgress('confirmed', 2, 1, verified_song='102'))
        self.assertEqual(app.progress['value'], 1)
        self.assertIn('muted', app.tree.item('next', 'tags'))


if __name__ == '__main__':
    unittest.main()
