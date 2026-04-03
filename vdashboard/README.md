# vdashboard

React webový dashboard pro real-time monitorování videa v rámci Vision Stream System.

Postaven na **React 18 + TypeScript + Vite + Tailwind CSS**.

## Co zobrazuje

- 📹 Živý MJPEG video stream ze služby `vprocessor`
- 📡 Real-time metadata detekcí přes Socket.IO (události `detection`, `stats`)
- 📊 Statistiky: FPS, počet osob, celkový počet detekcí, uptime
- 🌙 Responzivní dark UI (mobilní i desktop)

## Požadavky

- Node.js 18+
- Běžící instance služby `vprocessor` (výchozí: `http://localhost:8000`)

## Lokální vývoj

```bash
cd vdashboard
npm install
npm run dev
```

Dashboard bude dostupný na [http://localhost:5173](http://localhost:5173).

Vývojový server automaticky přesměrovává tyto cesty na `vprocessor`:

| Cesta | Cíl |
|-------|-----|
| `/stream` | `http://localhost:8000` |
| `/socket.io` | `http://localhost:8000` (WebSocket) |
| `/api` | `http://localhost:8000` |

## Konfigurace

| Proměnná | Výchozí | Popis |
|----------|---------|-------|
| `VITE_PROCESSOR_URL` | `http://localhost:8000` | URL služby `vprocessor` |

Proměnné lze nastavit v souboru `.env.local` v adresáři `vdashboard/`.

> Ve vývoji zajišťuje směrování Vite proxy, `VITE_PROCESSOR_URL` je potřeba pouze při přímém připojení na vzdálený server (např. staging) bez použití proxy.

## Produkční sestavení

```bash
npm run build      # výstup do dist/
npm run preview    # lokální náhled produkčního sestavení
```

## Docker

```bash
docker build -t vdashboard .
docker run -p 3000:80 vdashboard
```

Nebo přes kořenový `docker-compose.yml`, který automaticky propojí všechny služby:

```bash
docker compose up --build
```

Dashboard bude dostupný na [http://localhost:3000](http://localhost:3000) — uvnitř kontejneru běží nginx na portu 80.

## Architektura Socket.IO připojení

Socket.IO klient je implementován jako modul-level singleton (`_socket`) v `useSocket.ts`, nikoliv uvnitř React efektu. Tím se zabraňuje nežádoucímu odpojení a reconnectu při React 18 StrictMode (dvojité volání `useEffect`). Hook `useSocket` pouze registruje a odregistruje event listenery — `socket.disconnect()` nikdy nevolá.

## Struktura projektu

```text
vdashboard/
├── public/
├── src/
│   ├── components/
│   │   ├── Header.tsx          # horní lišta s názvem, hodinami, stavem připojení
│   │   ├── VideoStream.tsx     # MJPEG <img> s overlay stavy
│   │   ├── StatsPanel.tsx      # karty FPS / snímky / detekce / uptime
│   │   └── DetectionPanel.tsx  # počet osob + confidence bary
│   ├── hooks/
│   │   └── useSocket.ts        # Socket.IO singleton + obsluha událostí
│   ├── types.ts                # sdílená TypeScript rozhraní
│   ├── App.tsx                 # root layout
│   ├── index.css               # Tailwind direktivy + základní styly
│   └── main.tsx                # React 18 entry point
├── index.html
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
├── tsconfig.node.json
├── Dockerfile
└── nginx.conf
```
