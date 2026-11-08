"""Keep the UI in logical units while Tk draws text at the system DPI."""
from dataclasses import dataclass
import os


def enable_system_dpi():
    """Source runs do not have the Windows manifest used by the packaged EXE."""
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()


def work_area(widget):
    width, height = widget.winfo_screenwidth(), widget.winfo_screenheight()
    if os.name == 'nt':
        import ctypes
        from ctypes.wintypes import RECT
        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            width = min(width, rect.right - rect.left)
            height = min(height, rect.bottom - rect.top)
    return width, height


@dataclass(frozen=True)
class Display:
    dpi: int

    @classmethod
    def for_widget(cls, widget):
        return cls(max(1, round(widget.winfo_fpixels('1i'))))

    def px(self, logical):
        return round(logical * self.dpi / 96)

    def dims(self, *logical):
        return tuple(self.px(value) for value in logical)

    def configure_window(self, window, preferred, minimum):
        work_width, work_height = work_area(window)
        cap_width = max(1, work_width - self.px(24))
        cap_height = max(1, work_height - self.px(46))

        def fit(size):
            return min(cap_width, self.px(size[0])), min(cap_height, self.px(size[1]))

        width, height = fit(preferred)
        window.minsize(*fit(minimum))
        window.geometry(f'{width}x{height}')


def native_dpi(window):
    """Inspect the Windows DPI of the rendered window for the packaged smoke test."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    get_dpi = user32.GetDpiForWindow
    get_dpi.argtypes = [wintypes.HWND]
    get_dpi.restype = wintypes.UINT
    get_context = user32.GetWindowDpiAwarenessContext
    get_context.argtypes = [wintypes.HWND]
    get_context.restype = ctypes.c_void_p
    awareness = user32.GetAwarenessFromDpiAwarenessContext
    awareness.argtypes = [ctypes.c_void_p]
    awareness.restype = ctypes.c_int
    hwnd = window.winfo_id()
    return {'dpi': get_dpi(hwnd), 'awareness': awareness(get_context(hwnd))}
