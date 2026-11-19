"""Écoute la sortie audio et compare le son en direct au plouf de référence."""

from __future__ import annotations

import threading
import time
import wave
from collections import deque
from pathlib import Path

import numpy as np

try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover
    pyaudio = None


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_PATH = ROOT / "sounds" / "plouf.wav"
CAPTURE_DIR = ROOT / "sounds" / "captures"

TARGET_RATE = 16000
WIN = 512
HOP = 160
_FREQS = np.fft.rfftfreq(WIN, 1 / TARGET_RATE)
_BANDS = ((150, 400), (400, 900), (900, 1800), (1800, 3600), (3600, 7000))
_BAND_INDEX = [np.flatnonzero((_FREQS >= low) & (_FREQS < high)) for low, high in _BANDS]
_WINDOW = np.hanning(WIN).astype(np.float32)


def resample(mono: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    mono = np.asarray(mono, dtype=np.float32).reshape(-1)
    if mono.size == 0 or src_rate == dst_rate:
        return mono
    dst_count = max(1, int(round(mono.size * dst_rate / src_rate)))
    if mono.size == 1:
        return np.full(dst_count, mono[0], dtype=np.float32)
    source = np.arange(mono.size, dtype=np.float32)
    target = np.linspace(0, mono.size - 1, dst_count, dtype=np.float32)
    return np.interp(target, source, mono).astype(np.float32)


def loudest_segment(mono: np.ndarray, rate: int, seconds: float = 1.2) -> np.ndarray:
    length = int(seconds * rate)
    if mono.size <= length:
        return mono
    energy = np.cumsum(np.square(mono, dtype=np.float64))
    window = energy[length:] - energy[:-length]
    start = int(np.argmax(window))
    return mono[start : start + length]


def features(mono: np.ndarray) -> np.ndarray:
    """Énergie log dans cinq bandes. La forme de ce spectre sert de signature."""
    mono = np.ascontiguousarray(mono, dtype=np.float32).reshape(-1)
    if mono.size < WIN:
        mono = np.pad(mono, (0, WIN - mono.size))
    count = 1 + (mono.size - WIN) // HOP
    frames = np.lib.stride_tricks.as_strided(
        mono,
        shape=(count, WIN),
        strides=(HOP * mono.strides[0], mono.strides[0]),
    )
    spectrum = np.abs(np.fft.rfft(frames * _WINDOW, axis=1))
    bands = np.empty((count, len(_BAND_INDEX)), dtype=np.float32)
    for index, bins in enumerate(_BAND_INDEX):
        bands[:, index] = np.log1p(spectrum[:, bins].mean(axis=1))
    return bands


def similarity(live: np.ndarray, template: np.ndarray) -> float:
    """Meilleure similarité (0 à 100) d'une fenêtre glissante sur le son récent."""
    if live.ndim != 2 or template.ndim != 2 or live.shape[1] != template.shape[1]:
        return 0.0
    width = template.shape[0]
    if width < 2 or live.shape[0] < width:
        return 0.0
    centered = template - template.mean()
    template_norm = float(np.linalg.norm(centered))
    if template_norm < 1e-6:
        return 0.0
    flat = centered.ravel()
    best = 0.0
    span = live.shape[0] - width
    for start in range(0, span + 1, 2):
        window = live[start : start + width]
        shifted = window - window.mean()
        norm = float(np.linalg.norm(shifted))
        if norm < 1e-6:
            continue
        score = float(np.dot(shifted.ravel(), flat) / (norm * template_norm))
        if score > best:
            best = score
    return max(0.0, best) * 100.0


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("Le wav doit être en PCM 16 bits")
    if rate < 8000 or channels < 1:
        raise ValueError("Wav illisible")
    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        usable = samples.size - (samples.size % channels)
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(samples, dtype=np.float32), rate


def write_wav(path: Path, samples: np.ndarray, rate: int, channels: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(max(1, channels))
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())


def record_loopback(cancel: threading.Event, on_progress) -> tuple[np.ndarray, int, int, str]:
    """Enregistre la sortie par défaut jusqu'à l'arrêt, ou 3 minutes."""
    if pyaudio is None:
        raise RuntimeError("PyAudioWPatch n'est pas installé")
    pa = pyaudio.PyAudio()
    stream = None
    try:
        device = _loopback_device(pa)
        rate = int(device["defaultSampleRate"])
        channels = max(1, int(device["maxInputChannels"]))
        block = 2048
        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=block,
        )
        chunks: list[np.ndarray] = []
        frames = 0
        started = time.perf_counter()
        on_progress(0.0, device["name"])
        while not cancel.is_set() and time.perf_counter() - started < 180:
            raw = stream.read(block, exception_on_overflow=False)
            data = np.frombuffer(raw, dtype=np.float32).copy()
            if data.size == 0:
                continue
            chunks.append(data)
            frames += data.size // channels
            on_progress(frames / rate, device["name"])
        if not chunks:
            return np.zeros(0, dtype=np.float32), rate, channels, device["name"]
        return np.concatenate(chunks), rate, channels, device["name"]
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()


def _loopback_device(pa):
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice"):
        return speakers
    for loopback in pa.get_loopback_device_info_generator():
        if speakers["name"] in loopback["name"]:
            return loopback
    loopbacks = list(pa.get_loopback_device_info_generator())
    if not loopbacks:
        raise RuntimeError("Aucune sortie audio en boucle (WASAPI loopback)")
    return loopbacks[0]


class SplashListener:
    def __init__(self) -> None:
        self.threshold = 70.0
        self.cancel = threading.Event()
        self.armed = threading.Event()
        self.splash = threading.Event()
        self.device_name = ""
        self.error = ""
        self.reference_error = ""
        self.confidence = 0.0
        self.reference_ready = False
        self.listening = False
        self._template: np.ndarray | None = None
        self._template_mtime = 0.0
        self._chunks: deque[np.ndarray] = deque()
        self._samples = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._previous = 0.0
        self._cooldown = 0.0
        self._shown = 0.0
        self._shown_until = 0.0
        self._reset_request = False

    def ensure_started(self) -> None:
        if self._thread and self._thread.is_alive():
            self.listening = True
            return
        self.cancel.clear()
        self.listening = True
        self._chunks.clear()
        self._samples = 0
        self._previous = 0.0
        self.confidence = 0.0
        self._thread = threading.Thread(target=self._run, name="splash-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ferme la capture du son système."""
        self.listening = False
        self.armed.clear()
        self.splash.clear()
        self.cancel.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self.confidence = 0.0
        self._shown = 0.0
        self.device_name = ""
        self._chunks.clear()
        self._samples = 0

    def set_confidence(self, percent: float) -> None:
        self.threshold = min(95.0, max(20.0, float(percent)))

    def arm_listen(self) -> None:
        """Oublie le son du lancer. Un pic déjà au-dessus du seuil reste compté."""
        self._reset_request = True
        self.armed.set()

    def public_state(self) -> dict:
        self._refresh_template()
        listening = self.listening
        return {
            "audio_confidence": 0.0 if not listening else round(self.confidence, 1),
            "confidence_threshold": round(self.threshold, 1),
            "audio_armed": bool(listening and self.armed.is_set()),
            "audio_device": "" if not listening else self.device_name,
            "audio_error": self.error,
            "audio_listening": listening,
            "reference_ready": self.reference_ready,
            "reference_error": self.reference_error,
            "reference_path": "sounds/plouf.wav",
        }

    def _refresh_template(self) -> None:
        path = REFERENCE_PATH
        if not path.exists():
            self._template = None
            self._template_mtime = 0.0
            self.reference_ready = False
            self.reference_error = "Dépose le son découpé dans sounds/plouf.wav"
            return
        mtime = path.stat().st_mtime
        if self._template is not None and mtime == self._template_mtime:
            return
        try:
            samples, rate = load_wav(path)
            prepared = resample(samples, rate, TARGET_RATE)
            prepared = loudest_segment(prepared, TARGET_RATE, 1.2)
            if prepared.size < int(0.05 * TARGET_RATE):
                raise ValueError("Le son de référence est trop court")
            self._template = features(prepared)
            self._template_mtime = mtime
            self.reference_ready = True
            self.reference_error = ""
        except Exception as exc:
            self._template = None
            self._template_mtime = 0.0
            self.reference_ready = False
            self.reference_error = str(exc)

    def _run(self) -> None:
        if pyaudio is None:
            self.error = "PyAudioWPatch n'est pas installé"
            return
        try:
            self._listen()
        except Exception as exc:
            self.error = str(exc)

    def _listen(self) -> None:
        pa = pyaudio.PyAudio()
        stream = None
        try:
            device = _loopback_device(pa)
            self.device_name = device["name"]
            self.error = ""
            rate = int(device["defaultSampleRate"])
            channels = max(1, int(device["maxInputChannels"]))
            block = 1024
            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=block,
            )
            next_score = 0.0
            while not self.cancel.is_set():
                if self._reset_request:
                    self._reset_request = False
                    self._chunks.clear()
                    self._samples = 0
                    self._previous = 0.0
                    self._shown = 0.0
                    self.confidence = 0.0
                    self._cooldown = 0.0
                raw = stream.read(block, exception_on_overflow=False)
                data = np.frombuffer(raw, dtype=np.float32)
                if data.size == 0:
                    continue
                mono = data.reshape(-1, channels).mean(axis=1) if channels > 1 else data
                piece = resample(np.ascontiguousarray(mono), rate, TARGET_RATE)
                self._push(piece)
                now = time.perf_counter()
                if now >= next_score:
                    next_score = now + 0.08
                    self._score(now)
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            pa.terminate()

    def _push(self, piece: np.ndarray) -> None:
        if piece.size == 0:
            return
        self._chunks.append(piece)
        self._samples += piece.size
        limit = TARGET_RATE * 3
        while self._samples > limit and len(self._chunks) > 1:
            dropped = self._chunks.popleft()
            self._samples -= dropped.size

    def _score(self, now: float) -> None:
        if self._reset_request:
            self._reset_request = False
            self._chunks.clear()
            self._samples = 0
            self._previous = 0.0
            self._shown = 0.0
            self.confidence = 0.0
            self._cooldown = now + 0.2
            return
        self._refresh_template()
        template = self._template
        if template is None or not self._chunks:
            self.confidence = 0.0
            self._previous = 0.0
            return
        live = features(np.concatenate(self._chunks))
        measured = similarity(live, template)
        if measured >= self._shown:
            self._shown = measured
            self._shown_until = now + 1.5
        elif now >= self._shown_until:
            self._shown = measured
        self.confidence = self._shown
        if self.listening and max(measured, self.confidence) >= self.threshold:
            self.splash.set()
        self._previous = measured


listener = SplashListener()
