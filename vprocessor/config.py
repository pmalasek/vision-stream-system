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


# Globální instance konfigurace určená k importu napříč celou aplikací.
# Ostatní moduly by měly importovat tento objekt místo přímého volání os.getenv(),
# aby konfigurace zůstala centralizovaná a snadno testovatelná.
config = Config()
