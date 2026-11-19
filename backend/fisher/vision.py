"""Trouve le bouchon : un petit amas de plumes rouges, vertes et bleues sur l'eau."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class BobberHit:
    cx: int
    cy: int
    click_x: int
    click_y: int
    left: int
    top: int
    width: int
    height: int
    score: float
    visual: float | None = None

    def moved_far_from(self, previous: "BobberHit | None") -> bool:
        if previous is None:
            return False
        return abs(self.cx - previous.cx) > 70 or self.cy < previous.cy - 40


def locate_bobber(image_bgr: np.ndarray, previous: BobberHit | None = None) -> BobberHit | None:
    """Coordonnées dans l'image d'origine. `previous` sert à suivre le même bouchon."""
    from fisher.bobber_ref import find_with_reference

    ready, referenced = find_with_reference(image_bgr, previous)
    if ready:
        return referenced
    height, width = image_bgr.shape[:2]
    scale = 1.0
    work = image_bgr
    if width > 1280:
        scale = 1280 / width
        work = cv2.resize(
            image_bgr,
            (int(width * scale), int(height * scale)),
            interpolation=cv2.INTER_AREA,
        )
    prev = _scale_hit(previous, scale) if previous else None
    hit = _locate(work, prev)
    if hit is None or scale == 1.0:
        return hit
    return _scale_hit(hit, 1.0 / scale)


def annotate(image_bgr: np.ndarray, hit: BobberHit | None) -> np.ndarray:
    out = image_bgr.copy()
    if hit is None:
        return out
    cv2.rectangle(
        out,
        (hit.left, hit.top),
        (hit.left + hit.width, hit.top + hit.height),
        (80, 220, 120),
        2,
    )
    cv2.circle(out, (hit.click_x, hit.click_y), 14, (40, 170, 255), 2)
    cv2.drawMarker(out, (hit.click_x, hit.click_y), (40, 170, 255), cv2.MARKER_CROSS, 18, 2)
    return out


def _scale_hit(hit: BobberHit | None, scale: float) -> BobberHit | None:
    if hit is None:
        return None
    def s(value: int) -> int:
        return int(round(value * scale))

    return BobberHit(
        cx=s(hit.cx),
        cy=s(hit.cy),
        click_x=s(hit.click_x),
        click_y=s(hit.click_y),
        left=s(hit.left),
        top=s(hit.top),
        width=max(1, s(hit.width)),
        height=max(1, s(hit.height)),
        score=hit.score,
        visual=hit.visual,
    )


def _locate(image_bgr: np.ndarray, previous: BobberHit | None, mask_ui: bool = True) -> BobberHit | None:
    height, width = image_bgr.shape[:2]
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    # L'eau est bleue et très saturée : on ne cherche pas le bleu.
    # Le leurre se distingue par des plumes rouges ET vertes, même peu lumineuses.
    red = (((hue <= 18) | (hue >= 160)) & (sat >= 70) & (val >= 28))
    green = ((hue >= 32) & (hue <= 92) & (sat >= 50) & (val >= 28))
    chromatic = red | green

    mask = chromatic.astype(np.uint8) * 255
    # Barres d'action, portraits et minicarte : très colorés, jamais le bouchon.
    if mask_ui:
        mask[: int(height * 0.10), :] = 0
        mask[int(height * 0.84) :, :] = 0
        mask[: int(height * 0.36), int(width * 0.78) :] = 0

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)

    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    candidates: list[BobberHit] = []
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < 16 or area > 4500:
            continue
        left = int(stats[index, cv2.CC_STAT_LEFT])
        top = int(stats[index, cv2.CC_STAT_TOP])
        box_w = int(stats[index, cv2.CC_STAT_WIDTH])
        box_h = int(stats[index, cv2.CC_STAT_HEIGHT])
        if box_w < 8 or box_h < 8 or box_w > 200 or box_h > 220:
            continue
        aspect = box_w / box_h
        if aspect > 3.2 or aspect < 0.28:
            continue

        region = labels[top : top + box_h, left : left + box_w] == index
        red_n = int(red[top : top + box_h, left : left + box_w][region].sum())
        green_n = int(green[top : top + box_h, left : left + box_w][region].sum())
        colored = red_n + green_n
        if red_n < 5 or green_n < 5 or colored < 24:
            continue
        if red_n < colored * 0.12 or green_n < colored * 0.12:
            continue

        cx, cy = centroids[index]
        # Le flotteur clair est sous les plumes : on clique un peu plus bas que le centre coloré.
        click_x = int(round(cx))
        click_y = int(round(top + box_h * 0.72))
        click_y = min(height - 1, max(0, click_y))
        center_dist = math.hypot((cx - width / 2) / (width / 2), (cy - height / 2) / (height / 2))
        balance = min(red_n, green_n) / colored
        score = 80 + balance * 40 + min(area, 900) / 30 - center_dist * 18
        candidates.append(
            BobberHit(
                cx=int(round(cx)),
                cy=int(round(cy)),
                click_x=click_x,
                click_y=click_y,
                left=left,
                top=top,
                width=box_w,
                height=box_h,
                score=score,
            )
        )

    if not candidates:
        return None

    if previous is not None:
        near = [hit for hit in candidates if math.hypot(hit.cx - previous.cx, hit.cy - previous.cy) < 100]
        if near:
            return min(near, key=lambda hit: math.hypot(hit.cx - previous.cx, hit.cy - previous.cy))
        return None

    return max(candidates, key=lambda hit: hit.score)
