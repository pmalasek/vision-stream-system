/**
 * Komponenta VideoStream
 *
 * Zobrazuje živý MJPEG video stream ze serveru vprocessor a překrývá jej
 * stavovými overlays podle aktuálního stavu procesoru a stavu HTTP spojení.
 *
 * Tři možné vizuální stavy video plochy:
 *  1. „No-signal" overlay  – žádný použitelný snímek (odpojeno, chyba, načítání)
 *  2. „Frozen-frame" overlay – poslední přijatý snímek je vidět, ale stream
 *                              není živý (idle, starting, connecting, …)
 *  3. Živý stream           – MJPEG pipe aktivně dodává snímky, zobrazí se LIVE badge
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import type { FrameMetaEvent, StatsEvent } from "../types";

/**
 * Props komponenty VideoStream.
 *
 * @property streamUrl    – URL MJPEG endpointu (např. „/stream"); předává se
 *                          přímo do atributu `src` elementu `<img>`.
 * @property isConnected  – true pokud je Socket.IO spojení se serverem aktivní.
 * @property stats        – poslední stavová zpráva přijatá přes Socket.IO,
 *                          nebo null dokud nepřijde první událost.
 */
interface VideoStreamProps {
  streamUrl: string;
  webrtcUrl?: string;
  useWebRTC?: boolean;
  onFrameMeta?: (meta: FrameMetaEvent) => void;
  isConnected: boolean;
  stats: StatsEvent | null;
}

async function waitForIceGatheringComplete(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return;

  await new Promise<void>((resolve) => {
    const onStateChange = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", onStateChange);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", onStateChange);

    setTimeout(() => {
      pc.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    }, 3000);
  });
}

/**
 * Lookup tabulka stavů procesoru → vizuální metadata pro „frozen-frame" overlay.
 *
 * Klíče odpovídají hodnotám pole `status` ze zprávy StatsEvent, která přichází
 * ze serveru přes Socket.IO. Pro každý stav definujeme:
 *   - icon  – emoji zobrazená jako hlavní ikona overlaye
 *   - title – krátký nadpis stavu
 *   - sub   – podrobnější popis / instrukce pro uživatele
 *   - color – Tailwind třída barvy pro text nadpisu
 *
 * Stav „streaming" má záměrně prázdné hodnoty: když procesor streamuje,
 * overlay se vůbec nevykreslí (řídí to podmínka `showFrozenOverlay`), takže
 * tyto hodnoty nejsou nikdy použity. Záznam zde existuje pouze proto, aby
 * TypeScript uzavřel výčet a nevynucoval zvláštní větev v typech.
 */
const STATUS_OVERLAY: Record<
  StatsEvent["status"],
  { icon: string; title: string; sub: string; color: string }
> = {
  idle: {
    icon: "⏸",
    title: "Stream idle",
    sub: "Processor is idle, waiting to start…",
    color: "text-gray-400",
  },
  starting: {
    icon: "⚙️",
    title: "Starting up",
    sub: "Processor is initialising…",
    color: "text-blue-400",
  },
  connecting: {
    icon: "🔗",
    title: "Connecting to source",
    sub: "Establishing RTSP connection…",
    color: "text-yellow-400",
  },
  reconnecting: {
    icon: "🔄",
    title: "Reconnecting",
    sub: "Source lost — retrying…",
    color: "text-yellow-400",
  },
  /**
   * Stav „streaming": overlay se při tomto stavu nikdy nezobrazí,
   * proto jsou všechny hodnoty prázdné řetězce.
   */
  streaming: {
    icon: "",
    title: "",
    sub: "",
    color: "",
  },
  stopped: {
    icon: "⏹",
    title: "Stream stopped",
    sub: "Processor has been stopped.",
    color: "text-red-400",
  },
  error: {
    icon: "⚠️",
    title: "Stream error",
    sub: "An error occurred in the processor.",
    color: "text-red-400",
  },
};

/**
 * VideoStream – hlavní prezentační komponenta pro MJPEG video stream.
 *
 * Skládá se ze dvou částí:
 *  - Hlavička karty s názvem a connection badge
 *  - Video plocha s `<img>` elementem a třemi možnými overlays
 *
 * Komponenta záměrně udržuje `<img>` element vždy namontovaný v DOM, i když
 * stream není viditelný. Díky tomu browser udržuje HTTP long-poll spojení
 * (MJPEG pipe) živé a první snímek se zobrazí bez prodlevy jakmile server
 * začne odesílat data.
 */
const VideoStream: React.FC<VideoStreamProps> = ({
  streamUrl,
  webrtcUrl,
  useWebRTC = true,
  onFrameMeta,
  isConnected,
  stats,
}) => {
  /**
   * imgError – true pokud browser nahlásil chybu při načítání `<img>`.
   * Typicky nastane při HTTP 4xx/5xx odpovědi serveru nebo při výpadku sítě
   * poté, co bylo spojení navázáno. Resetuje se při úspěšném načtení.
   */
  const [imgError, setImgError] = useState(false);

  /**
   * imgLoaded – true jakmile browser přijme a zobrazí alespoň jeden snímek
   * (první volání `onLoad` na MJPEG streamu). Zůstane true i při přerušení
   * streamu – browser stále drží poslední zobrazený snímek v paměti.
   * Resetuje se při chybě, aby se znovu ukázal loading spinner po reconnectu.
   */
  const [imgLoaded, setImgLoaded] = useState(false);
  const [webrtcState, setWebrtcState] = useState<
    "idle" | "connecting" | "live" | "error"
  >("idle");
  const videoRef = useRef<HTMLVideoElement>(null);

  /**
   * handleError – voláno browserem při chybě načítání `<img>`.
   * Přepne stav do chybového režimu a zruší příznak úspěšného načtení.
   * Výsledkem je zobrazení „no-signal" overlaye s ikonou odpojené kamery.
   */
  const handleError = () => {
    setImgError(true);
    setImgLoaded(false);
  };

  /**
   * handleLoad – voláno browserem při každém úspěšně přijatém snímku.
   * Na MJPEG streamu se `onLoad` spouští opakovaně s každým novým JPEG framem.
   * Vymaže chybový stav a potvrdí, že obraz je k dispozici.
   */
  const handleLoad = () => {
    setImgError(false);
    setImgLoaded(true);
  };

  const handleFrameMetaMessage = useCallback(
    (rawData: unknown) => {
      if (!onFrameMeta) return;

      try {
        const text =
          typeof rawData === "string"
            ? rawData
            : rawData instanceof ArrayBuffer
              ? new TextDecoder().decode(rawData)
              : "";
        if (!text) return;

        const parsed = JSON.parse(text) as Partial<FrameMetaEvent>;
        if (
          typeof parsed.frame_id === "number" &&
          Number.isFinite(parsed.frame_id) &&
          typeof parsed.timestamp === "number" &&
          Number.isFinite(parsed.timestamp)
        ) {
          onFrameMeta({ frame_id: parsed.frame_id, timestamp: parsed.timestamp });
        }
      } catch {
        // ignorujeme nevalidní payload
      }
    },
    [onFrameMeta],
  );

  useEffect(() => {
    if (!useWebRTC) return;

    const signalingBase = (webrtcUrl ?? window.location.origin).replace(/\/$/, "");
    let closed = false;
    let pc: RTCPeerConnection | null = null;

    const start = async () => {
      setWebrtcState("connecting");

      try {
        pc = new RTCPeerConnection();

        pc.ontrack = (event) => {
          const [stream] = event.streams;
          if (!stream || !videoRef.current) return;
          videoRef.current.srcObject = stream;
          void videoRef.current.play().catch(() => {
            // autoPlay může být blokováno browser politikou; stream zůstává navázaný
          });
        };

        pc.ondatachannel = (event) => {
          if (event.channel.label !== "frame-meta") return;
          event.channel.onmessage = (msgEvent) => {
            handleFrameMetaMessage(msgEvent.data);
          };
        };

        pc.onconnectionstatechange = () => {
          if (!pc || closed) return;
          if (pc.connectionState === "connected") setWebrtcState("live");
          if (
            pc.connectionState === "failed" ||
            pc.connectionState === "disconnected" ||
            pc.connectionState === "closed"
          ) {
            setWebrtcState("error");
          }
        };

        pc.addTransceiver("video", { direction: "recvonly" });

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        await waitForIceGatheringComplete(pc);

        const localDesc = pc.localDescription;
        if (!localDesc) throw new Error("Missing localDescription");

        const response = await fetch(`${signalingBase}/webrtc/offer`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sdp: localDesc.sdp, type: localDesc.type }),
        });

        if (!response.ok) {
          throw new Error(`WebRTC signaling failed (${response.status})`);
        }

        const answer = (await response.json()) as {
          sdp: string;
          type: RTCSdpType;
        };

        await pc.setRemoteDescription(answer);
      } catch {
        if (!closed) {
          setWebrtcState("error");
        }
      }
    };

    void start();

    return () => {
      closed = true;
      if (pc) {
        pc.ontrack = null;
        pc.onconnectionstatechange = null;
        pc.ondatachannel = null;
        pc.close();
      }
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
      setWebrtcState("idle");
    };
  }, [handleFrameMetaMessage, useWebRTC, webrtcUrl]);

  /**
   * isLive – true pouze tehdy, když MJPEG pipe aktivně dodává živé snímky.
   *
   * Všechny čtyři podmínky musí platit současně:
   *   isConnected          → Socket.IO je připojeno (máme stavová data)
   *   !imgError            → browser nenahlásil chybu na <img> elementu
   *   imgLoaded            → browser úspěšně přijal alespoň jeden snímek
   *   stats?.status === "streaming" → procesor hlásí stav „streaming"
   *
   * Pokud libovolná podmínka selže, stream není považován za živý a zobrazí
   * se buď no-signal nebo frozen-frame overlay.
   */
  const videoReady = useWebRTC ? webrtcState === "live" : !imgError && imgLoaded;
  const isLive = isConnected && videoReady && stats?.status === "streaming";

  /**
   * showNoSignal – true když nemáme žádný použitelný snímek k zobrazení.
   *
   * Nastane při kterékoli z těchto situací:
   *   !isConnected → Socket.IO odpojeno; server je nedostupný
   *   imgError     → browser nahlásil HTTP chybu na MJPEG endpointu
   *   !imgLoaded   → připojeno, ale browser ještě nepřijal první snímek
   *                  (čekáme na první frame → zobrazíme loading spinner)
   *
   * V tomto stavu je `<img>` element neviditelný (opacity-0), aby nezabíral
   * vizuální prostor, a celou plochu pokryje tmavý overlay.
   */
  const showNoSignal = useWebRTC
    ? !isConnected || webrtcState === "connecting" || webrtcState === "error"
    : !isConnected || imgError || !imgLoaded;

  /**
   * showFrozenOverlay – true když máme snímek, ale stream není živý.
   *
   * Jde o „mezistav": browser zobrazuje poslední přijatý JPEG frame, ale
   * procesor nepřenáší nová data (je ve stavu idle, starting, connecting, …).
   * `<img>` zůstane viditelný se sníženou opacitou a šedým filtrem
   * (opacity-40 + grayscale), aby bylo jasné, že obraz je starý.
   * Semi-transparentní overlay informuje o aktuálním stavu procesoru.
   *
   * Podmínka: máme snímek (není showNoSignal) ale nejsme živí (!isLive).
   */
  const showFrozenOverlay = !showNoSignal && !isLive;

  /** Zkrácený alias pro aktuální stav procesoru; null před prvním stats eventem. */
  const processorStatus = stats?.status ?? null;

  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden flex flex-col h-full">
      {/* ── Hlavička karty ─────────────────────────────────────────────────── */}
      {/*
        Obsahuje název sekce a connection badge indikující stav Socket.IO
        spojení a dostupnost MJPEG streamu.
        Badge má „glow shadow" efekt pomocí Tailwind arbitrary shadow:
          zelená záře  → shadow-[0_0_6px_2px_rgba(74,222,128,0.5)]
          červená záře → shadow-[0_0_6px_2px_rgba(239,68,68,0.45)]
        Tečka svítí zeleně pokud je Socket.IO připojeno A <img> nemá chybu,
        jinak červeně, aby bylo okamžitě vidět jakýkoli problém se spojením.
      */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-lg">📹</span>
          <h2 className="text-white font-semibold text-sm tracking-wide uppercase">
            Live Stream
          </h2>
        </div>

        {/* Connection badge: barevná tečka + textový popis stavu spojení */}
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2.5 h-2.5 rounded-full ${
              isConnected && !imgError
                ? "bg-green-400 shadow-[0_0_6px_2px_rgba(74,222,128,0.5)]"
                : "bg-red-500 shadow-[0_0_6px_2px_rgba(239,68,68,0.45)]"
            }`}
          />
          <span
            className={`text-xs font-medium ${
              isConnected && !imgError ? "text-green-400" : "text-red-400"
            }`}
          >
            {isConnected && !imgError ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>

      {/* ── Video plocha ───────────────────────────────────────────────────── */}
      <div className="relative flex-1 bg-gray-950 flex items-center justify-center min-h-0">
        {/*
          MJPEG <img> element – záměrně vždy mountován v DOM.

          Důvod: Browser otevírá HTTP spojení k MJPEG endpointu okamžikem
          namountování elementu a udržuje ho živé po celou dobu, dokud je
          element v DOM. Kdybychom element podmíněně renderovali (pouze když
          isConnected === true), browser by při každém připojení/odpojení
          uzavíral a znovu otevíral TCP spojení a první frame by přišel
          s výraznou latencí.

          Tří-stavová opacity podle aktuálního vizuálního stavu:
            opacity-0            → showNoSignal: obraz není k dispozici;
                                   <img> je neviditelný, ale stále v DOM
            opacity-40 grayscale → showFrozenOverlay: starý zmrzlý snímek;
                                   zešednutý a poloprůhledný jako „ghosting"
            opacity-100          → isLive: plně viditelný živý obraz
        */}
        {useWebRTC ? (
          <video
            ref={videoRef}
            autoPlay
            muted
            playsInline
            className={`w-full h-full object-contain transition-opacity duration-300 ${
              showNoSignal
                ? "opacity-0"
                : showFrozenOverlay
                  ? "opacity-40 grayscale"
                  : "opacity-100"
            }`}
          />
        ) : (
          <img
            src={streamUrl}
            alt="Live MJPEG stream"
            className={`w-full h-full object-contain transition-opacity duration-300 ${
              showNoSignal
                ? "opacity-0"
                : showFrozenOverlay
                  ? "opacity-40 grayscale"
                  : "opacity-100"
            }`}
            onLoad={handleLoad}
            onError={handleError}
          />
        )}

        {/* ── No-signal overlay (žádný použitelný snímek) ────────────────── */}
        {/*
          Zobrazí se přes celou video plochu, kdykoliv showNoSignal === true.
          Má dvě vnitřní větve podle příčiny absence signálu:

          VĚTEV „loading":
            Podmínka: isConnected && !imgError
            Situace: Socket.IO je připojeno a chyba nenastala, ale browser
                     ještě nepřijal první MJPEG frame. Typicky trvá < 1 s.
            UI: zelený točící se spinner + pulzující text „Loading stream…"

          VĚTEV „disconnected":
            Podmínka: !isConnected || imgError
            Situace: Server je nedostupný nebo <img> nahlásil HTTP chybu.
            UI:
              - Ikona přeškrtnuté kamery (SVG s diagonální čárou)
              - Text „Stream unavailable" + podtext
              - Tři animované tečky s postupným `animationDelay` (0 / 150 / 300 ms),
                které vizuálně naznačují čekání na obnovení spojení
        */}
        {showNoSignal && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-gray-950">
            {(useWebRTC ? webrtcState === "connecting" : isConnected && !imgError) ? (
              /* Větev „loading": jsme připojeni, čekáme na první snímek */
              <>
                <svg
                  className="w-10 h-10 text-green-400 animate-spin"
                  xmlns="http://www.w3.org/2000/svg"
                  fill="none"
                  viewBox="0 0 24 24"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
                  />
                </svg>
                <p className="text-gray-400 text-sm font-medium animate-pulse">
                  {useWebRTC ? "Establishing WebRTC…" : "Loading stream…"}
                </p>
              </>
            ) : (
              /* Větev „disconnected": server nedostupný nebo chyba na <img> */
              <>
                {/* Ikona přeškrtnuté kamery */}
                <div className="w-16 h-16 rounded-full bg-gray-800 border border-gray-700 flex items-center justify-center">
                  <svg
                    className="w-8 h-8 text-gray-500"
                    xmlns="http://www.w3.org/2000/svg"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M15.75 10.5l4.72-4.72a.75.75 0 011.28.53v11.38a.75.75 0 01-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 002.25-2.25v-9A2.25 2.25 0 0013.5 5.25h-9A2.25 2.25 0 002.25 7.5v9A2.25 2.25 0 004.5 18.75z"
                    />
                    {/* Diagonální čára přes ikonu kamery – vizuální „přeškrtnutí" */}
                    <line
                      x1="3"
                      y1="3"
                      x2="21"
                      y2="21"
                      stroke="currentColor"
                      strokeWidth={1.5}
                      strokeLinecap="round"
                    />
                  </svg>
                </div>

                {/* Stavový text */}
                <div className="text-center">
                  <p className="text-gray-300 text-sm font-medium">
                    Stream unavailable
                  </p>
                  <p className="text-gray-500 text-xs mt-1">
                    Waiting for vprocessor connection…
                  </p>
                </div>

                {/*
                  Animované tečky naznačující čekání.
                  Každá tečka má jiné `animationDelay` (0 / 150 / 300 ms),
                  aby se bounceovaly za sebou jako „…" efekt.
                */}
                <div className="flex gap-1.5">
                  {[0, 1, 2].map((i) => (
                    <span
                      key={i}
                      className="w-1.5 h-1.5 rounded-full bg-gray-600 animate-bounce"
                      style={{ animationDelay: `${i * 0.15}s` }}
                    />
                  ))}
                </div>
              </>
            )}
          </div>
        )}

        {/* ── Frozen-frame overlay (snímek viditelný, stream pozastaven) ─── */}
        {/*
          Zobrazí se, když showFrozenOverlay === true, tedy:
            - máme alespoň jeden snímek od browseru (imgLoaded)
            - ale stream není živý (procesor není ve stavu „streaming")

          Vizuální efekty overlaye:
            bg-gray-950/60     → poloprůhledné tmavé pozadí (60 % opacity)
            backdrop-blur-[2px] → lehké rozmazání snímku pod overlayem,
                                  aby byl starý obraz jasně odlišen od živého

          Stavová karta uprostřed zobrazuje ikonu, nadpis a popis stavu
          procesoru vyhledaná v konstantě STATUS_OVERLAY.
          Fallback hodnoty (??  operátor) chrání před hypotetickým null,
          i když TypeScript výčet garantuje přítomnost záznamu.
        */}
        {showFrozenOverlay && processorStatus && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gray-950/60 backdrop-blur-[2px]">
            {/* Stavová karta se zaoblenými rohy a shadow efektem */}
            <div className="bg-gray-900/80 border border-gray-700 rounded-xl px-6 py-5 flex flex-col items-center gap-2 shadow-xl max-w-xs text-center">
              {/* Velká stavová emoji ikona */}
              <span className="text-3xl leading-none">
                {STATUS_OVERLAY[processorStatus]?.icon ?? "⏸"}
              </span>
              {/* Nadpis stavu v barvě odpovídající závažnosti */}
              <p
                className={`text-sm font-semibold ${STATUS_OVERLAY[processorStatus]?.color ?? "text-gray-300"}`}
              >
                {STATUS_OVERLAY[processorStatus]?.title ?? processorStatus}
              </p>
              {/* Popis stavu / instrukce pro uživatele */}
              <p className="text-gray-400 text-xs">
                {STATUS_OVERLAY[processorStatus]?.sub ?? ""}
              </p>
            </div>
            {/* Poznámka pod kartou – uživatel ví, že vidí starý snímek */}
            <p className="text-gray-500 text-xs italic">
              Showing last received frame
            </p>
          </div>
        )}

        {/* ── LIVE badge – zobrazí se pouze při skutečně živém streamu ─────── */}
        {/*
          Podmínka: isLive === true
            → Socket.IO připojeno, žádná chyba, snímek načten, status = "streaming"

          UI: pill v levém horním rohu s:
            - pulsující červenou tečkou (animate-pulse) jako broadcast indikátor
            - textem „LIVE" s rozšířeným letter-spacingem (tracking-widest)
          Průhledné pozadí (black/60) s backdrop-blur zajišťuje čitelnost
          na jakémkoli pozadí videa.
        */}
        {isLive && (
          <div className="absolute top-3 left-3 flex items-center gap-1.5 bg-black/60 backdrop-blur-sm px-2.5 py-1 rounded-full border border-red-500/40">
            {/* Pulsující červená tečka – klasický broadcast indikátor */}
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            <span className="text-white text-xs font-bold tracking-widest uppercase">
              Live
            </span>
          </div>
        )}

        {/* ── Non-live status pill (levý horní roh, zmrzlý snímek) ────────── */}
        {/*
          Podmínka: showFrozenOverlay === true && processorStatus !== null
            → máme snímek, ale procesor neposílá živá data

          Zobrazí se na stejné pozici jako LIVE badge (top-3 left-3), ale
          nikdy nejsou zobrazeny oba zároveň – jsou podmíněny vzájemně
          vylučujícími se hodnotami (isLive vs. showFrozenOverlay).

          UI: pill se žlutou pulsující tečkou a názvem aktuálního stavu
          procesoru (idle / starting / connecting / reconnecting / …).
          Žlutá barva naznačuje „pozor, čekáme" bez pocitu chyby.
        */}
        {showFrozenOverlay && processorStatus && (
          <div className="absolute top-3 left-3 flex items-center gap-1.5 bg-black/60 backdrop-blur-sm px-2.5 py-1 rounded-full border border-yellow-600/50">
            {/* Pulsující žlutá tečka – indikátor přechodného stavu */}
            <span className="w-2 h-2 rounded-full bg-yellow-500 animate-pulse" />
            <span className="text-yellow-300 text-xs font-bold tracking-widest uppercase">
              {processorStatus}
            </span>
          </div>
        )}
      </div>
    </div>
  );
};

export default VideoStream;
