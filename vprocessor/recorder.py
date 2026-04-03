"""
recorder.py – Modul pro záznam zpracovaných snímků a detekčních metadat.

Tento modul poskytuje třídu :class:`VideoRecorder`, která zajišťuje:
- Ukládání zpracovaných snímků do videosouboru ve formátu MP4.
- Ukládání metadat detekcí (počet osob, bounding boxy apod.) do souboru
  ve formátu JSONL (JSON Lines – jeden JSON záznam na řádek).

Výstupní soubory jsou ukládány do časově označeného podadresáře, aby se
jednotlivá nahrávací sezení nepřepisovala:

    <output_dir>/YYYY-MM-DD_HH-MM-SS/output.mp4
    <output_dir>/YYYY-MM-DD_HH-MM-SS/detections.jsonl

Typické použití::

    with VideoRecorder("/tmp/recordings") as recorder:
        for frame_id, (frame, detections, ts) in enumerate(source):
            recorder.write_frame(frame, frame_id, detections, ts)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import cv2
import numpy as np

# Modul-úrovňový logger – název loggeru odpovídá plnému názvu modulu,
# což usnadňuje filtrování logů v rozsáhlejších aplikacích.
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanty
# ---------------------------------------------------------------------------

# Počet snímků za sekundu výstupního videa.
# Hodnota 25 fps odpovídá standardnímu PAL/televiznímu standardu.
VIDEO_FPS = 25

# Čtyřznakový kód (FourCC) video kodeku použitého při zápisu.
# "mp4v" = MPEG-4 Part 2, který je široce podporovaný v kontejneru .mp4.
VIDEO_CODEC = "mp4v"

# Název výstupního videosouboru uvnitř adresáře sezení.
VIDEO_FILENAME = "output.mp4"

# Název souboru s metadaty detekcí ve formátu JSONL.
# Každý řádek obsahuje JSON objekt s údaji o jednom snímku.
METADATA_FILENAME = "detections.jsonl"


class VideoRecorder:
    """Zapisuje zpracované snímky do souboru MP4 a detekční metadata do souboru JSONL.

    Výstup je uložen v časově označeném podadresáři kořenového adresáře *output_dir*:

        <output_dir>/YYYY-MM-DD_HH-MM-SS/output.mp4
        <output_dir>/YYYY-MM-DD_HH-MM-SS/detections.jsonl

    Instance třídy :class:`cv2.VideoWriter` je vytvořena líně (lazy initialization)
    při prvním volání metody :meth:`write_frame`, aby byly v tu chvíli známy rozměry
    snímku (výška a šířka).

    Třídu lze používat jako kontextový manažer (klíčové slovo ``with``), čímž se
    zajistí automatické uvolnění prostředků i v případě výjimky.
    """

    def __init__(self, output_dir: str) -> None:
        """Připraví výstupní adresář pro aktuální nahrávací sezení.

        Vytvoří časově označený podadresář ve tvaru ``YYYY-MM-DD_HH-MM-SS``
        uvnitř zadaného kořenového adresáře. Pokud adresář neexistuje, je
        vytvořen (včetně všech nadřazených adresářů).

        Soubor s metadaty je otevřen ihned v režimu přidávání (``"a"``),
        zatímco :class:`cv2.VideoWriter` je inicializován až při prvním snímku.

        Args:
            output_dir: Kořenový adresář, ve kterém bude vytvořen podadresář
                        pro toto nahrávací sezení.
        """
        # Vygenerujeme časovou značku pro pojmenování adresáře sezení.
        # Formát je kompatibilní s názvem adresáře na všech platformách
        # (dvojtečky nahrazeny pomlčkami).
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.session_dir = os.path.join(output_dir, timestamp)

        # Vytvoříme adresář sezení; exist_ok=True zabraňuje chybě,
        # pokud by adresář z nějakého důvodu již existoval.
        os.makedirs(self.session_dir, exist_ok=True)

        # Sestavíme úplné cesty k výstupním souborům.
        self.video_path = os.path.join(self.session_dir, VIDEO_FILENAME)
        self.metadata_path = os.path.join(self.session_dir, METADATA_FILENAME)

        # VideoWriter je inicializován líně až v metodě write_frame,
        # protože teprve při prvním snímku známe jeho rozměry (výška, šířka).
        self._writer: Optional[cv2.VideoWriter] = None

        # Soubor s metadaty otevřeme hned – zapisujeme do něj průběžně
        # při každém snímku. Režim "a" (append) zajistí, že případné
        # předchozí záznamy nebudou přepsány (v praxi adresář sezení je unikátní,
        # ale jde o obranné programování).
        self._metadata_file = open(self.metadata_path, "a", encoding="utf-8")  # noqa: WPS515

        logger.info("VideoRecorder session started → dir='%s'", self.session_dir)

    # ------------------------------------------------------------------
    # Veřejné rozhraní (Public API)
    # ------------------------------------------------------------------

    def write_frame(
        self,
        frame: np.ndarray,
        frame_id: int,
        detections: list[dict],
        timestamp: float,
    ) -> None:
        """Uloží jeden snímek a k němu příslušná detekční metadata.

        Při prvním volání se provede líná inicializace :class:`cv2.VideoWriter`
        s rozměry odvozenými z dodaného snímku.

        Každý snímek je zapsán do videosouboru a zároveň je do souboru JSONL
        přidán jeden řádek s JSON objektem popisujícím detekce v tomto snímku.
        Soubor s metadaty je po každém zápisu explicitně flushován, aby data
        nebyla ztracena při případném pádu procesu.

        Args:
            frame:      Snímek ve formátu BGR jako pole NumPy (H × W × 3).
                        Tento formát přímo odpovídá výstupu OpenCV.
            frame_id:   Monotonně rostoucí číslo snímku (od 0 nebo 1).
            detections: Seznam slovníků s detekcemi vytvořených třídou
                        :class:`~detector.PersonDetector`. Každý slovník
                        typicky obsahuje klíče ``bbox``, ``confidence`` apod.
            timestamp:  Čas pořízení snímku jako Unix timestamp (počet sekund
                        od epochy), obvykle vrácený funkcí ``time.time()``.
        """
        # Pokud ještě nebyl VideoWriter inicializován, uděláme to nyní –
        # rozměry snímku jsou nyní k dispozici.
        if self._writer is None:
            self._init_writer(frame)

        # Zapíšeme snímek do videa pouze tehdy, pokud byl VideoWriter
        # úspěšně inicializován (mohl selhat, viz _init_writer).
        if self._writer is not None:
            self._writer.write(frame)

        # Sestavíme záznam metadat pro tento snímek jako Python slovník.
        record = {
            "frame_id": frame_id,  # pořadové číslo snímku
            "timestamp": timestamp,  # Unix čas pořízení snímku
            "person_count": len(detections),  # celkový počet detekovaných osob
            "detections": detections,  # seznam detailů jednotlivých detekcí
        }

        # Serializujeme záznam do JSON a zapíšeme jako jeden řádek (JSONL formát).
        # Flush zajistí okamžité zapsání na disk bez čekání na vyprázdnění bufferu.
        self._metadata_file.write(json.dumps(record) + "\n")
        self._metadata_file.flush()

    def release(self) -> None:
        """Uvolní VideoWriter a zavře soubor s metadaty.

        Tuto metodu je bezpečné volat opakovaně – druhé a další volání
        nemá žádný efekt. Je automaticky volána metodou :meth:`__exit__`
        při použití jako kontextový manažer.
        """
        # Uvolníme VideoWriter, pokud byl vytvořen, a vynulujeme referenci,
        # aby bylo možné metodu bezpečně volat vícekrát.
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            logger.info("VideoWriter released → '%s'", self.video_path)

        # Zavřeme soubor s metadaty, pouze pokud je stále otevřený.
        if self._metadata_file and not self._metadata_file.closed:
            self._metadata_file.close()
            logger.info("Metadata file closed → '%s'", self.metadata_path)

    # ------------------------------------------------------------------
    # Vlastnosti (Properties)
    # ------------------------------------------------------------------

    @property
    def session_output_dir(self) -> str:
        """Absolutní cesta k adresáři aktuálního nahrávacího sezení.

        Returns:
            Řetězec s cestou ve tvaru ``<output_dir>/YYYY-MM-DD_HH-MM-SS``.
        """
        return self.session_dir

    # ------------------------------------------------------------------
    # Privátní pomocné metody (Private helpers)
    # ------------------------------------------------------------------

    def _init_writer(self, frame: np.ndarray) -> None:
        """Vytvoří instanci :class:`cv2.VideoWriter` s rozměry odvozenými ze snímku *frame*.

        Metoda je volána líně při prvním snímku, aby byly rozměry výstupu
        automaticky přizpůsobeny skutečné velikosti zpracovávaného videa.
        Pokud se VideoWriter nepodaří otevřít (např. neplatná cesta nebo
        nepodporovaný kodek), nastaví ``self._writer`` zpět na ``None``
        a zaloguje chybu. Snímky pak nebudou ukládány do videa, ale záznam
        metadat bude nadále pokračovat.

        Args:
            frame: Referenční snímek, ze kterého jsou odečteny rozměry
                   výstupního videa (výška a šířka).
        """
        # Odečteme výšku a šířku ze tvaru pole NumPy; třetí dimenze (kanály)
        # nás zde nezajímá, proto použijeme řez [:2].
        height, width = frame.shape[:2]

        # Převedeme řetězcový kód kodeku na čtyřbajtový integer (FourCC),
        # který OpenCV používá k identifikaci kodeku při zápisu videa.
        fourcc = cv2.VideoWriter_fourcc(*VIDEO_CODEC)

        # Vytvoříme VideoWriter; rozlišení musí být zadáno jako (šířka, výška) –
        # pozor na opačné pořadí oproti NumPy konvenci (řádky × sloupce)!
        self._writer = cv2.VideoWriter(
            self.video_path, fourcc, VIDEO_FPS, (width, height)
        )

        # Ověříme, zda byl VideoWriter úspěšně inicializován.
        # isOpened() vrátí False například při neplatné cestě nebo chybějícím kodeku.
        if not self._writer.isOpened():
            logger.error(
                "Failed to open VideoWriter for '%s' – frames will not be saved.",
                self.video_path,
            )
            # Vynulujeme writer, aby metoda write_frame věděla, že zápis videa selhal.
            self._writer = None
            return

        logger.info(
            "VideoWriter initialised → '%s' (%dx%d @ %d fps)",
            self.video_path,
            width,
            height,
            VIDEO_FPS,
        )

    def __enter__(self) -> "VideoRecorder":
        """Podpora pro použití jako kontextový manažer (``with`` blok).

        Returns:
            Vrací sám sebe, aby bylo možné instanci přiřadit pomocí ``as``.
        """
        return self

    def __exit__(self, *_) -> None:
        """Automaticky uvolní prostředky při opuštění ``with`` bloku.

        Volá metodu :meth:`release`, čímž zajistí korektní uzavření
        VideoWriteru a souboru s metadaty bez ohledu na to, zda byl
        blok ukončen normálně nebo výjimkou.
        """
        # Předáme případné informace o výjimce metodě release ignorováním (*_),
        # výjimku sami nepotlačujeme – vrátíme None (falsy), takže se dál šíří.
        self.release()
