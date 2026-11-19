"""Fenêtre toujours au-dessus : cycle en cours et confiance du plouf."""

from __future__ import annotations

import ctypes
import threading

import win32api
import win32con
import win32gui

WIDTH = 460
HEIGHT = 118
BUTTON = (WIDTH - 86, 6, WIDTH - 8, 28)
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x02

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_uint


def _rgb(red: int, green: int, blue: int) -> int:
    return red | (green << 8) | (blue << 16)


class DebugHud:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._stop_hwnd = 0
        self._font = 0
        self._ready = threading.Event()
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="debug-hud", daemon=True)
        self._thread.start()
        self._ready.wait(1.5)

    def show(self) -> None:
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
        if self._stop_hwnd:
            self._place_stop()
            win32gui.ShowWindow(self._stop_hwnd, win32con.SW_SHOWNOACTIVATE)

    def hide(self) -> None:
        if self._stop_hwnd:
            win32gui.ShowWindow(self._stop_hwnd, win32con.SW_HIDE)
        if self._hwnd:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)

    def stop(self) -> None:
        if self._stop_hwnd:
            win32gui.PostMessage(self._stop_hwnd, win32con.WM_CLOSE, 0, 0)
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)

    def _place_stop(self) -> None:
        if not self._hwnd or not self._stop_hwnd:
            return
        left, top, right, _bottom = win32gui.GetWindowRect(self._hwnd)
        width = BUTTON[2] - BUTTON[0]
        height = BUTTON[3] - BUTTON[1]
        win32gui.SetWindowPos(
            self._stop_hwnd,
            win32con.HWND_TOPMOST,
            right - width - 8,
            top + BUTTON[1],
            width,
            height,
            win32con.SWP_NOACTIVATE,
        )

    def _run(self) -> None:
        class_name = "FishingBotHud"
        instance = win32api.GetModuleHandle(None)
        window_class = win32gui.WNDCLASS()
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        window_class.lpfnWndProc = self._wndproc
        window_class.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        window_class.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
        try:
            win32gui.RegisterClass(window_class)
        except win32gui.error:
            pass

        screen_w = win32api.GetSystemMetrics(0)
        x = max(8, screen_w - WIDTH - 16)
        style = (
            win32con.WS_EX_TOPMOST
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | WS_EX_NOACTIVATE
        )
        self._hwnd = win32gui.CreateWindowEx(
            style,
            class_name,
            "Pêche",
            win32con.WS_POPUP,
            x,
            12,
            WIDTH,
            HEIGHT,
            0,
            0,
            instance,
            None,
        )
        win32gui.SetLayeredWindowAttributes(self._hwnd, 0, 232, LWA_ALPHA)
        self._font = gdi32.CreateFontW(-15, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        self._stop_hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
            class_name,
            "Arrêter",
            win32con.WS_POPUP,
            x + BUTTON[0],
            12 + BUTTON[1],
            BUTTON[2] - BUTTON[0],
            BUTTON[3] - BUTTON[1],
            0,
            0,
            instance,
            None,
        )
        if not user32.SetTimer(self._hwnd, 1, 70, None):
            raise ctypes.WinError(ctypes.get_last_error())
        win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
        win32gui.ShowWindow(self._stop_hwnd, win32con.SW_HIDE)
        self._ready.set()
        try:
            win32gui.PumpMessages()
        finally:
            if self._font:
                gdi32.DeleteObject(self._font)
                self._font = 0
            self._hwnd = 0
            self._stop_hwnd = 0

    def _wndproc(self, hwnd, message, wparam, lparam):
        if hwnd == self._stop_hwnd:
            return self._stop_proc(hwnd, message, wparam, lparam)
        if message == win32con.WM_TIMER:
            if win32gui.IsWindowVisible(hwnd):
                win32gui.SetWindowPos(
                    hwnd,
                    win32con.HWND_TOPMOST,
                    0,
                    0,
                    0,
                    0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                )
                self._place_stop()
                win32gui.InvalidateRect(hwnd, None, False)
                if self._stop_hwnd:
                    win32gui.InvalidateRect(self._stop_hwnd, None, False)
            return 0
        if message == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        if message == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def _paint(self, hwnd) -> None:
        from fisher.bot import fisher

        hdc, paint = win32gui.BeginPaint(hwnd)
        try:
            state = fisher.snapshot()
            capturing = bool(state.get("capturing"))
            confidence = float(state.get("audio_confidence") or 0)
            threshold = float(state.get("confidence_threshold") or 70)
            if capturing:
                title = "Enregistrement"
                second = f"{float(state.get('capture_seconds') or 0):.1f} s"
                third = "Échap enregistre le wav"
                hot = False
            elif state.get("phase") == "hooked":
                title = "Un poisson a mordu !"
                second = f"Cycle {int(state.get('cycle') or 0)}"
                third = "Clic dans 2 s"
                hot = True
            else:
                title = f"Cycle {int(state.get('cycle') or 0)}"
                second = f"Confiance {confidence:.0f} %"
                third = f"Seuil {threshold:.0f} %"
                hot = confidence >= threshold and threshold > 0
            message = "" if state.get("phase") == "hooked" else (state.get("message") or "")[:42]

            rect = win32gui.GetClientRect(hwnd)
            bg = win32gui.CreateSolidBrush(_rgb(12, 20, 26))
            win32gui.FillRect(hdc, rect, bg)
            win32gui.DeleteObject(bg)

            if self._font:
                win32gui.SelectObject(hdc, self._font)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            win32gui.SetTextColor(hdc, _rgb(232, 165, 75))
            win32gui.DrawText(
                hdc, title, -1, (12, 8, BUTTON[0] - 8, 32), win32con.DT_LEFT | win32con.DT_SINGLELINE
            )
            win32gui.SetTextColor(hdc, _rgb(232, 165, 75) if hot else _rgb(214, 226, 234))
            win32gui.DrawText(hdc, second, -1, (12, 38, WIDTH - 12, 60), win32con.DT_LEFT | win32con.DT_SINGLELINE)
            win32gui.SetTextColor(hdc, _rgb(224, 122, 106))
            win32gui.DrawText(hdc, third, -1, (12, 62, WIDTH - 12, 84), win32con.DT_LEFT | win32con.DT_SINGLELINE)
            win32gui.SetTextColor(hdc, _rgb(138, 160, 176))
            win32gui.DrawText(hdc, message, -1, (12, 88, WIDTH - 12, 110), win32con.DT_LEFT | win32con.DT_SINGLELINE)
        except Exception as exc:
            self.last_error = str(exc)
        finally:
            win32gui.EndPaint(hwnd, paint)

    def _stop_proc(self, hwnd, message, wparam, lparam):
        if message == win32con.WM_LBUTTONUP:
            from fisher.bot import fisher, recorder

            fisher.request_stop("l'overlay")
            recorder.request_stop("l'overlay")
            return 0
        if message == win32con.WM_PAINT:
            hdc, paint = win32gui.BeginPaint(hwnd)
            try:
                rect = win32gui.GetClientRect(hwnd)
                brush = win32gui.CreateSolidBrush(_rgb(224, 122, 106))
                win32gui.FillRect(hdc, rect, brush)
                win32gui.DeleteObject(brush)
                if self._font:
                    win32gui.SelectObject(hdc, self._font)
                win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
                win32gui.SetTextColor(hdc, _rgb(45, 18, 16))
                win32gui.DrawText(
                    hdc,
                    "Arrêter",
                    -1,
                    rect,
                    win32con.DT_CENTER | win32con.DT_VCENTER | win32con.DT_SINGLELINE,
                )
            finally:
                win32gui.EndPaint(hwnd, paint)
            return 0
        if message == win32con.WM_CLOSE:
            win32gui.DestroyWindow(hwnd)
            return 0
        if message == win32con.WM_DESTROY:
            if hwnd == self._stop_hwnd:
                self._stop_hwnd = 0
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)


hud = DebugHud()
