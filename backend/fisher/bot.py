"""Séquence de pêche : focus, lancer, attente du plouf, clic sur le bouchon."""

from __future__ import annotations

import ctypes
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np
import win32gui

from fisher.audio import CAPTURE_DIR, listener as ear, record_loopback, write_wav
from fisher.bobber_ref import public_state as bobber_reference_state
from fisher.overlay import hud
from fisher.vision import BobberHit, annotate, locate_bobber
from fisher.window import find_wow_window, focus_window, is_foreground, window_bounds
from fisher.wininput import click_left, click_mouse4, cursor_pos, move_to, stick_to

DEBUG_PATH = Path(__file__).resolve().parent.parent / "debug" / "last.jpg"

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
VK_ESCAPE = 0x1B
PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF

user32 = ctypes.windll.user32
LRESULT = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


@dataclass
class Settings:
    pre_delay: float = 2.0
    loop: bool = True
    confidence: float = 70.0
    dip_pixels: int = 24
    bite_timeout: float = 22.0
    loot_delay: float = 1.8

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        return cls(
            pre_delay=min(15.0, max(0.0, float(data.get("pre_delay", 2.0)))),
            loop=bool(data.get("loop", True)),
            confidence=min(95.0, max(20.0, float(data.get("confidence", 70.0)))),
            dip_pixels=min(80, max(6, int(data.get("dip_pixels", 24)))),
            bite_timeout=min(40.0, max(8.0, float(data.get("bite_timeout", 22.0)))),
            loot_delay=min(8.0, max(0.4, float(data.get("loot_delay", 1.8)))),
        )


class MotionTracker:
    """Repère la descente franche du bouchon, une fois qu'il flotte calmement."""

    def __init__(self, dip_pixels: int):
        self.dip_pixels = dip_pixels
        self.points: deque[tuple[float, float]] = deque()
        self.anchor_y: float | None = None
        self.stable_since: float | None = None
        self._dip_frames = 0

    def reset(self) -> None:
        self.points.clear()
        self.anchor_y = None
        self.stable_since = None
        self._dip_frames = 0

    def push(self, now: float, y: float) -> bool:
        self.points.append((now, y))
        while self.points and now - self.points[0][0] > 1.5:
            self.points.popleft()

        recent = [py for pt, py in self.points if now - pt <= 0.25]
        sudden = bool(recent) and (y - min(recent)) >= max(8, self.dip_pixels * 0.45)
        dipped_now = (
            self.anchor_y is not None
            and self.stable_since is not None
            and now - self.stable_since >= 0.5
            and y >= self.anchor_y + self.dip_pixels
            and sudden
        )
        if dipped_now:
            self._dip_frames = getattr(self, "_dip_frames", 0) + 1
        else:
            self._dip_frames = 0
        dipped = self._dip_frames >= 2
        window = [py for pt, py in self.points if now - pt <= 0.5]
        if len(window) >= 4 and max(window) - min(window) <= 6:
            if self.stable_since is None:
                self.stable_since = now
            self.anchor_y = sum(window) / len(window)
        return dipped


def serve_escape(cancel: threading.Event, swallow, on_miss=None) -> None:
    """Échap global. swallow() renvoie True pour avaler la touche."""

    def handler(code, w_param, l_param):
        if code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if info.vkCode == VK_ESCAPE and swallow():
                return 1
        return user32.CallNextHookEx(None, code, w_param, l_param)

    hook_proc = HOOKPROC(handler)
    hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook_proc, None, 0)
    if not hook_id and on_miss is not None:
        on_miss()
    user32.MsgWaitForMultipleObjects.argtypes = [
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    msg = wintypes.MSG()
    try:
        while not cancel.is_set():
            user32.MsgWaitForMultipleObjects(0, None, False, 150, QS_ALLINPUT)
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        if hook_id:
            user32.UnhookWindowsHookEx(hook_id)
    # Garder la fonction de rappel en vie pendant toute l'écoute.
    del hook_proc


class SoundRecorder:
    """Passe sur le jeu et enregistre le son système jusqu'à Échap."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.recording = False
        self.seconds = 0.0
        self.last_path = ""
        self.error = ""
        self.device_name = ""
        self._thread: threading.Thread | None = None
        self._hook_thread: threading.Thread | None = None
        self._hook_proc = None
        self._last_publish = 0.0

    def public_state(self) -> dict:
        return {
            "capturing": self.recording,
            "capture_seconds": round(self.seconds, 1),
            "last_capture": self.last_path,
            "capture_error": self.error,
        }

    def start(self) -> str | None:
        if fisher.running:
            return "Arrête la pêche avant de capturer le son"
        if self.recording or (self._thread and self._thread.is_alive()):
            return "Une capture est déjà en cours"
        if self._hook_thread and self._hook_thread.is_alive():
            self.cancel.set()
            self._hook_thread.join(0.6)
            if self._hook_thread.is_alive():
                return "La capture précédente n'est pas encore fermée"
        self.cancel = threading.Event()
        self.recording = True
        self.seconds = 0.0
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="sound-capture", daemon=True)
        self._hook_thread = threading.Thread(
            target=self._esc_loop,
            args=(self.cancel,),
            name="capture-esc",
            daemon=True,
        )
        self._thread.start()
        self._hook_thread.start()
        return None

    def request_stop(self, _reason: str) -> None:
        if self.recording:
            self.cancel.set()

    def wait(self, timeout: float) -> None:
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    def _esc_loop(self, cancel: threading.Event) -> None:
        def swallow() -> bool:
            if self.recording and self.cancel is cancel:
                self.request_stop("Échap")
                return True
            return False

        self._hook_proc = swallow
        serve_escape(
            cancel,
            swallow,
            lambda: fisher._log("Raccourci Échap indisponible. Même niveau de droits que le jeu requis."),
        )

    def _progress(self, seconds: float, device: str) -> None:
        self.seconds = seconds
        self.device_name = device
        now = time.perf_counter()
        if now - self._last_publish < 0.2:
            return
        self._last_publish = now
        fisher.publish_audio()

    def _run(self) -> None:
        cancel = self.cancel
        try:
            found = find_wow_window()
            if found is None:
                self.error = "Fenêtre du jeu introuvable"
                fisher._set(message="Aucune fenêtre « World of Warcraft »")
                return
            hwnd, title = found
            fisher._set(window=title, message="Passage sur World of Warcraft")
            if not focus_window(hwnd):
                self.error = "Impossible de passer le jeu au premier plan"
                fisher._set(message=self.error)
                return
            fisher._set(message="Enregistrement du son. Échap pour sauver le wav.")
            hud.show()
            samples, rate, channels, device = record_loopback(cancel, self._progress)
            self.device_name = device
            if samples.size < rate * channels * 0.2:
                fisher._set(message="Capture trop courte, aucun fichier")
                return
            CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("capture-%Y%m%d-%H%M%S.wav")
            path = CAPTURE_DIR / name
            write_wav(path, samples, rate, channels)
            self.last_path = f"sounds/captures/{name}"
            fisher._log(f"Son enregistré : {self.last_path}")
            fisher._set(message=f"Son enregistré : {self.last_path}")
        except Exception as exc:
            self.error = str(exc)
            fisher._set(message=f"Capture impossible : {exc}")
        finally:
            self.recording = False
            self.seconds = 0.0
            hud.hide()
            cancel.set()
            hook = self._hook_thread
            if hook and hook.is_alive() and hook is not threading.current_thread():
                hook.join(0.6)
            if self.cancel is cancel:
                fisher.publish_audio()


class Fisher:
    def __init__(self):
        self.cancel = threading.Event()
        self.armed = threading.Event()
        self.splash = threading.Event()
        self._lock = threading.Lock()
        self._subs: list[tuple] = []
        self._hook_proc = None
        self._hook_thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._listener = ear
        self.running = False
        self._state = self._empty_state()

    def _empty_state(self) -> dict:
        return {
            "running": False,
            "phase": "idle",
            "message": "En attente",
            "cycle": 0,
            "catches": 0,
            "bobber": None,
            "audio_device": "",
            "audio_confidence": 0.0,
            "confidence_threshold": 70.0,
            "audio_error": "",
            "reference_ready": False,
            "capturing": False,
            "last_capture": "",
            "bobber_reference": False,
            "window": "",
            "log": [],
        }

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                **self._state,
                "log": list(self._state["log"]),
                "bobber": dict(self._state["bobber"]) if self._state["bobber"] else None,
            }
        data.update(ear.public_state())
        data.update(recorder.public_state())
        data.update(bobber_reference_state())
        return data

    def publish_audio(self) -> None:
        with self._lock:
            self._state.update(ear.public_state())
            self._state.update(recorder.public_state())
        self._publish()

    def subscribe(self, loop, queue) -> None:
        self._subs.append((loop, queue))

    def unsubscribe(self, queue) -> None:
        self._subs = [(loop, q) for loop, q in self._subs if q is not queue]

    def start(self, settings: Settings) -> bool:
        if recorder.recording:
            return False
        if self._worker and self._worker.is_alive():
            return False
        if self._hook_thread and self._hook_thread.is_alive():
            self.cancel.set()
            self._hook_thread.join(0.6)
            if self._hook_thread.is_alive():
                return False
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.cancel = threading.Event()
            self.armed = threading.Event()
            self.splash = threading.Event()
            self._listener = ear
            ear.set_confidence(settings.confidence)
            self._state = self._empty_state()
            self._state["running"] = True
            self._state["phase"] = "focus"
            self._state["message"] = "Recherche de la fenêtre"
            worker = threading.Thread(target=self._run, args=(settings,), name="fisher", daemon=True)
            hook = threading.Thread(target=self._esc_loop, args=(self.cancel,), name="esc-hook", daemon=True)
            self._worker = worker
            self._hook_thread = hook
        self._publish()
        worker.start()
        hook.start()
        hud.show()
        return True

    def request_stop(self, reason: str) -> None:
        if not self.running:
            return
        if not self.cancel.is_set():
            self._log(f"Arrêt demandé ({reason})")
        self.cancel.set()

    def _publish(self) -> None:
        event = self.snapshot()
        for loop, queue in list(self._subs):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception:
                pass

    def _set(self, **fields) -> None:
        with self._lock:
            self._state.update(fields)
        self._publish()

    def _log(self, text: str) -> None:
        line = {"t": datetime.now().strftime("%H:%M:%S"), "text": text}
        with self._lock:
            self._state["log"] = (self._state["log"] + [line])[-80:]
            self._state["message"] = text
        self._publish()

    def _audio_fields(self) -> dict:
        return ear.public_state()

    def _finish(self, phase: str, message: str) -> None:
        with self._lock:
            self.running = False
            self._state["running"] = False
            self._state["phase"] = phase
            self._state["message"] = message
        self.armed.clear()
        hud.hide()
        self._publish()

    def _run(self, settings: Settings) -> None:
        ear.set_confidence(settings.confidence)
        ear.ensure_started()
        ear.armed.clear()
        ear.splash.clear()
        self.armed = ear.armed
        self.splash = ear.splash
        cancel = self.cancel
        armed = self.armed
        try:
            found = find_wow_window()
            if found is None:
                self._log("Aucune fenêtre dont le titre commence par « World of Warcraft »")
                self._finish("error", "Fenêtre du jeu introuvable")
                return
            hwnd, title = found
            self._set(window=title, phase="focus")
            self._log(f"Fenêtre trouvée : {title}")
            if not focus_window(hwnd):
                self._log("La fenêtre n'a pas pu passer au premier plan")
                self._finish("error", "Impossible de passer le jeu au premier plan")
                return
            self._log("Jeu au premier plan")

            heard = self._wait_for_audio(1.2)
            if heard is None:
                return
            if ear.device_name:
                self._log(f"Écoute audio : {ear.device_name}")
            else:
                self._log(
                    f"Son indisponible ({ear.error or 'boucle WASAPI'}). Secours visuel seulement."
                )
            if ear.reference_ready:
                self._log(f"Référence : sounds/plouf.wav, seuil {settings.confidence:.0f} %")
            else:
                self._log(ear.reference_error or "Son de référence absent")

            self._set(phase="wait", **self._audio_fields())
            self._log(f"Tempo de {settings.pre_delay:.1f} s avant le lancer")
            if self._sleep(settings.pre_delay):
                self._finish("idle", "Arrêté")
                return

            cycle = 0
            catches = 0
            with mss.mss() as sct:
                while not self.cancel.is_set():
                    cycle += 1
                    self._set(cycle=cycle, phase="cast", **self._audio_fields())
                    if win32gui.IsIconic(hwnd) or not is_foreground(hwnd):
                        focus_window(hwnd)
                        if self._sleep(0.15):
                            break
                    self._log(f"Lancer {cycle} — bouton souris 4")
                    click_mouse4()
                    clicked = self._wait_bite(hwnd, sct, settings, time.time())
                    if self.cancel.is_set():
                        break
                    if clicked:
                        catches += 1
                        self._set(catches=catches, phase="loot", **self._audio_fields())
                        self._log(f"Ferrage {catches}. Pause de {settings.loot_delay:.1f} s")
                        if self._sleep(settings.loot_delay):
                            break
                    else:
                        self._log("Pas de touche. On relance.")
                        if self._sleep(0.6):
                            break
                    if not settings.loop:
                        break
            self._finish("idle", "Arrêté" if self.cancel.is_set() else "Terminé")
        except Exception as exc:
            self._log(f"Erreur : {exc}")
            self._finish("error", str(exc))
        finally:
            cancel.set()
            armed.clear()
            ear.stop()
            hud.hide()
            with self._lock:
                if self.cancel is cancel and self._state["running"]:
                    self.running = False
                    self._state["running"] = False
                    self._state["phase"] = "idle"
            if self.cancel is cancel:
                self._publish()

    def _wait_for_audio(self, seconds: float) -> bool | None:
        deadline = time.time() + seconds
        listener = self._listener
        while time.time() < deadline:
            if self.cancel.is_set():
                self._finish("idle", "Arrêté")
                return None
            self._set(**self._audio_fields())
            if listener and (listener.device_name or listener.error):
                return True
            self.cancel.wait(0.1)
        return True

    def _sleep(self, seconds: float) -> bool:
        """Attend, en publiant le niveau sonore. True si l'arrêt est demandé."""
        end = time.time() + max(0.0, seconds)
        while not self.cancel.is_set():
            remaining = end - time.time()
            if remaining <= 0:
                return False
            self._set(**self._audio_fields())
            self.cancel.wait(min(0.2, remaining))
        return True

    def _wait_bite(self, hwnd: int, sct, settings: Settings, cast_at: float) -> bool:
        tracker = MotionTracker(settings.dip_pixels)
        previous: BobberHit | None = None
        last_seen: BobberHit | None = None
        last_seen_at = 0.0
        seen_logged = False
        armed_logged = False
        last_preview = 0.0
        last_aim: tuple[int, int] | None = None
        deadline = cast_at + settings.bite_timeout
        self.armed.clear()
        self.splash.clear()
        self._set(phase="search", bobber=None, **self._audio_fields())

        while not self.cancel.is_set() and time.time() < deadline:
            loop_at = time.perf_counter()
            if not win32gui.IsWindow(hwnd):
                self._log("La fenêtre du jeu a disparu")
                self.cancel.set()
                return False

            image, origin = capture(hwnd, sct)
            now = time.time()
            hit = locate_bobber(image, previous) if image is not None else None
            if hit is not None and hit.moved_far_from(previous):
                tracker.reset()

            dipped = False
            if hit is not None:
                previous = hit
                last_seen = hit
                last_seen_at = now
                screen_x = origin[0] + hit.click_x
                screen_y = origin[1] + hit.click_y
                first_sight = not seen_logged
                if first_sight:
                    seen_logged = True
                    self._log("Bouchon repéré, curseur dessus")
                stick_to(screen_x, screen_y, first=first_sight)
                last_aim = (screen_x, screen_y)
                dipped = tracker.push(now, hit.cy)
                self._set(
                    phase="bite" if self.armed.is_set() else "search",
                    bobber={
                        "x": origin[0] + hit.click_x,
                        "y": origin[1] + hit.click_y,
                        **({"match": round(hit.visual)} if hit.visual is not None else {}),
                    },
                    **self._audio_fields(),
                )
            else:
                self._set(**self._audio_fields())
                if previous is not None and now - last_seen_at > 0.45:
                    previous = None

            if (
                now - cast_at >= 1.3
                and not self.armed.is_set()
                and ear.confidence < settings.confidence
            ):
                ear.arm_listen()
            if self.armed.is_set() and not armed_logged:
                armed_logged = True
                self._set(phase="bite", **self._audio_fields())
                self._log("À l'écoute du plouf")

            reason = None
            heard = ear.splash.is_set() or ear.confidence >= settings.confidence
            if heard and now - cast_at >= 0.4:
                reason = "son"
            elif self.armed.is_set() and dipped:
                reason = "image"

            if image is not None and now - last_preview > 0.45:
                last_preview = now
                save_preview(image, hit)

            if reason:
                self.armed.clear()
                self.splash.clear()
                ear.splash.clear()
                self._set(phase="hooked", message="Un poisson a mordu !", **self._audio_fields())
                self._log("Un poisson a mordu !")
                if hit is not None:
                    last_aim = (origin[0] + hit.click_x, origin[1] + hit.click_y)
                elif last_seen is not None:
                    last_aim = (origin[0] + last_seen.click_x, origin[1] + last_seen.click_y)
                if last_aim is None:
                    last_aim = cursor_pos()
                move_to(last_aim[0], last_aim[1])
                if self._sleep(2.0):
                    return False
                image, origin = capture(hwnd, sct)
                refreshed = locate_bobber(image, hit or last_seen) if image is not None else None
                if refreshed is not None:
                    last_aim = (origin[0] + refreshed.click_x, origin[1] + refreshed.click_y)
                    move_to(last_aim[0], last_aim[1])
                if self.cancel.is_set():
                    return False
                click_left()
                self._log("Clic gauche pour attraper la prise.")
                if image is not None:
                    save_preview(image, refreshed)
                return True

            leftover = 0.09 - (time.perf_counter() - loop_at)
            if leftover > 0 and self.cancel.wait(leftover):
                return False
        return False

    def _esc_loop(self, cancel: threading.Event) -> None:
        def swallow() -> bool:
            if self.running and self.cancel is cancel:
                self.request_stop("Échap")
                return True
            return False

        self._hook_proc = swallow
        serve_escape(
            cancel,
            swallow,
            lambda: self._log("Raccourci Échap global indisponible. Lance le bot avec les mêmes droits que le jeu."),
        )


def capture(hwnd: int, sct) -> tuple[np.ndarray | None, tuple[int, int, int, int]]:
    left, top, right, bottom = window_bounds(hwnd)
    width, height = right - left, bottom - top
    if width < 80 or height < 80:
        return None, (left, top, right, bottom)
    shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
    image = np.asarray(shot)[:, :, :3].copy()
    return image, (left, top, right, bottom)


def save_preview(image: np.ndarray, hit: BobberHit | None) -> None:
    try:
        DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        preview = annotate(image, hit)
        height, width = preview.shape[:2]
        if width > 1100:
            scale = 1100 / width
            preview = cv2.resize(preview, (1100, max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
        tmp = DEBUG_PATH.with_suffix(".tmp.jpg")
        if not cv2.imwrite(str(tmp), preview, [int(cv2.IMWRITE_JPEG_QUALITY), 68]):
            return
        tmp.replace(DEBUG_PATH)
    except Exception:
        pass


def detect_once() -> dict:
    """Passe le jeu au premier plan et cherche le bouchon, sans lancer la canne."""
    found = find_wow_window()
    if found is None:
        return {"ok": False, "error": "Aucune fenêtre dont le titre commence par « World of Warcraft »"}
    hwnd, title = found
    focused = focus_window(hwnd)
    time.sleep(0.35)
    with mss.mss() as sct:
        image, origin = capture(hwnd, sct)
    if image is None:
        return {"ok": False, "error": "Capture d'écran impossible", "window": title, "focused": focused}
    hit = locate_bobber(image, None)
    save_preview(image, hit)
    payload = {"ok": True, "found": hit is not None, "window": title, "focused": focused}
    if hit is not None:
        payload["x"] = origin[0] + hit.click_x
        payload["y"] = origin[1] + hit.click_y
        payload["score"] = round(hit.score, 1)
        if hit.visual is not None:
            payload["match"] = round(hit.visual)
        stick_to(payload["x"], payload["y"], first=True)
    return payload


recorder = SoundRecorder()
fisher = Fisher()
