# vprocessor

Python FastAPI služba, která přijímá RTSP video stream, detekuje osoby pomocí YOLOv8 a streamuje anotovaný výstup jako MJPEG, zatímco v reálném čase vysílá detekční metadata přes Socket.IO.

---

## Obsah

- [Přehled](#přehled)
- [Architektura](#architektura)
- [Požadavky](#požadavky)
- [Konfigurace](#konfigurace)
- [Spuštění](#spuštění)
  - [Lokálně (venv)](#lokálně-venv)
  - [Docker](#docker)
- [HTTP API](#http-api)
- [Socket.IO události](#socketio-události)
- [Výstupní soubory](#výstupní-soubory)
- [Struktura projektu](#struktura-projektu)

---

## Přehled

| Schopnost | Detail |
|---|---|
| Vstup | RTSP stream (např. z **vstreamer** na `rtsp://localhost:8554/live`) |
| Detekce | YOLOv8n – pouze třída 0 (osoby) |
| Živý výstup | MJPEG stream na `GET /stream` |
| Real-time události | Socket.IO události `detection` a `stats` |
| Výstup na disk | Anotované MP4 video + JSONL metadata |

---

## Architektura

Služba je postavena na **dvouvláknovém designu** v `processor.py`:

- **Frame grabber** (`_grabber_executor`) – drží RTSP spojení, volá `cap.read()` v těsné smyčce a ukládá poslední surový snímek do `_latest_raw_frame`, poté nastavuje `_raw_frame_event`.
- **Processing thread** (`_executor`) – čeká na `_raw_frame_event`, bere nejnovější surový snímek, spouští YOLO inferenci, kóduje JPEG, zapisuje na disk a vysílá Socket.IO události.

Oba procesy běží v `ThreadPoolExecutor` a neblokují asyncio event loop.

Při ukončení `stop()` nastaví `running=False`, `_stop_event` a `_raw_frame_event`, čímž zajistí okamžité dokončení obou vláken.

```
RTSP zdroj
    │
    ▼
cv2.VideoCapture           (vlákno přes run_in_executor)
    │
    ▼
PersonDetector             (YOLOv8 inference, kreslení ohraničujících rámečků)
    │
    ├──► VideoRecorder     (MP4 + JSONL → output/<timestamp>/)
    │
    ├──► latest_frame      (JPEG bajty, čte endpoint /stream)
    │
    └──► Socket.IO         (události "detection" a "stats" → klienti)
```

---

## Požadavky

- Python 3.11+
- Závislosti v `requirements.txt`
- Dostupný RTSP zdroj (např. služba **vstreamer**)

Při prvním spuštění ultralytics automaticky stáhne váhy YOLOv8n (`yolov8n.pt`, ~6 MB) do adresáře `models/`.

---

## Konfigurace

Veškerá nastavení jsou načítána z proměnných prostředí. Lze je nastavit přímo nebo přes soubor `.env` v adresáři `vprocessor/`.

| Proměnná | Výchozí hodnota | Popis |
|---|---|---|
| `RTSP_URL` | `rtsp://localhost:8554/live` | Adresa vstupního RTSP streamu |
| `RTSP_TRANSPORT` | `tcp` | Transportní protokol pro RTSP (tcp/udp) |
| `OUTPUT_DIR` | `./output` | Kořenový adresář pro nahrávky |
| `YOLO_MODEL` | `yolov8n.pt` | Soubor s váhami YOLOv8 nebo název modelu |
| `CONFIDENCE_THRESHOLD` | `0.5` | Minimální práh spolehlivosti (0.0 – 1.0) |
| `HOST` | `0.0.0.0` | Bind adresa pro uvicorn |
| `PORT` | `8000` | Bind port pro uvicorn |

> **Poznámka:** `RTSP_TRANSPORT=tcp` je výchozí, aby se předešlo H.264 artefaktům při ztrátě UDP paketů.

---

## Spuštění

### Lokálně (venv)

```bash
cd vprocessor
python3.11 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Nebo přímo přes uvicorn:

```bash
uvicorn main:socket_app --host 0.0.0.0 --port 8000 --reload
```

Pokud RTSP zdroj není dostupný, služba se každé 2 sekundy pokusí o znovupřipojení a zaloguje varování. HTTP endpointy a Socket.IO zůstávají dostupné po celou dobu.

### Docker

```bash
docker build -t vprocessor .
docker run -p 8000:8000 \
  -e RTSP_URL=rtsp://host.docker.internal:8554/live \
  -v ./output:/app/output \
  vprocessor
```

---

## HTTP API

### `GET /stream`

MJPEG stream anotovaného videa. Lze vložit přímo do prohlížeče:

```html
<img src="http://localhost:8000/stream" />
```

- **Media type:** `multipart/x-mixed-replace; boundary=frame`
- **CORS:** povoleno ze všech zdrojů

---

### `GET /health`

Liveness/readiness probe.

```json
{
  "status": "streaming",
  "uptime": 42.7,
  "fps": 24.3,
  "connected_clients": 1
}
```

---

### `GET /api/stats`

Aktuální statistiky zpracování.

```json
{
  "fps": 24.3,
  "total_frames": 1042,
  "total_detections": 318,
  "uptime_seconds": 42.7,
  "status": "streaming"
}
```

Možné hodnoty `status`: `idle`, `starting`, `connecting`, `streaming`, `reconnecting`, `stopped`.

---

### `GET /api/detections?limit=100&offset=0`

Historie detekcí ze JSONL souboru aktuální relace.

| Parametr | Typ | Výchozí | Popis |
|---|---|---|---|
| `limit` | integer | `100` | Maximální počet vrácených záznamů |
| `offset` | integer | `0` | Počet přeskočených záznamů |

```json
[
  {
    "frame_id": 101,
    "timestamp": 1712345678.123,
    "person_count": 1,
    "detections": [
      { "x1": 120, "y1": 45, "x2": 310, "y2": 480, "confidence": 0.9231 }
    ]
  }
]
```

---

## Socket.IO události

Připojte se na `http://localhost:8000` libovolným Socket.IO v5 klientem (endpoint `/socket.io`).

### Server → klient: `detection`

Odesláno pro každý zpracovaný snímek.

```json
{
  "frame_id": 101,
  "timestamp": 1712345678.123,
  "person_count": 1,
  "detections": [
    { "x1": 120, "y1": 45, "x2": 310, "y2": 480, "confidence": 0.9231 }
  ]
}
```

### Server → klient: `stats`

Odesláno jednou za sekundu a ihned po připojení klienta (pro okamžité naplnění dashboardu).

```json
{
  "fps": 24.3,
  "total_frames": 1042,
  "total_detections": 318,
  "uptime_seconds": 42.7,
  "status": "streaming"
}
```

---

## Výstupní soubory

Při každém spuštění se vytvoří nová nahrávací relace v `OUTPUT_DIR`:

```text
output/
└── 2024-04-05_14-32-10/
    ├── output.mp4          ← anotované video (MP4, kodek mp4v, 25 fps)
    └── detections.jsonl    ← jeden JSON objekt na řádek, jeden na snímek
```

Formát záznamu JSONL:

```jsonl
{"frame_id": 1, "timestamp": 1712345600.001, "person_count": 0, "detections": []}
{"frame_id": 2, "timestamp": 1712345600.041, "person_count": 1, "detections": [{"x1": 50, "y1": 30, "x2": 200, "y2": 420, "confidence": 0.8812}]}
```

---

## Struktura projektu

```text
vprocessor/
├── config.py          # konfigurace z proměnných prostředí
├── detector.py        # PersonDetector – YOLOv8 inference + anotace
├── recorder.py        # VideoRecorder – zápis MP4 + JSONL
├── processor.py       # VideoProcessor – asynchronní pipeline orchestrátor
├── main.py            # FastAPI aplikace, Socket.IO server, HTTP endpointy
├── requirements.txt
├── Dockerfile
└── output/            # vytváří se automaticky; obsahuje nahrávky
```
