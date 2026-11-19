"""Repère la fenêtre du jeu et la passe au premier plan."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import win32con
import win32gui

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

DWMWA_EXTENDED_FRAME_BOUNDS = 9
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
GA_ROOT = 2


def find_wow_window() -> tuple[int, str] | None:
    """Plus grande fenêtre visible dont le titre commence par « World of Warcraft »."""
    found: list[tuple[int, str, int]] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd) or ""
        if not title.lower().startswith("world of warcraft"):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = max(0, right - left) * max(0, bottom - top)
        found.append((hwnd, title, area))

    win32gui.EnumWindows(visit, None)
    if not found:
        return None
    found.sort(key=lambda item: item[2], reverse=True)
    hwnd, title, _ = found[0]
    return hwnd, title


def window_bounds(hwnd: int) -> tuple[int, int, int, int]:
    """Rectangle visible à l'écran, sans l'ombre invisible de Windows."""
    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if ok == 0 and rect.right > rect.left and rect.bottom > rect.top:
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return win32gui.GetWindowRect(hwnd)


def is_foreground(hwnd: int) -> bool:
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    return user32.GetAncestor(fg, GA_ROOT) == hwnd


def focus_window(hwnd: int) -> bool:
    """Amène la fenêtre au premier plan. Windows bloque SetForegroundWindow sans ce détour."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

    fg = user32.GetForegroundWindow()
    current = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)

    attached_fg = False
    attached_target = False
    try:
        if fg_thread and fg_thread != current:
            attached_fg = bool(user32.AttachThreadInput(current, fg_thread, True))
        if target_thread and target_thread != current:
            attached_target = bool(user32.AttachThreadInput(current, target_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    finally:
        if attached_fg:
            user32.AttachThreadInput(current, fg_thread, False)
        if attached_target:
            user32.AttachThreadInput(current, target_thread, False)

    if is_foreground(hwnd):
        return True

    # Dernier recours : une touche Alt fictive lève la restriction de Windows.
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    return bool(is_foreground(hwnd))
