"""
Vstupní bod FastAPI / Socket.IO aplikace pro službu vprocessor.

Tento modul zajišťuje:
- Sestavení FastAPI aplikace a Socket.IO serveru.
- Lifespan kontextový manažer, který při startu inicializuje detektor osob
  (YOLO), videorekordér a hlavní smyčku zpracování videa (VideoProcessor),
  a při ukončení provede čistý shutdown všech komponent.
- HTTP endpointy:
    GET /stream          – MJPEG stream anotovaného videa
    GET /health          – liveness/readiness sonda
    GET /api/stats       – aktuální statistiky zpracování
    GET /api/detections  – záznamy detekcí z JSONL souboru aktuální session
- Socket.IO event handlery pro připojení a odpojení klientů.
- Entry point pro přímé spuštění přes ``python main.py``.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from fractions import Fraction

import av
import cv2
import numpy as np
import socketio
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from config import config
from detector import PersonDetector
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from processor import VideoProcessor
from recorder import VideoRecorder

# ---------------------------------------------------------------------------
# Konfigurace logování
# ---------------------------------------------------------------------------

# Nastavení formátu logů pro celou aplikaci: časové razítko, úroveň,
# název loggeru a samotná zpráva.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Logger pro tento modul – použit ve všech funkčních blocích níže.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Socket.IO server
# ---------------------------------------------------------------------------

# Asynchronní Socket.IO server provozovaný jako ASGI aplikace.
# async_mode="asgi" zajišťuje integraci s asyncio event loop.
# CORS je povolen pro všechny origins ("*") – v produkci zpřísnit.
# Interní logy Socket.IO a Engine.IO jsou vypnuty (řídí se loggery výše).
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)

# ---------------------------------------------------------------------------
# Globální proměnné
# ---------------------------------------------------------------------------

# Hlavní instance VideoProcessoru; naplněna v lifespan při startu aplikace.
# Před inicializací je None – všechny handlery s ní musí počítat.
processor: VideoProcessor | None = None

# asyncio.Task obalující běžící smyčku VideoProcessoru (processor.start()).
# Udržujeme referenci, abychom mohli na konci lifespan korektně počkat
# na dokončení úlohy nebo ji v krajním případě zrušit.
_processor_task: asyncio.Task | None = None

# Časové razítko (time.time()) okamžiku startu aplikace.
# Slouží k výpočtu doby běhu (uptime) ve statistikách a health endpointu.
_start_time: float = 0.0

# Počet aktuálně připojených Socket.IO klientů.
# Inkrementován při připojení, dekrementován při odpojení.
_connected_clients: int = 0

# Aktivní WebRTC peer spojení (pc_id -> session).
_webrtc_sessions: dict[str, dict] = {}

# Reference na běžící instanci uvicorn.Server.
# Nastavuje ji __main__ entry point PŘED voláním server.run(), takže
# generátor MJPEG streamu může číst atribut server.should_exit a sám
# ukončit HTTP odpověď dříve, než lifespan cleanup potřebuje probít.
# Pokud je modul spuštěn přes ``python -m uvicorn`` (bez __main__),
# zůstane None a generátor se spolehne na běžnou nekonečnou smyčku
# (stále přerušitelnou zrušením asyncio tasku při force-exit).
_uvicorn_server = None  # uvicorn.Server | None


class WebRTCOffer(BaseModel):
    """SDP offer payload přijatý z browseru."""

    sdp: str
    type: str


class ProcessorVideoTrack(VideoStreamTrack):
    """WebRTC video track napojený na zpracované snímky z VideoProcessoru."""

    kind = "video"

    def __init__(self, video_processor: VideoProcessor):
        super().__init__()
        self._processor = video_processor
        self._last_seq = -1
        self._last_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        self._last_frame_id = 0
        self._last_timestamp = 0.0
        self._pts = 0
        self._time_base = Fraction(1, 90_000)

    @property
    def current_meta(self) -> dict:
        return {
            "frame_id": self._last_frame_id,
            "timestamp": self._last_timestamp,
        }

    async def recv(self):
        # Jemné pacing omezuje CPU spin a drží stabilní video výstup.
        await asyncio.sleep(1 / 30)

        jpeg_bytes, seq, frame_id, timestamp = (
            self._processor.get_latest_webrtc_frame_with_meta()
        )

        if jpeg_bytes and seq != self._last_seq:
            decoded = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
            if decoded is not None:
                self._last_frame = decoded
                self._last_seq = seq
                self._last_frame_id = frame_id
                self._last_timestamp = timestamp

        frame = av.VideoFrame.from_ndarray(self._last_frame, format="bgr24")
        self._pts += 3000  # 90_000 / 30 FPS
        frame.pts = self._pts
        frame.time_base = self._time_base
        return frame


async def _wait_for_ice_gathering(pc: RTCPeerConnection):
    """Počká na dokončení ICE gathering, aby odpověď obsahovala kandidáty."""
    if pc.iceGatheringState == "complete":
        return

    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    async def _on_ice_state_change():
        if pc.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=3.0)
    except TimeoutError:
        logger.warning("ICE gathering timeout, vracím partial SDP answer.")


async def _frame_meta_pump(
    pc: RTCPeerConnection, channel, track: ProcessorVideoTrack
) -> None:
    """Průběžně posílá frame_id/timestamp metadata přes WebRTC data channel."""
    last_frame_id = -1

    while True:
        if pc.connectionState in {"failed", "closed"}:
            return

        if channel.readyState == "open":
            meta = track.current_meta
            frame_id = int(meta.get("frame_id") or 0)
            if frame_id > 0 and frame_id != last_frame_id:
                channel.send(json.dumps(meta))
                last_frame_id = frame_id

        await asyncio.sleep(0.01)


async def _cleanup_webrtc_session(pc_id: str) -> None:
    """Uzavře WebRTC session a odstraní ji z registru."""
    session = _webrtc_sessions.pop(pc_id, None)
    if not session:
        return

    task = session.get("meta_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    pc = session.get("pc")
    if pc is not None:
        with contextlib.suppress(Exception):
            await pc.close()

# ---------------------------------------------------------------------------
# Lifespan kontextový manažer
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Správa životního cyklu FastAPI aplikace.

    Startup fáze (před ``yield``):
        - Zaznamenání času startu.
        - Inicializace PersonDetectoru (načtení YOLO modelu).
        - Inicializace VideoRecorderu (výstupní adresář pro záznamy).
        - Sestavení VideoProcessoru propojujícího všechny komponenty.
        - Spuštění hlavní zpracovatelské smyčky jako asyncio background task.

    Shutdown fáze (po ``yield``):
        - Signalizace procesoru k zastavení.
        - Čekání na korektní ukončení background tasku (max. 8 s).
        - V případě překročení timeoutu vynucené zrušení tasku.
        - Konzumace výsledku / výjimky tasku, aby asyncio nepsal varování.
    """
    global processor, _processor_task, _start_time

    logger.info("=== vprocessor starting up ===")

    # Uložení časového razítka startu pro pozdější výpočet uptime.
    _start_time = time.time()

    # --- Inicializace detektoru osob ---
    # PersonDetector načte YOLO model ze zadané cesty a nastaví práh
    # spolehlivosti detekcí dle konfigurace.
    # inference_scale=0.5 zmenšuje snímek na 50% rozlišení před YOLO detekci,
    # což výrazně zrychluje inferenci (~4x) s acceptabilní ztrátou přesnosti.
    detector = PersonDetector(
        model_path=config.YOLO_MODEL,
        confidence=config.CONFIDENCE_THRESHOLD,
        inference_scale=config.INFERENCE_SCALE,
        imgsz=config.YOLO_IMGSZ,
    )

    # --- Inicializace rekordéru videa ---
    # VideoRecorder se stará o ukládání anotovaných snímků / klipů
    # a průběžný zápis metadat detekcí do JSONL souboru.
    recorder = VideoRecorder(
        output_dir=config.OUTPUT_DIR,
        segment_duration_minutes=config.SEGMENT_DURATION_MINUTES,
        max_segments=config.MAX_SEGMENTS,
        metadata_flush_every=config.METADATA_FLUSH_EVERY,
        record_output=config.RECORD_OUTPUT,
    )

    # --- Sestavení VideoProcessoru ---
    # VideoProcessor propojuje zdroj videa, detektor, rekordér a Socket.IO
    # server. Přijímá globální konfiguraci, takže si sám načte adresu
    # streamu, FPS cíl a další parametry.
    processor = VideoProcessor(
        config=config,
        detector=detector,
        recorder=recorder,
        sio=sio,
    )

    # --- Spuštění zpracovatelské smyčky jako background task ---
    # Vytvoříme asyncio.Task, aby startup skončil okamžitě a server
    # mohl ihned začít přijímat HTTP / Socket.IO požadavky. Smyčka
    # běží souběžně s obsluhou požadavků po celou dobu života aplikace.
    _processor_task = asyncio.create_task(processor.start(), name="video-processor")

    # ── aplikace běží, obsluha požadavků ──────────────────────────────
    yield

    # ==================== SHUTDOWN SEKVENCE ====================
    logger.info("=== vprocessor shutting down ===")

    if processor is not None:
        # Odeslání signálu k zastavení procesoru:
        #   - nastaví interní příznak running=False,
        #   - spustí _stop_event (probouzí čekající smyčku),
        #   - uvolní OpenCV VideoCapture (_cap).
        # Tato volání jsou neblokující – samotné ukončení čekáme níže.
        await processor.stop()

    if _processor_task is not None:
        if not _processor_task.done():
            # Poskytneme worker threadu až 8 sekund na korektní ukončení.
            # asyncio.wait() samo tasky neruší – vlákno si zachovává šanci
            # řádně zavřít soubory, uvolnit zdroje apod.
            done, _ = await asyncio.wait({_processor_task}, timeout=8.0)
            if not done:
                # Timeout vypršel – přistoupíme k tvrdému zrušení tasku.
                logger.warning(
                    "Processor task did not stop within 8 s – force-cancelling."
                )
                _processor_task.cancel()

        # Vyčkáme na výsledek / výjimku tasku a potlačíme jakoukoli chybu.
        # Bez tohoto kroku by asyncio logoval varování
        # "Task exception was never retrieved".
        with contextlib.suppress(Exception):
            await _processor_task

    # Uzavření všech aktivních WebRTC peer session při shutdownu.
    for pc_id in list(_webrtc_sessions.keys()):
        await _cleanup_webrtc_session(pc_id)

    logger.info("=== vprocessor shutdown complete ===")


# ---------------------------------------------------------------------------
# FastAPI aplikace
# ---------------------------------------------------------------------------

# Hlavní FastAPI instance. Lifespan manažer výše řídí startup a shutdown.
app = FastAPI(
    title="vprocessor",
    description="Person-detection video processing service",
    version="1.0.0",
    lifespan=lifespan,
)

# --- CORS middleware ---
# Povoluje cross-origin požadavky ze všech origins, s libovolnými metodami
# a hlavičkami. V produkčním nasazení doporučujeme omezit allow_origins
# pouze na známé domény frontendové aplikace.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Povolené origins (v prod. zpřísnit)
    allow_credentials=True,  # Povolení přenosu cookies / auth hlaviček
    allow_methods=["*"],  # Povoleny všechny HTTP metody
    allow_headers=["*"],  # Povoleny všechny hlavičky
)

# ---------------------------------------------------------------------------
# Socket.IO ASGI wrapper
# ---------------------------------------------------------------------------

# Obalíme FastAPI aplikaci do socketio.ASGIApp, čímž Socket.IO server
# "přebírá" ASGI vrstvu. WebSocket / polling požadavky zpracovává Socket.IO,
# ostatní HTTP požadavky přeposílá dál do FastAPI.
# Tato proměnná (socket_app) je předána uvicornu jako kořenová ASGI aplikace.
socket_app = socketio.ASGIApp(sio, app)

# ---------------------------------------------------------------------------
# Socket.IO event handlery
# ---------------------------------------------------------------------------


@sio.event
async def connect(sid: str, environ: dict, auth=None):
    """Voláno při každém novém připojení Socket.IO klienta.

    Inkrementuje globální počítadlo připojených klientů a ihned odešle
    nově připojenému klientovi aktuální statistiky procesoru (event ``stats``),
    pokud je procesor již inicializován.

    Args:
        sid:     Unikátní identifikátor Socket.IO session přiřazený klientovi.
        environ: WSGI/ASGI environ slovník příchozího handshake požadavku.
        auth:    Volitelná autentizační data předaná klientem (není využito).
    """
    global _connected_clients

    # Zvýšení počtu připojených klientů.
    _connected_clients += 1
    logger.info("Socket.IO client connected: %s (total=%d)", sid, _connected_clients)

    if processor is not None:
        # Sestavení payloadu s aktuálními statistikami procesoru.
        status_payload = {
            "fps": processor.fps,  # Aktuální snímková frekvence
            "total_frames": processor.frame_count,  # Celkový počet zpracovaných snímků
            "total_detections": processor.total_detections,  # Celkový počet detekcí
            "uptime_seconds": round(
                time.time() - _start_time, 2
            ),  # Doba běhu v sekundách
            "status": processor.status,  # Textový stav procesoru
        }
        # Odeslání statistik pouze tomuto klientovi (cílení přes `to=sid`).
        await sio.emit("stats", status_payload, to=sid)


@sio.event
async def disconnect(sid: str):
    """Voláno při odpojení Socket.IO klienta (ať již čistém nebo náhlém).

    Dekrementuje globální počítadlo připojených klientů. Hodnota nikdy
    neklesne pod 0 díky funkci ``max()``.

    Args:
        sid: Unikátní identifikátor odpojené Socket.IO session.
    """
    global _connected_clients

    # Snížení počtu klientů; ochrana před podtečením na zápornou hodnotu.
    _connected_clients = max(0, _connected_clients - 1)
    logger.info("Socket.IO client disconnected: %s (total=%d)", sid, _connected_clients)


# ---------------------------------------------------------------------------
# HTTP endpointy
# ---------------------------------------------------------------------------


@app.get("/stream", tags=["video"])
async def mjpeg_stream():
    """MJPEG stream anotovaného (zpracovaného) videa.

    Endpoint vrací nekonečný multipart HTTP stream ve formátu
    ``multipart/x-mixed-replace``, kde každá část obsahuje jeden JPEG
    snímek s anotacemi detekcí osob.

    Stream lze přímo vložit do HTML stránky jako::

        <img src="/stream">

    Generátor se automaticky ukončí, jakmile uvicorn signalizuje vypnutí
    serveru (``_uvicorn_server.should_exit == True``), čímž uvolní HTTP
    spojení a umožní lifespan cleanup proběhnout bez nutnosti druhého
    stisku Ctrl-C.
    """

    async def generate():
        """Asynchronní generátor MJPEG snímků.

        Každou iteraci:
        1. Zkontroluje, zda má uvicorn vypnout – pokud ano, smyčka skončí
           a HTTP spojení se uzavře.
        2. Pokud procesor ještě není připraven, počká 100 ms a zkusí znovu.
        3. Načte nejnovější anotovaný snímek z procesoru.
        4. Pokud snímek existuje, sestaví MJPEG part (boundary + hlavičky
           + tělo) a odešle ho klientovi.
        5. Počká ~33 ms, aby výstupní FPS nepřekročil 30.
        """
        # Smyčka běží dokud:
        #   a) _uvicorn_server je None (spuštěno přes python -m uvicorn),
        #      tzn. referenci na server nemáme – jedeme bez podmínky,
        #   b) nebo server ještě nesignalizoval záměr ukončit se.
        # Jakmile should_exit == True, generátor vrátí StopAsyncIteration
        # a StreamingResponse uzavře spojení, čímž odblokuje uvicornův
        # shutdown mechanismus.
        last_seq = -1
        while _uvicorn_server is None or not _uvicorn_server.should_exit:
            if processor is None:
                # Procesor ještě není inicializován (startuji) – počkáme.
                await asyncio.sleep(0.1)
                continue

            # Získání posledního anotovaného snímku jako JPEG bytes.
            frame, seq = processor.get_latest_frame_with_seq()
            if frame and seq != last_seq:
                last_seq = seq
                # Sestavení jedné MJPEG "části" (part):
                #   --frame\r\n          ← MIME boundary
                #   Content-Type: ...\r\n ← typ obsahu
                #   \r\n                  ← prázdný řádek (konec hlaviček)
                #   <jpeg data>           ← tělo snímku
                #   \r\n                  ← oddělovač před dalším boundary
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")

            # Pokud nový snímek není k dispozici, krátce počkáme a zkusíme znovu.
            # Nízká prodleva drží latenci nízko bez zbytečného CPU spin-loop.
            await asyncio.sleep(0.005)

    return StreamingResponse(
        generate(),
        # MIME typ multipart/x-mixed-replace s definovaným boundary řetězcem.
        # Prohlížeče i přehrávače nahrazují předchozí snímek novým při každém
        # přijetí dalšího boundary.
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            # Zákaz jakéhokoli cachování – stream musí být vždy čerstvý.
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",  # Zpětná kompatibilita s HTTP/1.0
            "Expires": "0",  # Okamžité vypršení případné cache
            # Explicitní povolení CORS na úrovni streamu pro JS klienty.
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.post("/webrtc/offer", tags=["video"])
async def webrtc_offer(offer: WebRTCOffer):
    """Vytvoří WebRTC peer connection a vrátí SDP answer pro browser klienta."""
    if not config.ENABLE_WEBRTC:
        raise HTTPException(status_code=503, detail="WebRTC is disabled by config")

    if processor is None:
        raise HTTPException(status_code=503, detail="Processor not ready")

    pc = RTCPeerConnection()
    pc_id = str(uuid.uuid4())
    logger.info("WebRTC peer created: %s", pc_id)

    track = ProcessorVideoTrack(processor)
    pc.addTrack(track)
    meta_channel = pc.createDataChannel("frame-meta")
    meta_task = asyncio.create_task(_frame_meta_pump(pc, meta_channel, track))

    _webrtc_sessions[pc_id] = {
        "pc": pc,
        "meta_task": meta_task,
    }

    @pc.on("connectionstatechange")
    async def _on_connectionstatechange():
        logger.info("WebRTC peer %s state: %s", pc_id, pc.connectionState)
        if pc.connectionState in {"failed", "closed"}:
            await _cleanup_webrtc_session(pc_id)

    try:
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer.sdp, type=offer.type)
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await _wait_for_ice_gathering(pc)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "peer_id": pc_id,
        }
    except Exception as exc:
        logger.exception("WebRTC handshake failed for peer %s: %s", pc_id, exc)
        await _cleanup_webrtc_session(pc_id)
        raise HTTPException(status_code=500, detail="WebRTC handshake failed")


@app.get("/health", tags=["monitoring"])
async def health():
    """Liveness / readiness sonda pro orchestrátory (Docker, Kubernetes…).

    Vrací základní informace o stavu služby:
    - ``status``:            textový stav procesoru (nebo "starting")
    - ``uptime``:            doba běhu v sekundách
    - ``fps``:               aktuální snímková frekvence zpracování
    - ``connected_clients``: počet aktivních Socket.IO klientů

    Pokud procesor ještě nebyl inicializován (startuji), vrátí stav
    ``"starting"`` s nulovými hodnotami, ale HTTP kód zůstane 200 OK –
    orchestrátor tak pozná, že kontejner žije, ale ještě není plně připraven.
    """
    if processor is None:
        # Procesor ještě nebyl inicializován – aplikace se právě spouští.
        return {
            "status": "starting",
            "uptime": 0.0,
            "fps": 0.0,
            "connected_clients": _connected_clients,
        }

    # Procesor běží – vrátíme aktuální hodnoty.
    return {
        "status": processor.status,
        "uptime": round(
            time.time() - _start_time, 2
        ),  # Uptime zaokrouhlený na 2 des. místa
        "fps": processor.fps,
        "connected_clients": _connected_clients,
    }


@app.get("/api/stats", tags=["monitoring"])
async def api_stats():
    """Aktuální snapshot statistik zpracování videa.

    Vrací podrobnější statistiky než ``/health``, zejména agregované
    počty snímků a detekcí od startu aplikace:
    - ``fps``:               aktuální snímková frekvence
    - ``total_frames``:      celkový počet dosud zpracovaných snímků
    - ``total_detections``:  celkový počet detekovaných osob
    - ``uptime_seconds``:    doba běhu aplikace v sekundách
    - ``status``:            textový stav procesoru

    Pokud procesor ještě nebyl inicializován, vrátí nulové hodnoty
    se stavem ``"starting"``.
    """
    if processor is None:
        # Aplikace se spouští, data ještě nejsou k dispozici.
        return {
            "fps": 0.0,
            "total_frames": 0,
            "total_detections": 0,
            "uptime_seconds": 0.0,
            "status": "starting",
        }

    # Sestavení snapshotu aktuálního stavu procesoru.
    return {
        "fps": processor.fps,
        "total_frames": processor.frame_count,
        "total_detections": processor.total_detections,
        "uptime_seconds": round(time.time() - _start_time, 2),
        "status": processor.status,
    }


@app.get("/api/detections", tags=["detections"])
async def api_detections(limit: int = 100, offset: int = 0):
    """Čte záznamy detekcí uložené v JSONL souboru aktuální session.

    VideoRecorder průběžně zapisuje metadata každé detekce (čas, bounding
    box, spolehlivost, cesta k uloženému klipu…) do JSONL souboru, kde
    každý řádek je validní JSON objekt. Tento endpoint soubor přečte
    asynchronně a vrátí stránkovaný výsledek.

    Args:
        limit:  Maximální počet vrácených záznamů (výchozí 100).
        offset: Počet záznamů, které se přeskočí od začátku (výchozí 0).

    Returns:
        Seznam dicts se záznamy detekcí, nebo prázdný seznam pokud soubor
        neexistuje nebo ještě nebyla detekována žádná osoba.
    """
    # Lazy import – json a aiofiles nejsou potřeba při každém startu modulu,
    # pouze při skutečném volání tohoto endpointu.
    import json

    import aiofiles

    # Pokud procesor nebo rekordér ještě neexistuje, nemáme co číst.
    if processor is None or processor.recorder is None:
        return []

    # Cesta k JSONL souboru metadat pro aktuální session, jak ji zná rekordér.
    metadata_path = processor.recorder.metadata_path
    if not metadata_path:
        return []

    try:
        # Asynchronní čtení souboru – neblokujeme event loop.
        async with aiofiles.open(metadata_path, "r", encoding="utf-8") as fh:
            lines = await fh.readlines()
    except FileNotFoundError:
        # Soubor ještě neexistuje (žádná detekce dosud nezapsána) – OK.
        return []
    except Exception as exc:
        # Jiná chyba čtení (práva, poškozený fs…) – zalogujeme a vrátíme prázdno.
        logger.error("Error reading detections file: %s", exc)
        return []

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            # Přeskočíme prázdné řádky (může být na konci souboru).
            continue
        try:
            # Parsování každého řádku jako samostatného JSON objektu.
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # Poškozený řádek – tiše přeskočíme, neukončujeme zpracování.
            continue

    # Vrátíme stránkovaný výřez seznamu záznamů.
    return records[offset : offset + limit]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    # Nejprve sestavíme instanci uvicorn.Config a uvicorn.Server,
    # aby globální proměnná _uvicorn_server byla nastavena PŘED prvním
    # příchozím požadavkem. Tím generátor MJPEG streamu může okamžitě
    # číst server.should_exit a korektně se ukončit při SIGINT / SIGTERM.
    _cfg = uvicorn.Config(
        socket_app,  # Kořenová ASGI aplikace (Socket.IO wrapper)
        host=config.HOST,  # Bind adresa ze souboru konfigurace
        port=config.PORT,  # TCP port ze souboru konfigurace
        # Pojistka pro případ, že otevřená spojení (zejména MJPEG /stream)
        # se neuzavřou sama: po uplynutí tohoto timeoutu (v sekundách) od
        # prvního SIGINT uvicorn spojení násilně zavře a lifespan cleanup
        # může proběhnout. V praxi generátor /stream přečte should_exit=True
        # nejpozději za ~33 ms (1 iterace smyčky), takže tento timeout
        # slouží pouze jako bezpečnostní síť a téměř nikdy není dosažen.
        timeout_graceful_shutdown=3,
    )

    # Uložíme instanci serveru do globální proměnné, aby k ní měl přístup
    # MJPEG generátor spuštěný v kontextu jiného asyncio tasku.
    _uvicorn_server = uvicorn.Server(_cfg)

    # Spuštění serveru – blokující volání, vrátí se až po ukončení aplikace.
    _uvicorn_server.run()
