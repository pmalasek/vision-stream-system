"""
Konfigurační modul aplikace vprocessor.

Tento modul zajišťuje načtení konfigurace aplikace z prostředí (environment variables).
Hodnoty jsou čteny pomocí knihovny python-dotenv, která umožňuje definovat proměnné
v souboru .env ve vývojovém prostředí. V produkčním nasazení jsou proměnné nastaveny
přímo v prostředí kontejneru nebo operačního systému.

Jediná instance třídy Config je vytvořena na konci tohoto modulu jako `config`
a je určena k importování v ostatních částech aplikace.
"""

import os

from dotenv import load_dotenv

# Načte proměnné prostředí ze souboru .env (pokud existuje).
# Volání musí proběhnout před čtením os.getenv(), aby byly hodnoty z .env dostupné.
load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    """Vrátí bool hodnotu z proměnné prostředí.

    True hodnoty: "1", "true", "yes", "on" (case-insensitive).
    False hodnoty: "0", "false", "no", "off".
    Při neznámé hodnotě vrací default.
    """
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


class Config:
    """Třída sdružující veškerou konfiguraci aplikace.

    Každý atribut odpovídá jedné proměnné prostředí. Pokud proměnná prostředí
    není nastavena, použije se výchozí hodnota uvedená jako druhý argument
    funkce os.getenv().
    """

    # Adresa RTSP streamu, ze kterého se čtou snímky videa.
    # Výchozí hodnota předpokládá lokální RTSP server (např. MediaMTX) na portu 8554.
    RTSP_URL: str = os.getenv("RTSP_URL", "rtsp://localhost:8554/live")

    # Transportní protokol používaný pro RTSP spojení.
    # Výchozí hodnota je "tcp", protože TCP zabraňuje ztrátám paketů, které se
    # při použití UDP projevují jako artefakty v H.264 streamu
    # (varování o poškozených macrobloscích z FFmpeg/OpenCV).
    # Chcete-li přepnout zpět na UDP, nastavte v prostředí RTSP_TRANSPORT=udp.
    RTSP_TRANSPORT: str = os.getenv("RTSP_TRANSPORT", "tcp")

    # Adresář, do kterého se ukládají výstupní soubory (např. zachycené snímky,
    # výsledky detekcí apod.). Cesta může být relativní i absolutní.
    OUTPUT_DIR: str = os.getenv("OUTPUT_DIR", "./output")

    # Název nebo cesta k souboru modelu YOLO používaného pro detekci objektů.
    # Výchozí hodnota "yolov8n.pt" odkazuje na nejmenší (nano) variantu YOLOv8,
    # která nabízí nejrychlejší inferenci s přijatelnou přesností.
    YOLO_MODEL: str = os.getenv("YOLO_MODEL", "yolov8n.pt")

    # Minimální skóre spolehlivosti (confidence), které musí detekce dosáhnout,
    # aby byla považována za platnou a dále zpracována.
    # Hodnota je v rozsahu 0.0–1.0; výchozí hodnota 0.5 znamená 50 % jistotu.
    CONFIDENCE_THRESHOLD: float = float(os.getenv("CONFIDENCE_THRESHOLD", "0.5"))

    # Číslo portu, na kterém naslouchá HTTP server aplikace (např. FastAPI/Uvicorn).
    PORT: int = int(os.getenv("PORT", "8000"))

    # IP adresa síťového rozhraní, na kterém server naslouchá.
    # Výchozí hodnota "0.0.0.0" znamená naslouchání na všech dostupných rozhraních,
    # což je vhodné pro nasazení v kontejneru nebo na vzdáleném serveru.
    HOST: str = os.getenv("HOST", "0.0.0.0")

    # Délka jednoho nahrávacího segmentu v minutách. Po uplynutí této doby
    # rekordér uzavře aktuální soubory a otevře nový segment.
    SEGMENT_DURATION_MINUTES: int = int(os.getenv("SEGMENT_DURATION_MINUTES", "10"))

    # Maximální počet uchovaných segmentů (video + metadata).
    # Starší segmenty jsou automaticky mazány, jakmile jejich počet překročí
    # tuto hodnotu.
    MAX_SEGMENTS: int = int(os.getenv("MAX_SEGMENTS", "3"))

    # Škálování snímku před YOLO inferencí (0.0–1.0).
    # 0.5 = 50 % původního rozlišení; menší hodnota = rychlejší inference.
    INFERENCE_SCALE: float = float(os.getenv("INFERENCE_SCALE", "0.5"))

    # Cílová velikost vstupu pro YOLO inferenci (např. 640/512/416/320).
    # Menší hodnota obvykle zrychlí detekci za cenu nižší přesnosti.
    YOLO_IMGSZ: int = int(os.getenv("YOLO_IMGSZ", "640"))

    # Provádět inferenci pouze na každém N-tém snímku.
    # 1 = inference na každém snímku (výchozí chování), 2/3 = vyšší FPS.
    DETECT_EVERY_N: int = int(os.getenv("DETECT_EVERY_N", "1"))

    # Globální vypínač detekce (YOLO inference).
    # false = stream bez detekce, vhodné pro benchmark přenosové části pipeline.
    ENABLE_DETECTION: bool = _env_bool("ENABLE_DETECTION", True)

    # Jak často flushovat JSONL metadata na disk (po kolika snímcích).
    # 1 = flush každého snímku (nejbezpečnější, ale pomalejší).
    METADATA_FLUSH_EVERY: int = int(os.getenv("METADATA_FLUSH_EVERY", "1"))

    # Velikost FIFO fronty raw frame mezi grabberem a processing vláknem.
    # Vyšší hodnota zmenšuje trhání při krátkodobém jitteru, ale zvyšuje latenci.
    RAW_FRAME_QUEUE_SIZE: int = int(os.getenv("RAW_FRAME_QUEUE_SIZE", "8"))

    # Pokud je True, MJPEG /stream se generuje přímo z raw frame v grabber vlákně,
    # takže není blokován detekcí / zápisem na disk v processing vlákně.
    STREAM_FROM_RAW: bool = _env_bool("STREAM_FROM_RAW", True)

    # Povolit průběžné ukládání výstupů (MP4 + JSONL) na disk.
    # Pro čistý výkonový benchmark lze nastavit false.
    RECORD_OUTPUT: bool = _env_bool("RECORD_OUTPUT", True)

    # Zapnutí podrobného profilování pipeline po krocích (detect/encode/write/emit).
    # Pokud je True, v logu se periodicky vypisují průměrné časy jednotlivých kroků.
    PROFILE_PIPELINE: bool = _env_bool("PROFILE_PIPELINE", False)

    # Interval (sekundy), po kterém se vypíše souhrn profilovacích metrik.
    PROFILE_LOG_INTERVAL_SECONDS: float = float(
        os.getenv("PROFILE_LOG_INTERVAL_SECONDS", "5")
    )


# Globální instance konfigurace určená k importu napříč celou aplikací.
# Ostatní moduly by měly importovat tento objekt místo přímého volání os.getenv(),
# aby konfigurace zůstala centralizovaná a snadno testovatelná.
config = Config()
