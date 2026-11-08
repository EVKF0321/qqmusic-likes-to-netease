if __name__ == '__main__':
    import sys
    from musicbridge.display import enable_system_dpi
    # The EXE declares this in its manifest; source runs need to opt in before Tk starts.
    if not getattr(sys, 'frozen', False):
        enable_system_dpi()
    if len(sys.argv) == 3 and sys.argv[1] == '--smoke-test':
        import json
        from pathlib import Path
        import tempfile
        import tkinter as tk
        from qqmusic_api import Client, Credential
        from qqmusic_api.models.login import QRLoginType
        from PIL import Image
        import qrcode
        from musicbridge.gui import App
        from musicbridge.display import native_dpi
        from musicbridge.vault import Vault
        with tempfile.TemporaryDirectory() as folder:
            root = tk.Tk()
            callback_errors = []
            root.report_callback_exception = lambda _t, exc, _tb: callback_errors.append(type(exc).__name__)
            app = App(root, folder)
            compact = root.minsize()
            root.geometry(f'{compact[0]}x{compact[1]}')
            root.update()
            assert app.tree.winfo_height() > app.display.px(45)
            assert all(b.winfo_viewable() and b.winfo_width() >= b.winfo_reqwidth() for b in app.buttons)
            icon_loaded = False
            if sys.platform == 'win32':
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                get_parent = user32.GetParent
                get_parent.argtypes, get_parent.restype = [wintypes.HWND], wintypes.HWND
                send = user32.SendMessageW
                send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                send.restype = ctypes.c_void_p
                hwnd = get_parent(root.winfo_id()) or root.winfo_id()
                icon_loaded = bool(send(hwnd, 0x7F, 0, 0))  # WM_GETICON / ICON_SMALL
                assert icon_loaded
            dpi = native_dpi(root)
            if dpi:
                assert dpi['awareness'] in (1, 2) and abs(dpi['dpi'] - app.display.dpi) <= 1, dpi
            vault = Vault(Path(folder) / 'test.dpapi')
            vault.save({'test': 'only local diagnostic'})
            assert vault.read()['test'] == 'only local diagnostic'
            from dataclasses import asdict
            from musicbridge.core import Song, Workspace
            from musicbridge.review import ReviewWindow
            from musicbridge.providers import SearchResults
            import time
            class OfflineSearch:
                def search_keywords(self, keywords, offset=0):
                    results = SearchResults()
                    results.append(Song('901', '手动模拟候选', ('模拟歌手',), '模拟专辑', 200))
                    return results
                def song_by_reference(self, reference):
                    return Song('902', '链接模拟候选', ('模拟歌手',), '模拟专辑', 200)
            review_store = Workspace(Path(folder) / 'review-smoke')
            sample = asdict(Song('candidate', '模拟候选', ('模拟歌手',), '模拟专辑', 200))
            review_store.data = {'source': [asdict(Song(sid, '模拟来源', ('模拟歌手',), '模拟专辑', 200, i + 1)) for i, sid in enumerate(('q1', 'q2'))],
                                 'matches': {sid: {'chosen': None, 'method': 'pending', 'candidates': [{'song': sample, 'score': .7, 'reasons': []}]} for sid in ('q1', 'q2')}}
            review = ReviewWindow(root, review_store, api=OfflineSearch())
            compact_review = review.minsize()
            review.geometry(f'{compact_review[0]}x{compact_review[1]}')
            root.update()
            assert review.candidates.winfo_height() > app.display.px(45)
            assert all(b.winfo_viewable() for b in (review.confirm_button, review.search_button, review.reference_button))
            def wait_manual():
                deadline = time.monotonic() + 5
                while review.network_busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                assert not review.network_busy
            review.search_custom()
            wait_manual()
            assert '901' in review_store.data['matches']['q1']['manual_candidate_ids']
            review.reference.set('902')
            review.load_reference()
            wait_manual()
            review.confirm()
            assert review_store.data['matches']['q1']['chosen']['id'] == '902'
            assert review.queue.current == 'q2'
            review.confirm(skip=True)
            assert review.queue.current is None
            review.close()
            result = {'gui_created': True, 'size': [root.winfo_width(), root.winfo_height()],
                      'sdk_import': True, 'dpapi_roundtrip': True, 'continuous_review': True,
                      'manual_search_and_reference': True, 'compact_layout': True,
                      'dpi': dpi, 'tk_dpi': app.display.dpi, 'window_icon': icon_loaded, 'accounts_accessed': False}
            root.after(200, root.destroy)
            root.mainloop()
            assert not callback_errors, callback_errors
        Path(sys.argv[2]).write_text(json.dumps(result, indent=2), encoding='utf-8')
    else:
        import argparse
        from musicbridge.gui import main
        parser = argparse.ArgumentParser()
        parser.add_argument('--data-dir', default=None)
        main(parser.parse_args().data_dir)
