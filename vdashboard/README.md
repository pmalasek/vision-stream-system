# vdashboard

Real-time video monitoring dashboard for the Vision Stream System.

Built with **React 18 + TypeScript + Vite + Tailwind CSS**.

## Features

- 📹 Live MJPEG video stream from `vprocessor`
- 📡 Real-time detection metadata via Socket.IO
- 📊 Live statistics (FPS, person count, total detections, uptime)
- 🌙 Clean dark-themed UI

## Prerequisites

- Node.js 18+
- A running instance of `vprocessor` (default: `http://localhost:8000`)

## Development

```sh
npm install
npm run dev
```

The dashboard will be available at [http://localhost:5173](http://localhost:5173).

The Vite dev server automatically proxies the following paths to `vprocessor`:

| Path | Target |
|---|---|
| `/stream` | `http://localhost:8000` |
| `/socket.io` | `http://localhost:8000` (WebSocket) |
| `/api` | `http://localhost:8000` |

## Configuration

Copy `.env.example` to `.env.local` and adjust as needed:

```sh
cp .env.example .env.local
```

| Variable | Default | Description |
|---|---|---|
| `VITE_PROCESSOR_URL` | `http://localhost:8000` | Base URL of the `vprocessor` service |

> In development the Vite proxy handles routing, so `VITE_PROCESSOR_URL` is
> only needed when you want to point directly at a remote processor without the
> proxy (e.g. a staging server).

## Production Build

```sh
npm run build       # outputs to dist/
npm run preview     # preview the production build locally
```

## Docker

Build and run the containerised dashboard (requires the `vprocessor` container
to be reachable as `vprocessor` on the same Docker network):

```sh
docker build -t vdashboard .
docker run -p 80:80 vdashboard
```

Or use the root `docker-compose.yml` which wires everything up automatically:

```sh
docker compose up --build
```

The dashboard will be served by nginx on port **80**.

## Project Structure

```
vdashboard/
├── public/
├── src/
│   ├── components/
│   │   ├── Header.tsx          # Top bar with title, clock, connection status
│   │   ├── VideoStream.tsx     # MJPEG <img> with overlay states
│   │   ├── StatsPanel.tsx      # FPS / frames / detections / uptime cards
│   │   └── DetectionPanel.tsx  # Person count + per-detection confidence bars
│   ├── hooks/
│   │   └── useSocket.ts        # Socket.IO connection + event handling
│   ├── types.ts                # Shared TypeScript interfaces
│   ├── App.tsx                 # Root layout
│   ├── index.css               # Tailwind directives + base styles
│   └── main.tsx                # React 18 entry point
├── index.html
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
├── tsconfig.node.json
├── Dockerfile
├── nginx.conf
└── .env.example
```
