"""API locale : la page lance la séquence, ce processus fait les actions."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fisher.audio import listener
from fisher.bobber_ref import REFERENCE_PATH, save_bobber_image
from fisher.bot import DEBUG_PATH, Settings, detect_once, fisher, recorder
from fisher.overlay import hud


@asynccontextmanager
async def lifespan(_app: FastAPI):
    hud.start()
    pump = asyncio.create_task(_pump_audio())
    try:
        yield
    finally:
        pump.cancel()
        listener.stop()
        recorder.request_stop("l'arrêt du serveur")
        hud.stop()


async def _pump_audio() -> None:
    was_active = False
    while True:
        active = fisher.running or listener.listening or recorder.recording
        if active or was_active:
            try:
                fisher.publish_audio()
            except Exception:
                pass
        was_active = active
        await asyncio.sleep(0.08 if active else 0.4)


app = FastAPI(title="Pêche WoW", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConfidenceBody(BaseModel):
    confidence: float = Field(70, ge=20, le=95)


class BobberImageBody(BaseModel):
    image: str = Field(min_length=16)


class StartBody(BaseModel):
    pre_delay: float = Field(2.0, ge=0, le=15)
    loop: bool = True
    confidence: float = Field(70, ge=20, le=95)
    dip_pixels: int = Field(24, ge=6, le=80)
    bite_timeout: float = Field(22.0, ge=8, le=40)
    loot_delay: float = Field(1.8, ge=0.4, le=8)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    return fisher.snapshot()


@app.post("/api/confidence")
def confidence(body: ConfidenceBody):
    listener.set_confidence(body.confidence)
    return {"ok": True, "confidence_threshold": listener.threshold}


@app.post("/api/start")
def start(body: StartBody):
    if recorder.recording:
        raise HTTPException(status_code=409, detail="Une capture sonore est en cours")
    if not fisher.start(Settings.from_dict(body.model_dump())):
        raise HTTPException(status_code=409, detail="La pêche est déjà en cours")
    return {"ok": True}


@app.post("/api/capture")
def capture_sound():
    if fisher.running:
        raise HTTPException(status_code=409, detail="Arrête la pêche avant de capturer le son")
    reason = recorder.start()
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True}


@app.post("/api/stop")
def stop():
    fisher.request_stop("la page")
    recorder.request_stop("la page")
    listener.stop()
    recorder.wait(3)
    hud.hide()
    return {"ok": True, "last_capture": recorder.last_path}


@app.post("/api/bobber")
def upload_bobber(body: BobberImageBody):
    try:
        saved = save_bobber_image(body.image)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    fisher.publish_audio()
    return saved


@app.get("/api/bobber.png")
def bobber_image():
    if not REFERENCE_PATH.exists():
        raise HTTPException(status_code=404, detail="Pas encore d'image du bouchon")
    return FileResponse(
        REFERENCE_PATH,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/detect")
def detect():
    if fisher.running:
        raise HTTPException(status_code=409, detail="Arrête la pêche avant de tester le repérage")
    return detect_once()


@app.get("/api/debug.jpg")
def debug_image():
    if not DEBUG_PATH.exists():
        raise HTTPException(status_code=404, detail="Pas encore d'image")
    return FileResponse(
        DEBUG_PATH,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.websocket("/ws")
async def stream(websocket: WebSocket):
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    fisher.subscribe(loop, queue)
    try:
        await websocket.send_json(fisher.snapshot())
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        fisher.unsubscribe(queue)
