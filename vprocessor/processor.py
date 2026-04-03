import asyncio
import contextlib
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
from config import Config
from detector import PersonDetector
from recorder import VideoRecorder

logger = logging.getLogger(__name__)

RTSP_RETRY_DELAY = 2.0  # seconds between reconnect attempts
RTSP_READ_TIMEOUT_MS = 5_000  # ms – max time a single cap.read() may block
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

    The OpenCV loop runs inside a ``ThreadPoolExecutor`` as a plain synchronous
    function so it never blocks the asyncio event loop.  The main event loop
    is captured in :meth:`start` and stored as ``_main_loop`` so that
    Socket.IO coroutines can be scheduled onto it via
    ``asyncio.run_coroutine_threadsafe``.

    Shutdown contract
    -----------------
    Calling :meth:`stop` does three things in order so the worker thread exits
    as quickly as possible:

    1. Sets ``self.running = False`` — the loop condition and the post-read
       guard both check this flag.
    2. Sets ``self._stop_event`` — any ``_stop_event.wait(timeout)`` call
       (used instead of ``time.sleep``) returns *immediately* rather than
       sleeping the full retry delay.
    3. Releases ``self._cap`` under ``_cap_lock`` — this closes the network
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
    # Public async API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Capture the running event loop and start the processing thread."""
        if self.running:
            logger.warning("VideoProcessor.start() called while already running.")
            return

        self.running = True
        self._stop_event.clear()
        self._start_time = time.time()
        self.status = "starting"

        # Capture the main event loop *before* entering the executor so that
        # the worker thread can schedule coroutines back onto it.
        self._main_loop = asyncio.get_running_loop()

        logger.info("Starting VideoProcessor …")
        await self._main_loop.run_in_executor(
            self._executor, self._sync_processing_loop
        )

    async def stop(self) -> None:
        """Signal the processing loop to stop and wait for the thread to exit.

        Steps are ordered to minimise the time between this call and the
        worker thread actually exiting:

        1. ``self.running = False`` — loop guard + post-read guard.
        2. ``_stop_event.set()`` — wakes interruptible sleeps instantly.
        3. ``self._cap.release()`` — closes the RTSP socket so cap.read()
           returns immediately instead of blocking until the next frame.
        4. ``executor.shutdown(wait=True)`` (offloaded so the event loop is
           not blocked while we wait for the thread to finish).
        """
        logger.info("Stopping VideoProcessor …")
        self.running = False
        self.status = "stopped"

        # Wake any thread sleeping inside _stop_event.wait().
        self._stop_event.set()

        # Release the VideoCapture so that a blocking cap.read() returns
        # immediately with ret=False.  Guarded by a lock so we don't race
        # with the worker thread swapping out self._cap on reconnect.
        with self._cap_lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception as exc:  # pragma: no cover
                    logger.debug("Ignoring error while releasing cap on stop: %s", exc)
                self._cap = None

        # Run the blocking shutdown in the default thread-pool so the event
        # loop stays responsive while we wait for the worker thread to exit.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._executor.shutdown, True)
        logger.info("VideoProcessor stopped.")

    def get_latest_frame(self) -> bytes | None:
        """Return the most recent JPEG-encoded frame (thread-safe)."""
        with self._frame_lock:
            return self.latest_frame

    # ------------------------------------------------------------------
    # Internal – runs in the ThreadPoolExecutor worker thread
    # ------------------------------------------------------------------

    def _sync_processing_loop(self) -> None:
        """Purely synchronous OpenCV / YOLO loop – safe to run in any thread.

        All I/O that must reach the async world (Socket.IO events) is
        dispatched via ``asyncio.run_coroutine_threadsafe``.
        """
        # Local reference to the VideoCapture kept in sync with self._cap so
        # that stop() can release it externally without a use-after-free.
        cap: cv2.VideoCapture | None = None

        fps_frame_count: int = 0
        fps_timer: float = time.time()
        last_stats_time: float = time.time()

        try:
            while self.running:
                # ── Connect / reconnect ────────────────────────────────
                if cap is None or not cap.isOpened():
                    if cap is not None:
                        # Clear the shared reference before releasing so that
                        # stop() doesn't try to release an already-gone cap.
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

                # ── Read a frame ───────────────────────────────────────
                ret, frame = cap.read()

                # Check the stop flag before starting any heavy work.
                # stop() may have released the cap from outside, which
                # causes cap.read() to return (False, None); we want to
                # exit cleanly rather than log a spurious reconnect warning.
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

                # Update FPS counter
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
            # ── Clean up on exit ───────────────────────────────────────
            # stop() may have already released self._cap; releasing the local
            # reference again is harmless (VideoCapture.release() is idempotent).
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                with self._cap_lock:
                    self._cap = None
                logger.info("VideoCapture released.")

            try:
                self.recorder.release()
            except Exception as exc:
                logger.error("Error releasing recorder: %s", exc)

            logger.info("Processing loop exited (status=%s).", self.status)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, coro) -> None:
        """Schedule a Socket.IO coroutine onto the main event loop.

        Safe to call from any thread.  If the main loop is not yet available
        (e.g. called before :meth:`start`) the coroutine is silently dropped.
        """
        if self._main_loop is None or self._main_loop.is_closed():
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
