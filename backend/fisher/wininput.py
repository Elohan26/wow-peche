"""Simule le bouton latéral 4 (XBUTTON1) et le clic gauche."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
XBUTTON1 = 0x0001

ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("mi", MOUSEINPUT))


class POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


def _send(flags: int, data: int = 0, x: int = 0, y: int = 0) -> None:
    event = INPUT()
    event.type = INPUT_MOUSE
    event.mi = MOUSEINPUT(int(x), int(y), int(data), int(flags), 0, 0)
    sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
    if sent == 1:
        return
    # Secours si SendInput est refusé.
    user32.mouse_event(int(flags), int(x), int(y), int(data), 0)


def cursor_pos() -> tuple[int, int]:
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def stick_to(x: int, y: int, first: bool = False) -> None:
    """Pose le curseur sur le bouchon. Le premier placement est visible, la suite le suit."""
    x, y = int(x), int(y)
    cx, cy = cursor_pos()
    if first or abs(cx - x) + abs(cy - y) > 90:
        move_to(x, y, steps=8)
        return
    if abs(cx - x) > 5 or abs(cy - y) > 5:
        user32.SetCursorPos(x, y)


def move_to(x: int, y: int, steps: int = 6) -> None:
    """Déplace le curseur en quelques pas pour que le jeu prenne la position."""
    sx, sy = cursor_pos()
    tx, ty = int(x), int(y)
    steps = max(1, steps)
    for i in range(1, steps + 1):
        nx = round(sx + (tx - sx) * i / steps)
        ny = round(sy + (ty - sy) * i / steps)
        user32.SetCursorPos(int(nx), int(ny))
        time.sleep(0.008)


def click_mouse4() -> None:
    """Bouton latéral arrière, celui que les jeux appellent souris 4."""
    _send(MOUSEEVENTF_XDOWN, XBUTTON1)
    time.sleep(0.045)
    _send(MOUSEEVENTF_XUP, XBUTTON1)


def click_left() -> None:
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.045)
    _send(MOUSEEVENTF_LEFTUP)
