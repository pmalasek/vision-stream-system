import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

import socketio
from config import config
from detector import PersonDetector
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from processor import VideoProcessor
from recorder import VideoRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Socket.IO server
# ---------------------------------------------------------------------------

sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

# ---------------------------------------------------------------------------
# Global processor (populated in lifespan)
# ---------------------------------------------------------------------------

processor: VideoProcessor | None = None
_processor_task: asyncio.Task | None = None
_start_time: float = 0.0
_connected_clients: int = 0

# Reference to the running uvicorn.Server instance.  Set by the __main__
# entry-point so the MJPEG /stream generator can read server.should_exit
# and close the HTTP response before the lifespan cleanup needs to run.
# When the module is started via `python -m uvicorn` this stays None and
# the generator falls back to the normal infinite loop (still cancellable
# via asyncio task cancellation on force-exit).
_uvicorn_server = None  # uvicorn.Server | None

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global processor, _processor_task, _start_time

    logger.info("=== vprocessor starting up ===")
    _start_time = time.time()

    detector = PersonDetector(
        model_path=config.YOLO_MODEL,
        confidence=config.CONFIDENCE_THRESHOLD,
    )
    recorder = VideoRecorder(output_dir=config.OUTPUT_DIR)

    processor = VideoProcessor(
        config=config,
        detector=detector,
        recorder=recorder,
        sio=sio,
    )

    # Start the processing loop as a background task so startup completes
    # immediately and the server can begin accepting requests.
    _processor_task = asyncio.create_task(processor.start(), name="video-processor")

    yield  # ── application is running ──────────────────────────────────

    logger.info("=== vprocessor shutting down ===")

    if processor is not None:
        # Signal the worker thread to stop (sets running=False, fires
        # _stop_event, releases _cap).  Does NOT block – we wait below.
        await processor.stop()

    if _processor_task is not None:
        if not _processor_task.done():
            # Give the worker thread up to 8 s to exit gracefully before
            # we escalate to a hard cancel.  asyncio.wait() never cancels
            # tasks on its own, so the thread keeps its chance to clean up.
            done, _ = await asyncio.wait({_processor_task}, timeout=8.0)
            if not done:
                logger.warning(
                    "Processor task did not stop within 8 s – force-cancelling."
                )
                _processor_task.cancel()

        # Await the task to consume its result/exception so asyncio doesn't
        # log "Task exception was never retrieved" warnings.
        with contextlib.suppress(Exception):
            await _processor_task

    logger.info("=== vprocessor shutdown complete ===")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="vprocessor",
    description="Person-detection video processing service",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Socket.IO ASGI wrapper – this is what uvicorn actually serves
# ---------------------------------------------------------------------------

socket_app = socketio.ASGIApp(sio, app)

# ---------------------------------------------------------------------------
# Socket.IO event handlers
# ---------------------------------------------------------------------------


@sio.event
async def connect(sid: str, environ: dict, auth=None):
    global _connected_clients
    _connected_clients += 1
    logger.info("Socket.IO client connected: %s (total=%d)", sid, _connected_clients)
    if processor is not None:
        status_payload = {
            "fps": processor.fps,
            "total_frames": processor.frame_count,
            "total_detections": processor.total_detections,
            "uptime_seconds": round(time.time() - _start_time, 2),
            "status": processor.status,
        }
        await sio.emit("stats", status_payload, to=sid)


@sio.event
async def disconnect(sid: str):
    global _connected_clients
    _connected_clients = max(0, _connected_clients - 1)
    logger.info("Socket.IO client disconnected: %s (total=%d)", sid, _connected_clients)


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@app.get("/stream", tags=["video"])
async def mjpeg_stream():
    """MJPEG stream of the processed (annotated) video.

    Suitable for embedding directly in an ``<img src="/stream">`` tag.
    """

    async def generate():
        # Stop as soon as uvicorn signals it wants to shut down.
        # Exiting here closes the HTTP connection, which unblocks uvicorn's
        # "waiting for connections" phase so the lifespan cleanup (and
        # therefore processor.stop()) can actually run — without the caller
        # having to press Ctrl-C a second time.
        while _uvicorn_server is None or not _uvicorn_server.should_exit:
            if processor is None:
                await asyncio.sleep(0.1)
                continue

            frame = processor.get_latest_frame()
            if frame:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            await asyncio.sleep(1 / 30)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/health", tags=["monitoring"])
async def health():
    """Liveness / readiness probe."""
    if processor is None:
        return {
            "status": "starting",
            "uptime": 0.0,
            "fps": 0.0,
            "connected_clients": _connected_clients,
        }

    return {
        "status": processor.status,
        "uptime": round(time.time() - _start_time, 2),
        "fps": processor.fps,
        "connected_clients": _connected_clients,
    }


@app.get("/api/stats", tags=["monitoring"])
async def api_stats():
    """Current processing statistics snapshot."""
    if processor is None:
        return {
            "fps": 0.0,
            "total_frames": 0,
            "total_detections": 0,
            "uptime_seconds": 0.0,
            "status": "starting",
        }

    return {
        "fps": processor.fps,
        "total_frames": processor.frame_count,
        "total_detections": processor.total_detections,
        "uptime_seconds": round(time.time() - _start_time, 2),
        "status": processor.status,
    }


@app.get("/api/detections", tags=["detections"])
async def api_detections(limit: int = 100, offset: int = 0):
    """Read persisted detection records from the current session JSONL file.

    Args:
        limit:  Maximum number of records to return (default 100).
        offset: Number of records to skip from the start (default 0).
    """
    import json

    import aiofiles

    if processor is None or processor.recorder is None:
        return []

    metadata_path = processor.recorder.metadata_path

    try:
        async with aiofiles.open(metadata_path, "r", encoding="utf-8") as fh:
            lines = await fh.readlines()
    except FileNotFoundError:
        return []
    except Exception as exc:
        logger.error("Error reading detections file: %s", exc)
        return []

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return records[offset : offset + limit]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Build the server object before calling run() so that the global
    # _uvicorn_server reference is available to the MJPEG generator from
    # the very first request.
    _cfg = uvicorn.Config(
        socket_app,
        host=config.HOST,
        port=config.PORT,
        # Safety net: if open connections (e.g. MJPEG /stream) have not
        # closed themselves within 3 s after the first SIGINT, uvicorn
        # force-closes them so the lifespan cleanup can proceed.
        # In practice the /stream generator exits in ≤33 ms once it reads
        # server.should_exit = True, so this timeout is almost never hit.
        timeout_graceful_shutdown=3,
    )
    _uvicorn_server = uvicorn.Server(_cfg)
    _uvicorn_server.run()
