# Vision Stream System

Systém pro real-time zpracování videa, který detekuje osoby ve video streamu, vykresluje kolem nich rámečky a zobrazuje výsledky v živém webovém dashboardu.

## Ukázky

<table>
<tr>
  <td><img src="img/Sn%C3%ADmek%20obrazovky%20z%202026-04-03%2013-26-10.png" alt="Detekce 2 osob v exteriéru"></td>
  <td><img src="img/Sn%C3%ADmek%20obrazovky%20z%202026-04-03%2013-26-33.png" alt="Detekce osoby v dílně – pohled z bezpečnostní kamery shora"></td>
</tr>
<tr>
  <td><img src="img/Sn%C3%ADmek%20obrazovky%20z%202026-04-03%2013-35-01.png" alt="Detekce 8 osob na ulici"></td>
  <td><img src="img/Sn%C3%ADmek%20obrazovky%20z%202026-04-03%2013-34-11.png" alt="Detekce 13 osob na rušné ulici (Bond Street)"></td>
</tr>
</table>

---

## Navržené řešení

Rozdělil jsem si zadání do tří modulů :

1. **vStreamer** - Emulátor IP kamery, převede video na H264 stream a zpřístupní přes RTSP
2. **vProcessor** - Python/FastAPI/YOLOv8 zpracovatelský a SocketIO server - přes RTSP přijímá video stream, přes YOLOv8 jej zpracuje, metadata a video ukládá do output/<YYYY-MM-DD_hh_mm_ss>/ a současně streamuje video přes MJPEG a metadata přes SocketIO na UI.
3. **vDashboard** - React/Typescript/Tailwind UI - jen zobrazuje video a metadata.

**Kdybych bych navrhoval reálný systém, postupoval bych trochu jinak**:

- Pokud by zpracování probíhalo na serverech (Cloudu), aby by byla možnost škálovat počet vWorkerů v rámci infrastruktury, rozdělil bych vProcessor na :
  - **vServer** - asi bych použil C++ nebo Go - pouze přijímá streamy z vStreamerů (kamer) a předává je ke zpracování vWorkerům, a schraňuje výsledná metadata.
  - **vWorker** - Python/YOLOv8 - samostatný "zpracovávač", který se stará o ukládání a předává zpracovaný stream přímo na vDashboard a metadata pak vServeru, který je přes SocketIO doručí na vDashboard
- Pokud by zpracování probíhalo v embedded zařízení, poohlédl bych se po nějakém SoC optimalizovaném pro zpracování obrazu, např. MCM-iMX95 (<https://www.compulab.com/products/computer-on-modules/mcm-imx95-nxp-i-mx-95-som-smd-system-on-module/#specs>) a podle počtu video vstupů bych se nebál jich spojit do virtuálního embedded serveru, kde každý SoC bude zpracovávat jen několik video streamů.

<p align="center">
  <em>--- Testováno na Lenovo Yoga Slim 7 14APU8 s nainstalovaným Ubuntu 25.10 ---</em>
</p>

## Architektura

```bash
┌─────────────────┐  RTSP :8554    ┌───────────────────────────────┐
│   vstreamer     │ ─────────────► │         vprocessor            │
│     (Go)        │                │       (Python/FastAPI)        │
│                 │                │                               │
│ gortsplib v4    │                │  Frame grabber vlákno         │
│ + ffmpeg        │                │  (drží RTSP, volá cap.read()) │
│                 │                │                               │
│ Chová se jako   │                │  Processing vlákno            │
│ IP kamera       │                │  (YOLOv8 inference, JPEG)     │
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
                                   │          vdashboard           │
                                   │     (React/TS/Tailwind)       │
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
| **vprocfast** | Go (MVP, synthetic pipeline) | `8001` (HTTP) | Alternativní Go-only procesor s kompatibilním API (`/stream`, `/api/stats`, `/api/detections`, `/socket.io`) |
| **vdashboard** | React + TypeScript + Tailwind | `3000` (HTTP) | Webový dashboard zobrazující živý stream a real-time metadata detekcí |

`vProcFast` vznikl jako **testovací prostředí pro spolupráci Go a Pythonu**: cílem je mít rychlý, jednoduše laditelný Go backend se stejným API kontraktem jako `vprocessor`, na kterém lze bezpečně ověřovat změny v pipeline, streamingu a dashboardu bez nutnosti hned zasahovat do produkčnější Python/YOLO části.

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

> **Poznámka:** Při prvním spuštění se automaticky stáhne model YOLOv8n (~6 MB) do kontejneru `vprocessor/models`.

### 4. Otevření dashboardu

Přejdi na **<http://localhost:3000>** v prohlížeči.

### 5. Volitelné spuštění vProcFast (Go-only)

`vProcFast` je dostupný jako volitelný Compose profil `fast`:

```bash
docker compose --profile fast up --build vprocfast
```

Health endpoint:

```bash
curl http://localhost:8001/health
```

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
- Node.js 18+
- ffmpeg s podporou `libx264` (musí být dostupný v `PATH`)

---

### vstreamer

```bash
cd vstreamer
go mod tidy
go build -o vstreamer .
./vstreamer --video /cesta/k/videu.mp4
```

Dostupné přepínače:

| Přepínač | Výchozí hodnota | Popis |
|----------|-----------------|-------|
| `--video` | *(povinné)* | Cesta ke vstupnímu video souboru |
| `--port` | `8554` | Port RTSP serveru |
| `--udp-rtp` | `8000` | UDP port pro RTP pakety (0 = zakázat UDP) |
| `--udp-rtcp` | `8001` | UDP port pro RTCP pakety (0 = zakázat UDP) |
| `--path` | `live` | Cesta RTSP streamu |
| `--loop` | `true` | Opakování videa po skončení |

Otestování pomocí ffplay:

```bash
ffplay rtsp://localhost:8554/live
```

---

### vprocessor

```bash
cd vprocessor
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Konfiguraci lze přepsat proměnnými prostředí nebo souborem `.env` v adresáři `vprocessor/`:

| Proměnná | Výchozí | Popis |
|----------|---------|-------|
| `RTSP_URL` | `rtsp://localhost:8554/live` | Adresa vstupního RTSP streamu |
| `RTSP_TRANSPORT` | `tcp` | Transportní protokol pro RTSP (`tcp` / `udp`) |
| `OUTPUT_DIR` | `./output` | Kořenový adresář pro nahrávky |
| `YOLO_MODEL` | `yolov8n.pt` | Soubor s váhami YOLOv8 nebo název modelu |
| `CONFIDENCE_THRESHOLD` | `0.5` | Minimální práh spolehlivosti (0.0 – 1.0) |
| `HOST` | `0.0.0.0` | Bind adresa pro uvicorn |
| `PORT` | `8000` | Bind port pro uvicorn |

> **Poznámka:** `RTSP_TRANSPORT=tcp` je výchozí, aby se předešlo H.264 artefaktům způsobeným ztrátou UDP paketů.

Dostupné API endpointy:

| Metoda | Cesta | Popis |
|--------|-------|-------|
| `GET` | `/stream` | MJPEG video stream |
| `GET` | `/hls/stream.m3u8` | LL-HLS playlist (segmenty pod `/hls/*.ts`) |
| `POST` | `/webrtc/offer` | WebRTC signaling endpoint (SDP offer/answer) |
| `GET` | `/health` | Zdravotní stav služby + statistiky |
| `GET` | `/api/stats` | Aktuální statistiky zpracování |
| `GET` | `/api/detections?limit=100&offset=0` | Historie detekcí ze souboru JSONL |
| `WS` | `/socket.io` | Real-time události (`detection`, `stats`) |

Socket.IO události vysílané serverem:

```jsonc
// "detection" — odesláno při každém zpracovaném snímku
{
  "frame_id": 42,
  "timestamp": 1712345678.123,
  "person_count": 2,
  "detections": [
    { "x1": 100, "y1": 80, "x2": 200, "y2": 400, "confidence": 0.91 }
  ]
}

// "stats" — odesláno každou sekundu + ihned po připojení klienta
{
  "fps": 24.8,
  "total_frames": 1500,
  "total_detections": 312,
  "uptime_seconds": 60.3,
  "status": "streaming"
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
npm run dev
```

Otevři **http://localhost:5173**

Proměnnou `VITE_PROCESSOR_URL` lze nastavit v souboru `.env.local` v adresáři `vdashboard/` (ve vývoji ji Vite proxy nahrazuje automaticky):

```env
VITE_PROCESSOR_URL=http://localhost:8000
```

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

| Služba | Protokol | Port | URL / poznámka |
|--------|----------|------|----------------|
| vstreamer | RTSP/TCP | 8554 | `rtsp://localhost:8554/live` |
| vstreamer | UDP RTP | 8556 | UDP transport pro RTP pakety |
| vstreamer | UDP RTCP | 8557 | UDP transport pro RTCP pakety |
| vprocessor | HTTP / WS | 8000 | `http://localhost:8000` |
| vprocfast | HTTP / WS | 8001 | `http://localhost:8001` |
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
│   └── README.md
├── vprocfast/                  ← Go-only MVP zpracovatelská služba
│   ├── go.mod
│   ├── main.go
│   ├── Dockerfile
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
    └── README.md
```

---

## Připojení reálné IP kamery

Protože `vprocessor` čte standardní RTSP URL, lze `vstreamer` zcela nahradit reálnou IP kamerou. Stačí aktualizovat proměnnou prostředí `RTSP_URL`:

```env
# .env nebo docker-compose override
RTSP_URL=rtsp://admin:heslo@192.168.1.100:554/stream
```

Pokud kamera vysílá přes UDP a dochází k artefaktům, přepni transport na TCP:

```env
RTSP_TRANSPORT=tcp
```

Žádné další změny nejsou potřeba.

---

## Licence

MIT
