"""
Modul processor.py – jádro zpracovatelské pipeline pro video stream.

Architektura dvou vláken (Grabber + Processing thread)
======================================================

Pipeline je záměrně rozdělena do dvou samostatných pracovních vláken, aby
latence YOLO inference (~100 ms) nemohla vyhladovět vyčítání síťového bufferu
RTSP streamu (~33 ms/snímek):

1. **Frame grabber** (``_grabber_executor``)
   - Spouští metodu :meth:`VideoProcessor._sync_frame_grabber`.
   - Vlastní veškerou logiku RTSP připojení a opětovného připojování.
   - V těsné smyčce volá ``cap.read()``, čímž průběžně vyprazdňuje dekodérský
     buffer FFmpeg.
   - Každý úspěšně dekódovaný snímek uloží do ``_latest_raw_frame`` a nastaví
     ``_raw_frame_event``, čímž probudí zpracovatelské vlákno.

2. **Processing thread** (``_executor``)
   - Spouští metodu :meth:`VideoProcessor._sync_processing_loop`.
   - Čeká na ``_raw_frame_event``, převezme poslední surový snímek,
     spustí YOLO inferenci, zakóduje výsledek do JPEG, zapíše na disk
     a odešle události přes Socket.IO.

Obě vlákna běží uvnitř instancí ``ThreadPoolExecutor``, takže nikdy
neblokují asyncio event loop. Hlavní event loop je zachycen v metodě
:meth:`VideoProcessor.start` a uložen jako ``_main_loop``, aby bylo možné
z pracovních vláken plánovat Socket.IO coroutiny pomocí
``asyncio.run_coroutine_threadsafe``.

Kontrakt ukončení (Shutdown contract)
--------------------------------------
Viz docstring metody :meth:`VideoProcessor.stop`.
"""

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

# Standardní Python logger pojmenovaný podle modulu (hierarchické logování).
logger = logging.getLogger(__name__)

# Prodleva v sekundách mezi jednotlivými pokusy o znovupřipojení k RTSP streamu.
RTSP_RETRY_DELAY = 2.0  # sekundy mezi pokusy o reconnect

# Maximální doba v milisekundách, po kterou smí jedno volání cap.read() blokovat.
# Slouží jako pojistka pro případ, že externí uvolnění z metody stop() nestihne
# přerušit volání cap.read() dostatečně rychle.
RTSP_READ_TIMEOUT_MS = 2_000  # ms – max. čas blokování jednoho cap.read()

# Kvalita JPEG komprese při kódování snímků (rozsah 0–100, vyšší = lepší kvalita).
# Hodnota 90 poskytuje vyšší vizuální kvalitu, která je klíčová pro viditelnosti
# pomalých pohybů a zmenšení zápasů (artifacts) při nižších frame ratech.
JPEG_QUALITY = 90  # kvalita JPEG kódování (0–100)

# Potlačení vlastních C-úrovňových WARNING zpráv OpenCV (např. "backend is generally
# available but can't be used to capture by name"), které obcházejí Python logging.
# Některé buildy opencv-python-headless ale API setLogLevel neposkytují, proto je
# konfigurace podmíněná a s fallbackem na cv2.utils.logging.
# Úrovně: 0=SILENT  1=FATAL  2=ERROR  3=WARNING  4=INFO  5=DEBUG
try:
    if hasattr(cv2, "setLogLevel"):
        cv2.setLogLevel(2)
    elif hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except Exception:
    # Log-level nastavení je best-effort; nesmí shodit import celého modulu.
    pass


@contextlib.contextmanager
def _suppress_c_stderr():
    """Kontextový manažer: přesměruje souborový deskriptor 2 do /dev/null.

    Proč je to potřeba
    ------------------
    FFmpeg (a další C rozšíření) zapisují chybová hlášení o připojení přímo
    na fd 2 (stderr na úrovni OS), zcela obcházejíc ``sys.stderr`` i modul
    ``logging``. Jediný spolehlivý způsob, jak je umlčet, je dočasně
    nasměrovat fd 2 na ``/dev/null`` přímo na úrovni OS pomocí ``os.dup2``.

    Jak to funguje
    --------------
    - Původní fd 2 se uloží pomocí ``os.dup`` pod novým číslem deskriptoru
      (``saved_fd``).
    - ``os.dup2`` přesměruje fd 2 na ``/dev/null``.
    - Po skončení bloku (nebo při výjimce) ``finally`` část vždy obnoví
      původní fd 2 a zavře pomocné deskriptory, takže kontext je bezpečný
      i při výjimce.

    Příklad použití::

        with _suppress_c_stderr():
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    """
    # Záloha původního stderr deskriptoru pod novým číslem.
    saved_fd = os.dup(2)
    # Otevření /dev/null pro zápis – sem budou přesměrována C-level chybová hlášení.
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        # Přesměrování fd 2 → /dev/null (C-level výstupy se "pohltí").
        os.dup2(devnull, 2)
        yield
    finally:
        # Bezpodmínečné obnovení původního stderr, aby aplikace fungovala dál.
        os.dup2(saved_fd, 2)
        # Uvolnění pomocných deskriptorů, aby nedošlo k úniku (fd leak).
        os.close(saved_fd)
        os.close(devnull)


class VideoProcessor:
    """Řídí pipeline: zachycení → detekce → anotace → záznam → vysílání.

    Dvou-vláknová architektura
    --------------------------
    Pipeline je rozdělena do dvou dedikovaných pracovních vláken, aby latence
    YOLO inference (~100 ms) nemohla vyhladovět vyčítání RTSP síťového bufferu
    (~33 ms/snímek):

    * **Frame grabber** (``_grabber_executor``) — spouští
      :meth:`_sync_frame_grabber`, který vlastní veškerou logiku RTSP
      připojení/odpojení a volá ``cap.read()`` v těsné smyčce.
      Každý úspěšně dekódovaný snímek zapíše do ``_latest_raw_frame``
      a nastaví ``_raw_frame_event``, aby jej zpracovatelské vlákno mohlo
      vyzvednout.

    * **Processing thread** (``_executor``) — spouští
      :meth:`_sync_processing_loop`, který čeká na ``_raw_frame_event``,
      vezme poslední surový snímek, spustí YOLO inferenci, zakóduje výsledek
      do JPEG, zapíše na disk a odešle Socket.IO události.

    Obě vlákna běží uvnitř ``ThreadPoolExecutor`` instancí, takže nikdy
    neblokují asyncio event loop. Hlavní event loop je zachycen v metodě
    :meth:`start` a uložen jako ``_main_loop``, aby bylo možné z pracovních
    vláken plánovat Socket.IO coroutiny pomocí
    ``asyncio.run_coroutine_threadsafe``.

    Kontrakt ukončení
    -----------------
    Viz docstring metody :meth:`stop`.

    ``latest_frame`` je chráněno ``threading.Lock``, protože je zapisováno
    z pracovního vlákna a čteno z asynchronních HTTP handlerů běžících
    na hlavním event loopu.
    """

    def __init__(
        self,
        config: Config,
        detector: PersonDetector,
        recorder: VideoRecorder,
        sio,  # socketio.AsyncServer
    ) -> None:
        # ── Závislosti injektované zvenčí ──────────────────────────────────
        self.config = config  # konfigurace aplikace (RTSP URL, transport, …)
        self.detector = detector  # detektor osob (obaluje YOLO model)
        self.recorder = recorder  # zapisovač videa na disk
        self.sio = sio  # Socket.IO AsyncServer pro real-time události

        # ── Stavové příznaky ────────────────────────────────────────────────
        # Příznak běhu – nastavení na False způsobí ukončení obou pracovních vláken.
        self.running: bool = False
        # Celkový počet zpracovaných snímků od spuštění.
        self.frame_count: int = 0
        # Celkový počet detekcí osob od spuštění.
        self.total_detections: int = 0

        # ── Sdílený JPEG výstup ────────────────────────────────────────────
        # Zámek chrání latest_frame před souběžným čtením/zápisem z různých vláken.
        self._frame_lock = threading.Lock()
        # Poslední JPEG-zakódovaný snímek (bytes) nebo None, dokud žádný nepřišel.
        self.latest_frame: bytes | None = None

        # ── Pomocné stavové proměnné ───────────────────────────────────────
        # Poslední seznam detekcí (sdíleno s HTTP endpointy).
        self.latest_detections: list[dict] = []
        # Aktuálně měřená snímková frekvence (přepočítávána každou sekundu).
        self.fps: float = 0.0
        # Textový stav pipeline ("idle", "starting", "streaming", "reconnecting", …).
        self.status: str = "idle"

        # ── Interní časovače ───────────────────────────────────────────────
        # Čas spuštění pipeline – používá se pro výpočet doby běhu (uptime).
        self._start_time: float = 0.0
        # Reference na hlavní asyncio event loop; zachytí se v metodě start().
        self._main_loop: asyncio.AbstractEventLoop | None = None
        # ThreadPoolExecutor pro zpracovatelské vlákno (YOLO inference, kódování, …).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vproc")

        # ── Koordinace vypnutí (Shutdown coordination) ────────────────────
        # _stop_event nahrazuje prosté time.sleep() ve smyčkách, takže metoda
        # stop() může vlákna probudit okamžitě namísto čekání až
        # RTSP_RETRY_DELAY sekund na expiraci spánku.
        self._stop_event = threading.Event()

        # _cap uchovává aktuální VideoCapture; _cap_lock chrání přístup k němu,
        # aby metoda stop() mohla deskriptor uvolnit z jiného vlákna, čímž
        # přeruší blokující volání cap.read() a zastaví FFmpeg dekódování.
        self._cap: cv2.VideoCapture | None = None
        self._cap_lock = threading.Lock()

        # ── Frame grabber – dedikované vlákno pro čtení RTSP bufferu ────────
        # Samostatný ThreadPoolExecutor zabraňuje tomu, aby YOLO inference
        # blokovala čtení ze sítě.
        self._grabber_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="grabber"
        )

        # Poslední surový snímek sdílený mezi grabberem a zpracovatelským vláknem.
        # Chráněn _raw_frame_lock; vždy se přepíše nejnovějším snímkem (starý
        # se zahodí, pokud jej zpracovatelské vlákno ještě nestihlo vyzvednout).
        self._latest_raw_frame: np.ndarray | None = None
        self._raw_frame_lock = threading.Lock()

        # Nastavuje grabber po každém novém uloženém snímku; maže zpracovatelské
        # vlákno po jeho vyzvednutí. Nastavuje i metoda stop(), aby okamžitě
        # probudila zpracovatelské vlákno blokující v _raw_frame_event.wait().
        self._raw_frame_event = threading.Event()

    # ──────────────────────────────────────────────────────────────────────────
    # Veřejné asynchronní API
    # ──────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Zachytí běžící event loop a spustí obě pracovní vlákna pipeline.

        Metoda nejprve inicializuje stavové proměnné, poté zachytí aktuální
        asyncio event loop (nutné pro pozdější ``asyncio.run_coroutine_threadsafe``
        volání z pracovních vláken) a nakonec spustí grabber i zpracovatelské
        vlákno přes ``run_in_executor``. Čeká na dokončení obou vláken pomocí
        ``asyncio.gather`` a loguje případné výjimky.
        """
        # Zabránění dvojitému spuštění – pipeline již běží.
        if self.running:
            logger.warning("VideoProcessor.start() voláno, ale pipeline již běží.")
            return

        # Nastavení příznaku běhu a vynulování synchronizačních událostí.
        self.running = True
        self._stop_event.clear()
        self._raw_frame_event.clear()
        self._start_time = time.time()
        self.status = "starting"

        # Zachycení hlavního event loopu PŘED vstupem do executoru, aby
        # pracovní vlákna mohla plánovat coroutiny zpět na něj.
        self._main_loop = asyncio.get_running_loop()

        logger.info("Spouštění VideoProcessor …")

        # Spuštění grabberu v jeho dedikovaném thread poolu.
        grabber = self._main_loop.run_in_executor(
            self._grabber_executor, self._sync_frame_grabber
        )
        # Spuštění zpracovatelské smyčky v jejím dedikovaném thread poolu.
        processor = self._main_loop.run_in_executor(
            self._executor, self._sync_processing_loop
        )

        # Čekání na obě vlákna. return_exceptions=True zajistí, že pokud jedno
        # vlákno vyhodí výjimku, druhé je přesto vyčkáno před návratem start().
        results = await asyncio.gather(grabber, processor, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.error("Vlákno skončilo s výjimkou: %s", r)

    async def stop(self) -> None:
        """Pošle signál k zastavení obou pracovních vláken pipeline.

        Metoda pouze *signalizuje* ukončení – **nečeká** na faktické
        doběhnutí pracovních vláken. Za čekání zodpovídá volající (lifespan
        v ``main.py``) sledováním ``_processor_task``.

        Kroky ukončení
        --------------
        1. ``self.running = False`` — podmínka smyčky v obou vláknech.
        2. ``_stop_event.set()`` — probudí každé volání ``_stop_event.wait()``
           (náhrada za ``time.sleep``), aby se okamžitě vrátilo.
        3. ``_raw_frame_event.set()`` — probudí zpracovatelské vlákno čekající
           v ``_raw_frame_event.wait()``, aby nečekalo celých 0,5 s na timeout.
        4. ``_cap.release()`` — zavře RTSP socket, čímž způsobí, že blokující
           ``cap.read()`` se okamžitě vrátí s ``ret=False`` namísto čekání na
           další snímek. Chráněno ``_cap_lock`` kvůli ochraně před race
           condition s reconnect sekcí grabberu.
        5. Oba executory jsou uzavřeny (neblokujícím způsobem).
        """
        logger.info("Zastavování VideoProcessor …")
        # Nastavení příznaku – obě smyčky při příštím průchodu zjistí, že mají skončit.
        self.running = False
        self.status = "stopped"

        # Probuzení každého vlákna, které spí v _stop_event.wait().
        self._stop_event.set()

        # Probuzení zpracovatelského vlákna čekajícího na nový snímek,
        # aby nečekalo celých 0,5 s na timeout události.
        self._raw_frame_event.set()

        # Uvolnění VideoCapture, aby se blokující cap.read() okamžitě vrátil
        # s ret=False. Zámek zabraňuje race condition s grabberem, který může
        # zrovna provádět reconnect a měnit self._cap.
        with self._cap_lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception as exc:  # pragma: no cover
                    logger.debug("Ignoruji chybu při uvolňování cap ve stop(): %s", exc)
                self._cap = None

        # Neblokující uzavření obou thread poolů. Pracovní vlákna se ukončí
        # sama po detekci running=False a nastavených událostí; pooly budou
        # uvolněny po jejich dokončení.
        self._executor.shutdown(wait=False)
        self._grabber_executor.shutdown(wait=False)
        logger.info("Signál stop odeslán do VideoProcessor.")

    def get_latest_frame(self) -> bytes | None:
        """Vrátí nejnovější JPEG-zakódovaný snímek (thread-safe).

        Přístup k ``latest_frame`` je chráněn ``_frame_lock``, protože
        snímek je zapisován z pracovního vlákna a čten z asyncio HTTP
        handlerů na hlavním event loopu.

        Returns
        -------
        bytes | None
            JPEG bajty posledního zpracovaného snímku, nebo ``None``,
            pokud ještě žádný snímek nebyl zpracován.
        """
        with self._frame_lock:
            return self.latest_frame

    # ──────────────────────────────────────────────────────────────────────────
    # Interní metody – spouštěné v pracovních vláknech ThreadPoolExecutoru
    # ──────────────────────────────────────────────────────────────────────────

    def _sync_frame_grabber(self) -> None:
        """Průběžně čte surové snímky z RTSP streamu.

        Běží v dedikovaném vlákně, takže ``cap.read()`` není nikdy blokováno
        YOLO inferencí. Každý získaný snímek přepíše ``_latest_raw_frame``
        a nastaví ``_raw_frame_event``, aby jej zpracovatelské vlákno mohlo
        vyzvednout.

        Logika připojení
        ----------------
        - Pokud ``cap`` není otevřen, metoda se pokusí o (znovu)připojení.
        - Před otevřením streamu nastaví proměnnou prostředí
          ``OPENCV_FFMPEG_CAPTURE_OPTIONS`` na zvolený RTSP transport (TCP/UDP).
        - Volání ``cv2.VideoCapture`` je obaleno ``_suppress_c_stderr``, aby
          FFmpeg chybová hlášení neznečišťovala výstup uvicornu.
        - Po úspěšném otevření jsou nastaveny timeouty čtení.
        - Při neúspěchu metoda čeká ``RTSP_RETRY_DELAY`` sekund pomocí
          ``_stop_event.wait()``, což umožňuje okamžité přerušení ze stop().

        Sdílení snímků
        --------------
        Starý snímek je jednoduše zahozen, pokud jej zpracovatelské vlákno
        ještě nestihlo vyzvednout – grabber vždy ukládá *nejnovější* snímek.
        """
        # Lokální proměnná pro aktuální VideoCapture objekt.
        cap: cv2.VideoCapture | None = None

        try:
            while self.running:
                # ── Připojení / znovupřipojení ─────────────────────────────
                if cap is None or not cap.isOpened():
                    # Pokud existuje starý cap (byl otevřen, ale přestal fungovat),
                    # odstraníme jej ze sdílené proměnné a uvolníme jeho zdroje.
                    if cap is not None:
                        with self._cap_lock:
                            self._cap = None
                        cap.release()
                        cap = None

                    logger.info(
                        "Připojování k RTSP streamu: %s …", self.config.RTSP_URL
                    )
                    self.status = "connecting"

                    # Nastavení FFmpeg transportního protokolu (TCP/UDP) přes
                    # proměnnou prostředí, aby UDP ztráty paketů nezpůsobovaly
                    # H.264 "corrupted macroblock" varování dekodéru.
                    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                        f"rtsp_transport;{self.config.RTSP_TRANSPORT}"
                    )

                    # Obalení _suppress_c_stderr potlačí nízkoúrovňová FFmpeg
                    # hlášení jako "[tcp @ ...] Connection refused", která se
                    # jinak zapisují přímo na fd 2 a znečišťují log uvicornu.
                    with _suppress_c_stderr():
                        new_cap = cv2.VideoCapture(self.config.RTSP_URL, cv2.CAP_FFMPEG)

                    # Pokud se stream nepodařilo otevřít, čekáme a zkusíme znovu.
                    if not new_cap.isOpened():
                        logger.warning(
                            "Nelze otevřít RTSP stream '%s'. Opakuji za %.1f s …",
                            self.config.RTSP_URL,
                            RTSP_RETRY_DELAY,
                        )
                        new_cap.release()
                        # Přerušitelný spánek – stop() nastaví událost, takže
                        # wait() vrátí True okamžitě namísto čekání 2 s.
                        if self._stop_event.wait(RTSP_RETRY_DELAY):
                            break
                        continue

                    # Nastavení maximální doby blokování pro cap.read() –
                    # pojistka pro případ, že externí release ze stop() nestihne
                    # přerušit volání včas.
                    new_cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, RTSP_READ_TIMEOUT_MS)
                    try:
                        # CAP_PROP_READ_TIMEOUT_MSEC je dostupné až od OpenCV 4.6;
                        # na starších sestaveních toto nastavení tiše přeskočíme,
                        # aby nezhroutilo celou pipeline.
                        new_cap.set(
                            cv2.CAP_PROP_READ_TIMEOUT_MSEC, RTSP_READ_TIMEOUT_MS
                        )
                    except Exception:
                        pass

                    # Zveřejnění nového cap do sdílené proměnné, aby jej
                    # metoda stop() mohla externě uvolnit z jiného vlákna.
                    cap = new_cap
                    with self._cap_lock:
                        self._cap = cap

                    logger.info("RTSP stream úspěšně otevřen.")
                    self.status = "streaming"

                # Bezpečnostní kontrola: před zahájením nového čtení ověříme,
                # zda stop() nebyl zavolán v průběhu reconnect sekce.
                if self._stop_event.is_set():
                    break

                # ── Čtení snímku ───────────────────────────────────────────
                # Toto volání může blokovat až RTSP_READ_TIMEOUT_MS milisekund.
                ret, frame = cap.read()

                # Kontrola příznaku running ihned po návratu z blokujícího read().
                if not self.running:
                    break

                # Pokud čtení selhalo (stream přerušen, síťová chyba apod.),
                # uvolníme cap a přejdeme do stavu reconnecting.
                if not ret or frame is None:
                    logger.warning(
                        "Nepodařilo se přečíst snímek – stream mohl být přerušen. "
                        "Opakuji za %.1f s …",
                        RTSP_RETRY_DELAY,
                    )
                    with self._cap_lock:
                        self._cap = None
                    cap.release()
                    cap = None
                    self.status = "reconnecting"
                    # Přerušitelný spánek – okamžitě se vrátí, pokud stop() zavolá set().
                    if self._stop_event.wait(RTSP_RETRY_DELAY):
                        break
                    continue

                # ── Sdílení snímku se zpracovatelským vláknem ──────────────
                # Vždy přepíšeme _latest_raw_frame nejnovějším snímkem.
                # Starý snímek se jednoduše zahodí, pokud jej procesor nestačil
                # vyzvednout – vždy chceme zpracovávat co nejčerstvější obraz.
                with self._raw_frame_lock:
                    self._latest_raw_frame = frame
                # Probuzení zpracovatelského vlákna, aby vědělo o novém snímku.
                self._raw_frame_event.set()

        except Exception as exc:
            # Neočekávaná výjimka – zalogujeme celý traceback a přejdeme do stavu error.
            logger.exception("Neočekávaná chyba v grabberu snímků: %s", exc)
            self.status = "error"

        finally:
            # Bezpodmínečné uvolnění VideoCapture při jakémkoliv ukončení smyčky.
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
                with self._cap_lock:
                    self._cap = None
                logger.info("VideoCapture uvolněn (grabber).")

            # Probuzení zpracovatelského vlákna, aby mohlo čistě skončit,
            # i když právě čeká uvnitř _raw_frame_event.wait().
            self._raw_frame_event.set()
            logger.info("Frame grabber ukončen (status=%s).", self.status)

    def _sync_processing_loop(self) -> None:
        """Spouští YOLO inferenci na nejnovějším surovém snímku z grabberu.

        Záměrně odděleno od čtení RTSP, aby latence inference nikdy
        nevyhladověla vyčítání síťového bufferu v grabber vlákně.

        Průběh jedné iterace smyčky
        ---------------------------
        1. Čeká na ``_raw_frame_event`` (max. 0,5 s timeout pro periodické
           ověření ``self.running``).
        2. Vezme nejnovější surový snímek z ``_latest_raw_frame``.
        3. Spustí detekci osob přes ``detector.detect()``.
        4. Zakóduje anotovaný snímek do JPEG a uloží do ``latest_frame``.
        5. Předá anotovaný snímek rekordéru pro zápis na disk.
        6. Odešle Socket.IO událost ``detection`` s výsledky detekce.
        7. Aktualizuje FPS čítač a jednou za sekundu odešle Socket.IO
           událost ``stats``.
        """
        # Lokální čítač snímků pro výpočet FPS (nezávislý na self.frame_count).
        fps_frame_count: int = 0
        # Čas posledního resetu FPS čítače.
        fps_timer: float = time.time()
        # Čas posledního odeslání statistik přes Socket.IO.
        last_stats_time: float = time.time()

        # ── Profilování pipeline (volitelné, řízené konfigurací) ─────────
        profile_enabled: bool = self.config.PROFILE_PIPELINE
        profile_interval: float = max(1.0, self.config.PROFILE_LOG_INTERVAL_SECONDS)
        detect_every_n: int = max(1, self.config.DETECT_EVERY_N)
        profile_window_start: float = time.time()
        profile_frame_count: int = 0
        detect_sum_ms: float = 0.0
        encode_sum_ms: float = 0.0
        write_sum_ms: float = 0.0
        emit_sum_ms: float = 0.0
        total_sum_ms: float = 0.0

        if profile_enabled:
            logger.info(
                "Pipeline profiling ENABLED (interval=%.1f s, detect_every_n=%d)",
                profile_interval,
                detect_every_n,
            )

        try:
            while self.running:
                # ── Čekání na nový surový snímek ───────────────────────────
                # Timeout 0,5 s umožňuje periodické ověřování self.running,
                # i když žádné snímky nepřicházejí (např. stream ještě není připojen).
                if not self._raw_frame_event.wait(timeout=0.5):
                    # Timeout vypršel bez nového snímku – opakujeme čekání.
                    continue
                # Vynulování události, abychom čekali na skutečně nový snímek.
                self._raw_frame_event.clear()

                # Vyzvednutí nejnovějšího snímku pod zámkem.
                with self._raw_frame_lock:
                    frame = self._latest_raw_frame

                # Pokud není snímek k dispozici nebo pipeline končí, přeskočíme.
                if frame is None or not self.running:
                    continue

                frame_start_perf = time.perf_counter()

                # Inkrementace globálního čítače zpracovaných snímků.
                self.frame_count += 1
                fps_frame_count += 1
                timestamp = time.time()

                # ── Detekce osob (volitelně pouze každý N-tý snímek) ───────
                # detector.detect() se volá jen na každém N-tém snímku.
                # U mezilehlých snímků posíláme do UI čistý obraz bez boxů.
                detect_start_perf = time.perf_counter()
                do_detect = ((self.frame_count - 1) % detect_every_n) == 0
                if do_detect:
                    annotated_frame, detections = self.detector.detect(frame)
                    # Uložení detekcí pro HTTP endpoint /api/detections.
                    self.latest_detections = detections
                    # Průběžné sčítání celkového počtu detekcí od spuštění.
                    self.total_detections += len(detections)
                else:
                    detections = []
                    annotated_frame = frame
                detect_end_perf = time.perf_counter()

                # ── Kódování do JPEG a uložení ─────────────────────────────
                # Anotovaný snímek zakódujeme do JPEG s nastavenou kvalitou.
                encode_start_perf = time.perf_counter()
                ok, buffer = cv2.imencode(
                    ".jpg", annotated_frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                )
                if ok:
                    jpeg_bytes = buffer.tobytes()
                    # Zápis pod zámkem – latest_frame čtou HTTP handlery z jiného vlákna.
                    with self._frame_lock:
                        self.latest_frame = jpeg_bytes
                encode_end_perf = time.perf_counter()

                # ── Zápis na disk ──────────────────────────────────────────
                # Rekordér rozhodne, zda snímek zapsat (závisí na konfiguraci záznamu).
                write_start_perf = time.perf_counter()
                self.recorder.write_frame(
                    frame=annotated_frame,
                    frame_id=self.frame_count,
                    detections=detections,
                    timestamp=timestamp,
                )
                write_end_perf = time.perf_counter()

                # ── Odeslání Socket.IO události "detection" ────────────────
                # Event emitujeme pouze tehdy, když proběhla inference.
                emit_start_perf = time.perf_counter()
                if do_detect:
                    detection_payload = {
                        "frame_id": self.frame_count,
                        "timestamp": timestamp,
                        "person_count": len(detections),
                        "detections": detections,
                    }
                    self._emit(self.sio.emit("detection", detection_payload))
                emit_end_perf = time.perf_counter()

                if profile_enabled:
                    detect_sum_ms += (detect_end_perf - detect_start_perf) * 1000.0
                    encode_sum_ms += (encode_end_perf - encode_start_perf) * 1000.0
                    write_sum_ms += (write_end_perf - write_start_perf) * 1000.0
                    emit_sum_ms += (emit_end_perf - emit_start_perf) * 1000.0
                    total_sum_ms += (time.perf_counter() - frame_start_perf) * 1000.0
                    profile_frame_count += 1

                # ── Aktualizace FPS čítače ─────────────────────────────────
                now = time.time()
                elapsed_fps = now - fps_timer
                if elapsed_fps >= 1.0:
                    # Přepočet FPS jako počet snímků za uplynulou sekundu.
                    self.fps = round(fps_frame_count / elapsed_fps, 2)
                    # Reset lokálního čítače a timeru pro další periodu.
                    fps_frame_count = 0
                    fps_timer = now

                # Jednou za sekundu odeslat statistiky přes Socket.IO.
                if now - last_stats_time >= 1.0:
                    self._emit(self.sio.emit("stats", self._build_stats_payload()))
                    last_stats_time = now

                if profile_enabled and (now - profile_window_start) >= profile_interval:
                    if profile_frame_count > 0:
                        avg_detect_ms = detect_sum_ms / profile_frame_count
                        avg_encode_ms = encode_sum_ms / profile_frame_count
                        avg_write_ms = write_sum_ms / profile_frame_count
                        avg_emit_ms = emit_sum_ms / profile_frame_count
                        avg_total_ms = total_sum_ms / profile_frame_count
                        avg_other_ms = max(
                            0.0,
                            avg_total_ms
                            - (
                                avg_detect_ms
                                + avg_encode_ms
                                + avg_write_ms
                                + avg_emit_ms
                            ),
                        )

                        logger.info(
                            "Pipeline profile (%d frames / %.1f s): "
                            "avg_total=%.2f ms | detect=%.2f ms | encode=%.2f ms | "
                            "write=%.2f ms | emit=%.3f ms | other=%.2f ms | fps=%.2f",
                            profile_frame_count,
                            now - profile_window_start,
                            avg_total_ms,
                            avg_detect_ms,
                            avg_encode_ms,
                            avg_write_ms,
                            avg_emit_ms,
                            avg_other_ms,
                            self.fps,
                        )

                    profile_window_start = now
                    profile_frame_count = 0
                    detect_sum_ms = 0.0
                    encode_sum_ms = 0.0
                    write_sum_ms = 0.0
                    emit_sum_ms = 0.0
                    total_sum_ms = 0.0

        except Exception as exc:
            # Neočekávaná výjimka v inferenční smyčce – zalogujeme a přejdeme do stavu error.
            logger.exception("Neočekávaná chyba ve zpracovatelské smyčce: %s", exc)
            self.status = "error"

        finally:
            # Bezpodmínečné uvolnění rekordéru při jakémkoliv ukončení smyčky.
            try:
                self.recorder.release()
            except Exception as exc:
                logger.error("Chyba při uvolňování rekordéru: %s", exc)

            logger.info("Zpracovatelská smyčka ukončena.")

    # ──────────────────────────────────────────────────────────────────────────
    # Pomocné metody
    # ──────────────────────────────────────────────────────────────────────────

    def _emit(self, coro) -> None:
        """Naplánuje Socket.IO coroutinu na hlavní event loop.

        Bezpečné pro volání z libovolného vlákna. Pokud hlavní event loop
        ještě není dostupný (např. voláno před :meth:`start`) nebo je již
        uzavřen, coroutina se tiše zahodí, aby nedocházelo k varování
        "coroutine was never awaited".

        Parameters
        ----------
        coro:
            Coroutina vrácená metodou ``sio.emit(...)``; musí být ihned
            naplánována nebo explicitně zavřena.
        """
        if self._main_loop is None or self._main_loop.is_closed():
            # Event loop není k dispozici – coroutinu explicitně zavřeme,
            # abychom předešli Pythonovu varování "coroutine was never awaited".
            coro.close()
            return
        # Bezpečné naplánování coroutiny z libovolného vlákna na hlavní event loop.
        asyncio.run_coroutine_threadsafe(coro, self._main_loop)

    def _build_stats_payload(self) -> dict:
        """Sestaví slovník statistik pro Socket.IO událost ``stats``.

        Returns
        -------
        dict
            Slovník s klíči: ``fps``, ``total_frames``, ``total_detections``,
            ``uptime_seconds``, ``status``.
        """
        # Výpočet doby běhu od spuštění pipeline; 0.0 pokud ještě neběžela.
        uptime = time.time() - self._start_time if self._start_time else 0.0
        return {
            "fps": self.fps,  # aktuální snímková frekvence
            "total_frames": self.frame_count,  # celkový počet zpracovaných snímků
            "total_detections": self.total_detections,  # celkový počet detekcí osob
            "uptime_seconds": round(uptime, 2),  # doba běhu v sekundách
            "status": self.status,  # textový stav pipeline
        }
