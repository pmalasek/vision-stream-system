import asyncio
import contextlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
from config import Config
from detector import PersonDetector
from recorder import VideoRecorder

logger = logging.getLogger(__name__)

RTSP_RETRY_DELAY = 2.0  # seconds between reconnect attempts
RTSP_READ_TIMEOUT_MS = 2_000  # ms – max time a single cap.read() may block
JPEG_QUALITY = 85  # cv2 JPEG encode quality (0-100)

# Suppress OpenCV's own C-level WARN messages (e.g. "backend is generally
# available but can't be used to capture by name") that bypass Python logging.
# Use integer 2 directly (= ERROR in OpenCV's LogLevel enum) because the
# cv2.LOG_LEVEL_* constants are not exposed in all OpenCV builds/versions.
cv2.setLogLevel(2)  # 0=SILENT 1=FATAL 2=ERROR 3=WARNING 4=INFO 5=DEBUG


@contextlib.contextmanager
def _suppress_c_stderr():
    """Redirect raw file-descriptor 2 to /dev/null for the duration of the block.

    FFmpeg (and other C extensions) write connection-error diagnostics directly
    to fd 2, completely bypassing Python's ``sys.stderr`` and the ``logging``
    module.  The only reliable way to silence them is to temporarily point fd 2
    at ``/dev/null`` at the OS level.

    The original fd is saved with ``os.dup`` and restored unconditionally in
    the ``finally`` clause, so the context is always safe to use even if the
    wrapped code raises.

    Usage::

        with _suppress_c_stderr():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    """
    saved_fd = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)
        os.close(devnull)


class VideoProcessor:
    """Drives the capture → detect → annotate → record → broadcast pipeline.

    Two-thread design
    -----------------
    The pipeline is split across two dedicated worker threads to prevent YOLO
    inference latency (~100 ms) from starving the RTSP network buffer drain
    (~33 ms/frame):

    * **Frame grabber** (``_grabber_executor``) — runs
      :meth:`_sync_frame_grabber`, which owns all RTSP
      connection/reconnection logic and calls ``cap.read()`` in a tight loop.
      Each successfully decoded frame is written to ``_latest_raw_frame`` and
      ``_raw_frame_event`` is set so the processing thread can pick it up.

    * **Processing thread** (``_executor``) — runs
      :meth:`_sync_processing_loop`, which waits on ``_raw_frame_event``,
      takes the latest raw frame, runs YOLO inference, encodes the result to
      JPEG, writes to disk, and emits Socket.IO events.

    Both threads run inside ``ThreadPoolExecutor`` instances so they never
    block the asyncio event loop.  The main event loop is captured in
    :meth:`start` and stored as ``_main_loop`` so that Socket.IO coroutines
    can be scheduled onto it via ``asyncio.run_coroutine_threadsafe``.

    Shutdown contract
    -----------------
    Calling :meth:`stop` does the following so both worker threads exit as
    quickly as possible:

    1. Sets ``self.running = False`` — the loop condition in both threads
       checks this flag.
    2. Sets ``self._stop_event`` — any ``_stop_event.wait(timeout)`` call
       (used instead of ``time.sleep`` in the grabber) returns *immediately*
       rather than sleeping the full retry delay.
    3. Sets ``self._raw_frame_event`` — wakes the processing thread
       immediately if it is currently blocking inside
       ``_raw_frame_event.wait()``, preventing it from waiting the full 0.5 s
       timeout.
    4. Releases ``self._cap`` under ``_cap_lock`` — this closes the network
       socket so the blocking ``cap.read()`` call inside the FFmpeg decoder
       returns right away with ``ret=False`` instead of waiting for the next
       frame to arrive.  Without this step, the FFmpeg threads keep decoding
       and printing H.264 error messages until the OS kills the process.

    ``latest_frame`` is guarded by a ``threading.Lock`` because it is written
    from the worker thread and read from async HTTP handlers running on the
    main event loop.
    """

    def __init__(
        self,
        config: Config,
        detector: PersonDetector,
        recorder: VideoRecorder,
        sio,  # socketio.AsyncServer
    ) -> None:
        self.config = config
        self.detector = detector
        self.recorder = recorder
        self.sio = sio

        self.running: bool = False
        self.frame_count: int = 0
        self.total_detections: int = 0

        self._frame_lock = threading.Lock()
        self.latest_frame: bytes | None = None

        self.latest_detections: list[dict] = []
        self.fps: float = 0.0
        self.status: str = "idle"

        self._start_time: float = 0.0
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vproc")

        # ------------------------------------------------------------------
        # Shutdown coordination
        # ------------------------------------------------------------------
        # _stop_event replaces bare time.sleep() in the loop so that stop()
        # can wake the thread immediately instead of waiting up to
        # RTSP_RETRY_DELAY seconds for the sleep to expire.
        self._stop_event = threading.Event()

        # _cap / _cap_lock let stop() release the VideoCapture from outside
        # the worker thread, which interrupts any blocking cap.read() call
        # so FFmpeg stops decoding (and printing errors) right away.
        self._cap: cv2.VideoCapture | None = None
        self._cap_lock = threading.Lock()

        # ------------------------------------------------------------------
        # Frame grabber – dedicated thread that drains the RTSP buffer
        # continuously so YOLO inference never blocks the network read.
        # ------------------------------------------------------------------
        self._grabber_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="grabber"
        )

        # Latest raw frame shared between the grabber and the processing
        # thread.  Guarded by _raw_frame_lock; always overwritten with the
        # newest frame.
        self._latest_raw_frame: np.ndarray | None = None
        self._raw_frame_lock = threading.Lock()

        # Set by the grabber whenever a new frame is stored; cleared by the
        # processing thread after it takes the frame.  Also set by stop() to
        # immediately unblock the processing thread.
        self._raw_frame_event = threading.Event()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Capture the running event loop and start the processing thread."""
        if self.running:
            logger.warning("VideoProcessor.start() called while already running.")
            return

        self.running = True
        self._stop_event.clear()
        self._raw_frame_event.clear()
        self._start_time = time.time()
        self.status = "starting"

        # Capture the main event loop *before* entering the executor so that
        # the worker thread can schedule coroutines back onto it.
        self._main_loop = asyncio.get_running_loop()

        logger.info("Starting VideoProcessor …")

        grabber = self._main_loop.run_in_executor(
            self._grabber_executor, self._sync_frame_grabber
        )
        processor = self._main_loop.run_in_executor(
            self._executor, self._sync_processing_loop
        )

        # Await both threads.  return_exceptions=True ensures that if one
        # raises the other is still awaited before start() returns.
        results = await asyncio.gather(grabber, processor, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Thread exited with exception: %s", r)

    async def stop(self) -> None:
        """Signal the processing loop to stop.

        This method only *signals* – it does **not** block waiting for the
        worker threads to exit.  The caller (``main.py`` lifespan) is
        responsible for waiting by monitoring ``_processor_task``.

        Steps
        -----
        1. ``self.running = False`` — loop guard in both threads.
        2. ``_stop_event.set()`` — wakes every ``_stop_event.wait()`` call
           (used instead of ``time.sleep``) so they return immediately.
        3. ``_raw_frame_event.set()`` — wakes the processing thread so it
           exits from ``_raw_frame_event.wait()`` immediately instead of
           waiting the full 0.5 s timeout.
        4. ``_cap.release()`` — closes the RTSP socket so a blocking
           ``cap.read()`` returns right away with ``ret=False``.
           Guarded by ``_cap_lock`` to avoid racing with the reconnect
           section of the grabber thread.
        5. Both ``executor`` and ``grabber_executor`` are shut down
           (non-blocking).
        """
        logger.info("Stopping VideoProcessor …")
        self.running = False
        self.status = "stopped"

        # Wake any thread sleeping inside _stop_event.wait().
        self._stop_event.set()

        # Wake the processing thread so it exits from _raw_frame_event.wait()
        # immediately instead of waiting the full 0.5 s timeout.
        self._raw_frame_event.set()

        # Release the VideoCapture so that a blocking cap.read() returns
        # immediately with ret=False.  Guarded by a lock so we don't race
        # with the grabber thread swapping out self._cap on reconnect.
        with self._cap_lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception as exc:  # pragma: no cover
                    logger.debug("Ignoring error while releasing cap on stop: %s", exc)
                self._cap = None

        # Mark both executors as shut-down (non-blocking).  The worker
        # threads will exit naturally after seeing running=False / events
        # being set; the executor pools are freed once that happens.
        self._executor.shutdown(wait=False)
        self._grabber_executor.shutdown(wait=False)
        logger.info("VideoProcessor stop signal sent.")

    def get_latest_frame(self) -> bytes | None:
        """Return the most recent JPEG-encoded frame (thread-safe)."""
        with self._frame_lock:
            return self.latest_frame

    # ------------------------------------------------------------------
    # Internal – runs in the ThreadPoolExecutor worker threads
    # ------------------------------------------------------------------

    def _sync_frame_grabber(self) -> None:
        """Continuously reads raw frames from the RTSP stream.

        Runs in a dedicated thread so that ``cap.read()`` is never blocked by
        YOLO inference.  Each acquired frame overwrites ``_latest_raw_frame``
        and sets ``_raw_frame_event`` so the processing thread can pick it up.
        """
        cap: cv2.VideoCapture | None = None

        try:
            while self.running:
                # ── Connect / reconnect ────────────────────────────────
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        with self._cap_lock:
                            self._cap = None
                        cap.release()
                        cap = None

                    logger.info("Connecting to RTSP stream: %s …", self.config.RTSP_URL)
                    self.status = "connecting"

                    # Tell the embedded FFmpeg to use the configured transport
                    # (TCP by default) so that UDP packet loss cannot produce
                    # H.264 "corrupted macroblock" decoder warnings.
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                        f"rtsp_transport;{self.config.RTSP_TRANSPORT}"
                    )
                    # Wrap in _suppress_c_stderr so that FFmpeg's low-level
                    # "[tcp @ ...] Connection refused" messages (written
                    # directly to fd 2) don't pollute the uvicorn log output.
                    with _suppress_c_stderr():
                        new_cap = cv2.VideoCapture(self.config.RTSP_URL, cv2.CAP_FFMPEG)

                    if not new_cap.isOpened():
                        logger.warning(
                            "Cannot open RTSP stream '%s'. Retrying in %.1fs …",
                            self.config.RTSP_URL,
                            RTSP_RETRY_DELAY,
                        )
                        new_cap.release()
                        # Interruptible wait – stop() sets the event so this
                        # returns True immediately instead of sleeping 2 s.
                        if self._stop_event.wait(RTSP_RETRY_DELAY):
                            break
                        continue

                    # Set a hard upper bound on how long a single cap.read()
                    # may block.  This acts as a backstop in case the external
                    # release from stop() doesn't interrupt the call in time.
                    new_cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, RTSP_READ_TIMEOUT_MS)
                    try:
                        # Available in OpenCV >= 4.6; silently skip on older
                        # builds rather than crashing the whole pipeline.
                        new_cap.set(
                            cv2.CAP_PROP_READ_TIMEOUT_MSEC, RTSP_READ_TIMEOUT_MS
                        )
                    except Exception:
                        pass

                    # Publish the new cap so stop() can release it externally.
                    cap = new_cap
                    with self._cap_lock:
                        self._cap = cap

                    logger.info("RTSP stream opened successfully.")
                    self.status = "streaming"

                # Guard: bail out before starting a new read after stop().
                if self._stop_event.is_set():
                    break

                # ── Read a frame ───────────────────────────────────────
                ret, frame = cap.read()

                if not self.running:
                    break

                if not ret or frame is None:
                    logger.warning(
                        "Failed to read frame – stream may have dropped. "
                        "Retrying in %.1fs …",
                        RTSP_RETRY_DELAY,
                    )
                    with self._cap_lock:
                        self._cap = None
                    cap.release()
                    cap = None
                    self.status = "reconnecting"
                    # Interruptible sleep – exits immediately if stop() fires.
                    if self._stop_event.wait(RTSP_RETRY_DELAY):
                        break
                    continue

                # ── Share with processing thread ───────────────────────
                # Always overwrite so the processor always gets the latest
                # frame; the old frame is simply dropped if the processor
                # hasn't consumed it yet.
                with self._raw_frame_lock:
                    self._latest_raw_frame = frame
                self._raw_frame_event.set()

        except Exception as exc:
            logger.exception("Unexpected error in frame grabber: %s", exc)
            self.status = "error"

        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                with self._cap_lock:
                    self._cap = None
                logger.info("VideoCapture released (grabber).")

            # Wake the processing thread so it can exit cleanly even if it
            # is currently waiting inside _raw_frame_event.wait().
            self._raw_frame_event.set()
            logger.info("Frame grabber exited (status=%s).", self.status)

    def _sync_processing_loop(self) -> None:
        """Runs YOLO inference on the latest raw frame from ``_sync_frame_grabber``.

        Deliberately decoupled from RTSP reading so that inference latency
        never starves the network buffer drain in the grabber thread.
        """
        fps_frame_count: int = 0
        fps_timer: float = time.time()
        last_stats_time: float = time.time()

        try:
            while self.running:
                # ── Wait for a new raw frame ───────────────────────────
                # Timeout of 0.5 s lets us re-check self.running periodically
                # even when no frames arrive (e.g. stream not yet connected).
                if not self._raw_frame_event.wait(timeout=0.5):
                    continue
                self._raw_frame_event.clear()

                with self._raw_frame_lock:
                    frame = self._latest_raw_frame

                if frame is None or not self.running:
                    continue

                self.frame_count += 1
                fps_frame_count += 1
                timestamp = time.time()

                # ── Detect persons ─────────────────────────────────────
                annotated_frame, detections = self.detector.detect(frame)
                self.latest_detections = detections
                self.total_detections += len(detections)

                # ── Encode to JPEG and store ───────────────────────────
                ok, buffer = cv2.imencode(
                    ".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                if ok:
                    jpeg_bytes = buffer.tobytes()
                    with self._frame_lock:
                        self.latest_frame = jpeg_bytes

                # ── Write to disk ──────────────────────────────────────
                self.recorder.write_frame(
                    frame=annotated_frame,
                    frame_id=self.frame_count,
                    detections=detections,
                    timestamp=timestamp,
                )

                # ── Emit Socket.IO events ──────────────────────────────
                detection_payload = {
                    "frame_id": self.frame_count,
                    "timestamp": timestamp,
                    "person_count": len(detections),
                    "detections": detections,
                }
                self._emit(self.sio.emit("detection", detection_payload))

                # ── Update FPS counter ─────────────────────────────────
                now = time.time()
                elapsed_fps = now - fps_timer
                if elapsed_fps >= 1.0:
                    self.fps = round(fps_frame_count / elapsed_fps, 2)
                    fps_frame_count = 0
                    fps_timer = now

                # Emit stats once per second
                if now - last_stats_time >= 1.0:
                    self._emit(self.sio.emit("stats", self._build_stats_payload()))
                    last_stats_time = now

        except Exception as exc:
            logger.exception("Unexpected error in processing loop: %s", exc)
            self.status = "error"

        finally:
            try:
                self.recorder.release()
            except Exception as exc:
                logger.error("Error releasing recorder: %s", exc)

            logger.info("Processing loop exited.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, coro) -> None:
        """Schedule a Socket.IO coroutine onto the main event loop.

        Safe to call from any thread.  If the main loop is not yet available
        (e.g. called before :meth:`start`) the coroutine is silently dropped.
        """
        if self._main_loop is None or self._main_loop.is_closed():
            # The event loop is gone – discard the coroutine explicitly so
            # Python does not emit "coroutine was never awaited" warnings.
            coro.close()
            return
        asyncio.run_coroutine_threadsafe(coro, self._main_loop)

    def _build_stats_payload(self) -> dict:
        uptime = time.time() - self._start_time if self._start_time else 0.0
        return {
            "fps": self.fps,
            "total_frames": self.frame_count,
            "total_detections": self.total_detections,
            "uptime_seconds": round(uptime, 2),
            "status": self.status,
        }
