# Vision Stream System

Systém pro real-time zpracování videa, který detekuje osoby ve video streamu, vykresluje kolem nich rámečky a zobrazuje výsledky v živém webovém dashboardu.

## Architektura

```
┌─────────────────┐  RTSP :8554    ┌──────────────────────────────┐
│   vstreamer     │ ─────────────► │         vprocessor            │
│     (Go)        │                │       (Python/FastAPI)        │
│                 │                │                               │
│ gortsplib v4    │                │  cv2.VideoCapture(rtsp://...) │
│ + ffmpeg        │                │  YOLOv8 detekce osob          │
│                 │                │  vykreslení rámečků           │
│ Chová se jako   │                │  ukládání videa na disk       │
│ IP kamera       │                │  ukládání metadat (JSONL)     │
└─────────────────┘                │                               │
                                   │  GET  /stream   (MJPEG)       │
                                   │  WS   /socket.io              │
                                   │  GET  /api/detections         │
                                   │  GET  /health                 │
                                   └──────────────┬────────────────┘
                                                  │
                                      MJPEG + Socket.IO
                                                  │
                                   ┌──────────────▼────────────────┐
                                   │          vdashboard            │
                                   │     (React/TS/Tailwind)        │
                                   │                               │
                                   │  Živý video stream            │
                                   │  Real-time statistiky detekcí │
                                   │  Počet osob a historie        │
                                   └───────────────────────────────┘
```

## Komponenty

| Komponenta | Technologie | Port | Popis |
|-----------|-------------|------|-------|
| **vstreamer** | Go + gortsplib + ffmpeg | `8554` (RTSP) | Načte video soubor a streamuje ho jako RTSP stream — chová se jako IP kamera |
| **vprocessor** | Python + FastAPI + YOLOv8 | `8000` (HTTP) | Zachytává RTSP stream, detekuje osoby, streamuje výsledky přes MJPEG + Socket.IO |
| **vdashboard** | React + TypeScript + Tailwind | `3000` (HTTP) | Webový dashboard zobrazující živý stream a real-time metadata detekcí |

---

## Rychlý start (Docker Compose)

### 1. Požadavky

- [Docker](https://docs.docker.com/get-docker/) s Docker Compose v2
- Video soubor (MP4, AVI, MKV nebo jakýkoliv formát podporovaný ffmpeg)

### 2. Příprava video souboru

```bash
mkdir -p videos
cp /cesta/k/vasemu/videu.mp4 videos/input.mp4
```

### 3. Spuštění všech služeb

```bash
docker compose up --build
```

> **Poznámka:** Při prvním spuštění se automaticky stáhne model YOLOv8n (~6 MB) do kontejneru `vprocessor`.

### 4. Otevření dashboardu

Přejdi na **http://localhost:3000** v prohlížeči.

### Proměnné prostředí

Vytvoř soubor `.env` v kořenovém adresáři projektu pro přepsání výchozích hodnot:

```env
# Cesta k videu uvnitř kontejneru vstreamer (mapováno z ./videos na hostiteli)
VIDEO_FILE=/videos/input.mp4

# Velikost modelu YOLOv8: yolov8n.pt | yolov8s.pt | yolov8m.pt | yolov8l.pt | yolov8x.pt
YOLO_MODEL=yolov8n.pt

# Minimální práh spolehlivosti pro detekci osob (0.0 – 1.0)
CONFIDENCE_THRESHOLD=0.5
```

---

## Lokální vývoj (bez Dockeru)

### Požadavky

- Go 1.21+
- Python 3.11+
- Node.js 20+
- ffmpeg (musí být dostupný v `PATH`)

---

### vstreamer

```bash
cd vstreamer
go mod download
go run . --video /cesta/k/videu.mp4
```

Dostupné přepínače:

| Přepínač | Výchozí hodnota | Popis |
|----------|-----------------|-------|
| `--video` | *(povinné)* | Cesta ke vstupnímu video souboru |
| `--port` | `8554` | Port RTSP serveru |
| `--path` | `live` | Cesta RTSP streamu |
| `--loop` | `true` | Opakování videa po skončení |

Otestování pomocí VLC nebo ffplay:

```bash
ffplay rtsp://localhost:8554/live
```

---

### vprocessor

```bash
cd vprocessor
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # uprav dle potřeby
uvicorn main:socket_app --host 0.0.0.0 --port 8000 --reload
```

Dostupné API endpointy:

| Metoda | Cesta | Popis |
|--------|-------|-------|
| `GET` | `/stream` | MJPEG video stream |
| `GET` | `/health` | Zdravotní stav služby + statistiky |
| `GET` | `/api/stats` | Aktuální statistiky zpracování |
| `GET` | `/api/detections?limit=100&offset=0` | Historie detekcí ze souboru JSONL |
| `WS` | `/socket.io` | Real-time události (detection, stats) |

Socket.IO události vysílané serverem:

```jsonc
// "detection" — odesláno při každém zpracovaném snímku
{
  "frame_id": 42,
  "timestamp": "2024-01-15T10:30:00Z",
  "person_count": 2,
  "detections": [
    { "x1": 100, "y1": 80, "x2": 200, "y2": 400, "confidence": 0.91 }
  ]
}

// "stats" — odesláno každou sekundu
{
  "fps": 24.8,
  "total_frames": 1500,
  "total_detections": 312,
  "uptime_seconds": 60.3,
  "status": "running"
}
```

Záznamy jsou ukládány do `output/<YYYY-MM-DD_HH-MM-SS>/`:

```
output/
└── 2024-01-15_10-30-00/
    ├── output.mp4          ← video s vyznačenými detekcemi
    └── detections.jsonl    ← jeden JSON objekt na řádek, jeden na snímek
```

---

### vdashboard

```bash
cd vdashboard
npm install

cp .env.example .env             # uprav VITE_PROCESSOR_URL dle potřeby
npm run dev
```

Otevři **http://localhost:5173**

Sestavení pro produkci:

```bash
npm run build
npm run preview
```

---

## Tok dat

```
video.mp4
    │
    ▼
vstreamer  ──── RTSP (H.264) ────►  vprocessor
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                     /stream        Socket.IO       /app/output/
                    (MJPEG)        (metadata)     output.mp4
                          │              │        detections.jsonl
                          └──────────────┘
                                   │
                              vdashboard
                           (prohlížeč, port 3000)
```

---

## Přehled portů

| Služba | Protokol | Port | URL |
|--------|----------|------|-----|
| vstreamer | RTSP | 8554 | `rtsp://localhost:8554/live` |
| vprocessor | HTTP / WS | 8000 | `http://localhost:8000` |
| vdashboard | HTTP | 3000 | `http://localhost:3000` |

---

## Struktura projektu

```
vision-stream-system/
├── docker-compose.yml
├── README.md
├── videos/                     ← sem vlož vstupní video
│   └── input.mp4
├── vstreamer/                  ← Go RTSP server
│   ├── go.mod
│   ├── go.sum
│   ├── main.go
│   ├── Dockerfile
│   └── README.md
├── vprocessor/                 ← Python zpracovatelská služba
│   ├── main.py
│   ├── processor.py
│   ├── detector.py
│   ├── recorder.py
│   ├── config.py
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── .env.example
│   └── README.md
└── vdashboard/                 ← React dashboard
    ├── src/
    │   ├── App.tsx
    │   ├── main.tsx
    │   ├── index.css
    │   ├── types.ts
    │   ├── hooks/
    │   │   └── useSocket.ts
    │   └── components/
    │       ├── Header.tsx
    │       ├── VideoStream.tsx
    │       ├── StatsPanel.tsx
    │       └── DetectionPanel.tsx
    ├── package.json
    ├── vite.config.ts
    ├── Dockerfile
    ├── nginx.conf
    ├── .env.example
    └── README.md
```

---

## Připojení reálné IP kamery

Protože `vprocessor` čte standardní RTSP URL, lze `vstreamer` zcela nahradit reálnou IP kamerou. Stačí aktualizovat proměnnou prostředí `RTSP_URL`:

```env
# .env nebo docker-compose override
RTSP_URL=rtsp://admin:heslo@192.168.1.100:554/stream
```

Žádné další změny nejsou potřeba.

---

## Licence

MIT
