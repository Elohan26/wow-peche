"""Image collée du bouchon, et comparaison avec ce qui est à l'écran."""

from __future__ import annotations

import base64
import threading
from pathlib import Path

import cv2
import numpy as np

from fisher.vision import BobberHit

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_PATH = ROOT / "references" / "bobber.png"
MAX_SIDE = 180
ACCEPT = 58.0
TRACK_ACCEPT = 50.0

_lock = threading.Lock()
_template: np.ndarray | None = None
_mtime = 0.0


def public_state() -> dict:
    _load()
    ready = _template is not None
    return {
        "bobber_reference": ready,
        "bobber_saved_at": int(_mtime) if ready else 0,
    }


def save_bobber_image(payload: str) -> dict:
    raw = _decode(payload)
    if len(raw) > 6_000_000:
        raise ValueError("Image trop lourde")
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError("Image illisible")
    crop = _isolate(image)
    crop = _limit(crop, MAX_SIDE)
    REFERENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(REFERENCE_PATH), crop):
        raise ValueError("Enregistrement impossible")
    with _lock:
        global _template, _mtime
        _template = crop
        _mtime = REFERENCE_PATH.stat().st_mtime
    height, width = crop.shape[:2]
    return {"ok": True, "width": width, "height": height, "bobber_saved_at": int(_mtime)}


def find_with_reference(image_bgr: np.ndarray, previous: BobberHit | None) -> tuple[bool, BobberHit | None]:
    """(référence chargée, bouchon trouvé). Sans référence, le second vaut None."""
    template = _load()
    if template is None:
        return False, None
    if previous is not None:
        window = _around(image_bgr, previous, 240)
        if window is not None:
            crop, origin = window
            hit = _best(crop, template, (0.85, 1.0, 1.15), TRACK_ACCEPT)
            if hit is not None:
                return True, _shift(hit, origin)
    roi, origin_y = _water(image_bgr)
    hit = _best(roi, template, (0.65, 0.85, 1.0, 1.2, 1.45, 1.75), ACCEPT)
    if hit is None:
        return True, None
    return True, _shift(hit, (0, origin_y))


def _decode(payload: str) -> bytes:
    text = payload.strip()
    if "," in text and text.startswith("data:"):
        text = text.split(",", 1)[1]
    try:
        return base64.b64decode(text, validate=False)
    except Exception as exc:
        raise ValueError("Image illisible") from exc


def _isolate(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    if max(height, width) <= 280:
        return image
    from fisher.vision import _locate

    hit = _locate(image, None, mask_ui=max(height, width) > 700)
    if hit is None:
        if max(height, width) > 520:
            raise ValueError("Recadre le bouchon : il n'a pas pu être isolé dans cette image.")
        return image
    pad_x = int(hit.width * 0.4)
    pad_y = int(hit.height * 0.5)
    x0 = max(0, hit.left - pad_x)
    y0 = max(0, hit.top - int(hit.height * 0.25))
    x1 = min(width, hit.left + hit.width + pad_x)
    y1 = min(height, hit.top + hit.height + pad_y)
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return image
    return crop


def _limit(image: np.ndarray, longest: int) -> np.ndarray:
    height, width = image.shape[:2]
    if max(height, width) <= longest:
        return image
    scale = longest / max(height, width)
    return cv2.resize(image, (max(8, int(width * scale)), max(8, int(height * scale))), interpolation=cv2.INTER_AREA)


def _load() -> np.ndarray | None:
    global _template, _mtime
    path = REFERENCE_PATH
    if not path.exists():
        with _lock:
            _template = None
            _mtime = 0.0
        return None
    mtime = path.stat().st_mtime
    with _lock:
        if _template is not None and mtime == _mtime:
            return _template
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        return None
    with _lock:
        _template = image
        _mtime = mtime
        return _template


def _water(image: np.ndarray) -> tuple[np.ndarray, int]:
    height, width = image.shape[:2]
    y0 = int(height * 0.08)
    y1 = int(height * 0.86)
    roi = image[y0:y1, :].copy()
    map_bottom = int(height * 0.36) - y0
    map_left = int(width * 0.78)
    if map_bottom > 0 and map_left < width:
        flat = np.median(roi.reshape(-1, 3), axis=0).astype(np.uint8)
        roi[:map_bottom, map_left:] = flat
    return roi, y0


def _around(image: np.ndarray, previous: BobberHit, radius: int) -> tuple[np.ndarray, tuple[int, int]] | None:
    height, width = image.shape[:2]
    x0 = max(0, previous.cx - radius)
    y0 = max(0, previous.cy - radius)
    x1 = min(width, previous.cx + radius)
    y1 = min(height, previous.cy + radius)
    if x1 - x0 < 20 or y1 - y0 < 20:
        return None
    return image[y0:y1, x0:x1], (x0, y0)


def _best(image: np.ndarray, template: np.ndarray, scales: tuple[float, ...], minimum: float) -> BobberHit | None:
    height, width = image.shape[:2]
    scale_down = 1.0
    work = image
    templ = template
    if width > 1100:
        scale_down = 1100 / width
        work = cv2.resize(
            image,
            (int(width * scale_down), int(height * scale_down)),
            interpolation=cv2.INTER_AREA,
        )
        templ = _limit(template, max(12, int(max(template.shape[:2]) * scale_down)))
    found = _search(work, templ, scales)
    if found is None or found[4] < minimum:
        return None
    left, top, box_w, box_h, score = found
    if scale_down != 1.0:
        left = int(round(left / scale_down))
        top = int(round(top / scale_down))
        box_w = max(1, int(round(box_w / scale_down)))
        box_h = max(1, int(round(box_h / scale_down)))
    # _water cropped the top 8%. The caller of _best on a window passes origin separately.
    return _hit(left, top, box_w, box_h, score, image.shape[1], image.shape[0])


def _search(image: np.ndarray, template: np.ndarray, scales: tuple[float, ...]):
    height, width = image.shape[:2]
    best = None
    for scale in scales:
        box_w = int(round(template.shape[1] * scale))
        box_h = int(round(template.shape[0] * scale))
        if box_w < 10 or box_h < 10 or box_w >= width or box_h >= height:
            continue
        sized = template
        if box_w != template.shape[1] or box_h != template.shape[0]:
            sized = cv2.resize(
                template,
                (box_w, box_h),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR,
            )
        result = cv2.matchTemplate(image, sized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        score = float(max_val) * 100.0
        if best is None or score > best[4]:
            best = (max_loc[0], max_loc[1], box_w, box_h, score)
    return best


def _hit(left: int, top: int, box_w: int, box_h: int, score: float, width: int, height: int) -> BobberHit:
    click_x = min(width - 1, max(0, left + box_w // 2))
    click_y = min(height - 1, max(0, top + int(box_h * 0.58)))
    return BobberHit(
        cx=click_x,
        cy=min(height - 1, max(0, top + box_h // 2)),
        click_x=click_x,
        click_y=click_y,
        left=left,
        top=top,
        width=box_w,
        height=box_h,
        score=score,
        visual=score,
    )


def _shift(hit: BobberHit, origin: tuple[int, int]) -> BobberHit:
    x0, y0 = origin
    return BobberHit(
        cx=hit.cx + x0,
        cy=hit.cy + y0,
        click_x=hit.click_x + x0,
        click_y=hit.click_y + y0,
        left=hit.left + x0,
        top=hit.top + y0,
        width=hit.width,
        height=hit.height,
        score=hit.score,
        visual=hit.visual,
    )
