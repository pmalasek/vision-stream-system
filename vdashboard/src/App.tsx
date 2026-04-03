/**
 * @module App
 *
 * Kořenová komponenta celé aplikace vdashboard.
 *
 * Zodpovídá za:
 *  - inicializaci Socket.IO spojení prostřednictvím hooku `useSocket`
 *  - sestavení hlavního layoutu (Header → main grid → footer)
 *  - předávání dat z WebSocket událostí do jednotlivých panelů
 *
 * Layout na velkých obrazovkách (lg+):
 *   ┌─────────────────────────────────┐
 *   │            Header               │  ← fixní výška, shrink-0
 *   ├──────────────────┬──────────────┤
 *   │  VideoStream     │  StatsPanel  │  ← flex-1, overflow skrytý
 *   │  (2/3 šířky)     │──────────────│
 *   │                  │DetectionPanel│
 *   ├──────────────────┴──────────────┤
 *   │            Footer               │  ← fixní výška, shrink-0
 *   └─────────────────────────────────┘
 */

import Header from "./components/Header";
import VideoStream from "./components/VideoStream";
import StatsPanel from "./components/StatsPanel";
import DetectionPanel from "./components/DetectionPanel";
import { useSocket } from "./hooks/useSocket";
import { useEffect, useRef, useState } from "react";
import type { DetectionEvent, FrameMetaEvent } from "./types";

/**
 * Základní URL adresa backendu (processor služby).
 *
 * Hodnota se čte z Vite proměnné prostředí `VITE_PROCESSOR_URL`, která
 * se nastavuje v souboru `.env` (nebo `.env.local`) jako:
 *   VITE_PROCESSOR_URL=http://muj-server:8000
 *
 * Pokud proměnná není definována, použije se stejný origin jako dashboard
 * (`window.location.origin`) a volání jdou přes nginx proxy (`/stream`, `/api`,
 * `/socket.io`).
 *
 * Prefix `VITE_` je povinný – Vite jiné proměnné do klientského kódu
 * nevkládá (bezpečnostní pravidlo).
 */
const PROCESSOR_BASE =
  (import.meta.env.VITE_PROCESSOR_URL as string | undefined) ??
  window.location.origin;

/**
 * Volitelná přímá URL adresa MJPEG streamu.
 *
 * Pokud je nastavena `VITE_STREAM_URL`, použije se přímo pro `<img src>`
 * a obejde nginx proxy ve `vdashboard`. To je výhodné v produkci, kde chceme
 * stream vést jinou cestou než zbytek dashboard provozu (Socket.IO, API).
 */
const DIRECT_STREAM_URL = import.meta.env.VITE_STREAM_URL as string | undefined;

/** Volitelná přímá URL pro WebRTC signaling endpoint backendu. */
const WEBRTC_BASE_URL =
  (import.meta.env.VITE_WEBRTC_URL as string | undefined) ?? PROCESSOR_BASE;

/** Zapíná WebRTC přehrávání videa (výchozí true). */
const USE_WEBRTC =
  (import.meta.env.VITE_USE_WEBRTC as string | undefined)?.toLowerCase() !==
  "false";

/**
 * Plná URL adresa MJPEG video streamu.
 *
 * Sestavuje se připojením cesty `/stream` k základní URL procesoru.
 * Tato adresa se předává komponentě `VideoStream` jako atribut `src`
 * pro tag `<img>`, který MJPEG stream přehrává nativně v prohlížeči.
 */
const STREAM_URL = DIRECT_STREAM_URL ?? `${PROCESSOR_BASE}/stream`;

/**
 * Kořenová komponenta aplikace vdashboard.
 *
 * Získává živá data (detekce osob, statistiky, stav spojení) z hooku
 * `useSocket` a distribuuje je do potomků. Sama neobsahuje žádnou
 * aplikační logiku – pouze kompozici layoutu.
 *
 * @returns JSX strom celé aplikace
 */
export default function App() {
  // Připojení k Socket.IO serveru; hook udržuje stav spojení a
  // reaktivně aktualizuje `latestDetection` a `stats` při každé události.
  const { latestDetection, stats, isConnected } = useSocket();

  // Poslední frame_id potvrzený WebRTC data channel (reálně přehraný obraz).
  const [videoFrameMeta, setVideoFrameMeta] = useState<FrameMetaEvent | null>(
    null,
  );

  // Detekce doručené dříve přes Socket.IO než dojde odpovídající video frame.
  const pendingDetectionsRef = useRef<DetectionEvent[]>([]);

  // Detekce, kterou skutečně zobrazíme v UI (po synchronizaci na frame_id videa).
  const [syncedDetection, setSyncedDetection] = useState<DetectionEvent | null>(
    null,
  );

  useEffect(() => {
    if (!latestDetection) return;

    if (!USE_WEBRTC) {
      setSyncedDetection(latestDetection);
      return;
    }

    pendingDetectionsRef.current.push(latestDetection);
    if (pendingDetectionsRef.current.length > 500) {
      pendingDetectionsRef.current = pendingDetectionsRef.current.slice(-500);
    }
  }, [latestDetection]);

  useEffect(() => {
    if (!USE_WEBRTC) return;
    if (!videoFrameMeta) return;

    const currentFrameId = videoFrameMeta.frame_id;
    if (!Number.isFinite(currentFrameId) || currentFrameId <= 0) return;

    const queue = pendingDetectionsRef.current;
    let newestReady: DetectionEvent | null = null;
    const remaining: DetectionEvent[] = [];

    for (const evt of queue) {
      if (evt.frame_id <= currentFrameId) {
        newestReady = evt;
      } else {
        remaining.push(evt);
      }
    }

    pendingDetectionsRef.current = remaining;
    if (newestReady) {
      setSyncedDetection(newestReady);
    }
  }, [videoFrameMeta]);

  return (
    /*
     * Kořenový kontejner s flexbox layoutem (flex-col).
     *
     * Na mobilech:   min-h-screen  → stránka je minimálně výška viewportu,
     *                               obsah může přetéct a scrollovat.
     * Na desktopu:   lg:h-screen   → přesně výška viewportu,
     *                lg:overflow-hidden → zabraňuje scrollování celé stránky;
     *                               scroll je povolen pouze uvnitř pravého sloupce.
     */
    <div className="min-h-screen lg:h-screen bg-gray-900 text-white flex flex-col lg:overflow-hidden">
      {/* Záhlaví aplikace – zobrazuje název a indikátor stavu spojení */}
      <Header isConnected={isConnected} />

      {/*
       * Hlavní obsahová oblast.
       *
       * flex-1       → zabírá veškerý zbývající prostor mezi Header a Footer
       * lg:min-h-0   → opravuje chování flex potomka v Safari; bez toho by
       *                flex-1 nerespektoval výšku rodiče a layout by přetekl
       * lg:overflow-hidden → potlačuje případný přetok z vnitřního gridu
       */}
      <main className="flex-1 p-4 lg:p-6 lg:min-h-0 lg:overflow-hidden">
        {/*
         * 3-sloupcový responzivní grid.
         *
         * Mobil (výchozí):  1 sloupec  → panely se skládají pod sebe
         * Deskstop (lg+):   3 sloupce  → levý sloupec zabírá 2/3, pravý 1/3
         *
         * lg:h-full zajišťuje, že grid vyplní celou výšku elementu <main>,
         * takže sloupce mohou správně uplatnit svou vlastní výškovou logiku.
         */}
        <div className="max-w-screen-2xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-4 lg:gap-6 lg:h-full">
          {/*
           * Levý sloupec – video stream (zabírá 2 ze 3 sloupců na lg+).
           *
           * flex flex-col  → umožňuje `VideoStream` uvnitř použít h-full
           *                  a vyplnit celou výšku sloupce
           * lg:min-h-0     → stejná Safari oprava jako u <main>; bez toho
           *                  by flex potomek ignoroval výškové omezení rodiče
           */}
          <div className="lg:col-span-2 flex flex-col lg:min-h-0">
            <VideoStream
              streamUrl={STREAM_URL}
              webrtcUrl={WEBRTC_BASE_URL}
              useWebRTC={USE_WEBRTC}
              onFrameMeta={setVideoFrameMeta}
              isConnected={isConnected}
              stats={stats}
            />
          </div>

          {/*
           * Pravý sloupec – statistiky a detekce (1/3 šířky na lg+).
           *
           * Strategie scrollování:
           *   lg:overflow-y-auto  → pokud obsah (StatsPanel + DetectionPanel)
           *                         přesáhne výšku sloupce, zobrazí se
           *                         scrollbar POUZE v tomto sloupci.
           *                         Levý sloupec s videem zůstane zcela statický.
           * lg:min-h-0  → opět nutné pro správné omezení výšky ve flex kontextu
           */}
          <div className="flex flex-col gap-4 lg:min-h-0 lg:overflow-y-auto">
            {/* Panel se statistikami (FPS, počet snímků, uptime, stav) */}
            <StatsPanel stats={stats} isConnected={isConnected} />
            {/* Panel s posledním detekčním eventem (bounding boxy, počet osob) */}
            <DetectionPanel
              detection={USE_WEBRTC ? syncedDetection : latestDetection}
            />
          </div>
        </div>
      </main>

      {/*
       * Zápatí aplikace.
       *
       * shrink-0 → zabraňuje zmenšení zápatí flexboxem, i kdyby byl
       *            obsah <main> příliš velký; zápatí má vždy pevnou výšku.
       */}
      <footer className="text-center text-gray-600 text-xs py-3 border-t border-gray-800 shrink-0">
        Vision Stream System &mdash; vdashboard
      </footer>
    </div>
  );
}
