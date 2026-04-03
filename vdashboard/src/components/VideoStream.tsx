import React, { useState } from "react";
import type { StatsEvent } from "../types";

interface VideoStreamProps {
  streamUrl: string;
  isConnected: boolean;
  stats: StatsEvent | null;
}

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

const VideoStream: React.FC<VideoStreamProps> = ({
  streamUrl,
  isConnected,
  stats,
}) => {
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);

  const handleError = () => {
    setImgError(true);
    setImgLoaded(false);
  };

  const handleLoad = () => {
    setImgError(false);
    setImgLoaded(true);
  };

  // True only when the MJPEG pipe is actually delivering live frames.
  const isLive =
    isConnected && !imgError && imgLoaded && stats?.status === "streaming";

  // Show the full black "no signal" overlay when we have no image at all.
  const showNoSignal = !isConnected || imgError || !imgLoaded;

  // Show the semi-transparent "frozen" overlay when we have a frame but it's stale.
  const showFrozenOverlay = !showNoSignal && !isLive;

  const processorStatus = stats?.status ?? null;

  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden flex flex-col h-full">
      {/* ── Card header ───────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700 shrink-0">
        <div className="flex items-center gap-2">
          <span className="text-lg">📹</span>
          <h2 className="text-white font-semibold text-sm tracking-wide uppercase">
            Live Stream
          </h2>
        </div>

        {/* Socket.IO connection badge */}
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

      {/* ── Video area ────────────────────────────────────────────── */}
      <div className="relative flex-1 bg-gray-950 flex items-center justify-center min-h-0">
        {/* MJPEG image — always mounted so the browser keeps the pipe alive */}
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

        {/* ── No-signal overlay (no image at all) ─────────────────── */}
        {showNoSignal && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-4 bg-gray-950">
            {isConnected && !imgError ? (
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
                  Loading stream…
                </p>
              </>
            ) : (
              <>
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
                <div className="text-center">
                  <p className="text-gray-300 text-sm font-medium">
                    Stream unavailable
                  </p>
                  <p className="text-gray-500 text-xs mt-1">
                    Waiting for vprocessor connection…
                  </p>
                </div>
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

        {/* ── Frozen-frame overlay (image visible but stream is paused) ── */}
        {showFrozenOverlay && processorStatus && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-gray-950/60 backdrop-blur-[2px]">
            <div className="bg-gray-900/80 border border-gray-700 rounded-xl px-6 py-5 flex flex-col items-center gap-2 shadow-xl max-w-xs text-center">
              <span className="text-3xl leading-none">
                {STATUS_OVERLAY[processorStatus]?.icon ?? "⏸"}
              </span>
              <p
                className={`text-sm font-semibold ${STATUS_OVERLAY[processorStatus]?.color ?? "text-gray-300"}`}
              >
                {STATUS_OVERLAY[processorStatus]?.title ?? processorStatus}
              </p>
              <p className="text-gray-400 text-xs">
                {STATUS_OVERLAY[processorStatus]?.sub ?? ""}
              </p>
            </div>
            <p className="text-gray-500 text-xs italic">
              Showing last received frame
            </p>
          </div>
        )}

        {/* ── LIVE badge — only when truly streaming ────────────────── */}
        {isLive && (
          <div className="absolute top-3 left-3 flex items-center gap-1.5 bg-black/60 backdrop-blur-sm px-2.5 py-1 rounded-full border border-red-500/40">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            <span className="text-white text-xs font-bold tracking-widest uppercase">
              Live
            </span>
          </div>
        )}

        {/* ── Non-live status pill (top-left, when image present but frozen) ── */}
        {showFrozenOverlay && processorStatus && (
          <div className="absolute top-3 left-3 flex items-center gap-1.5 bg-black/60 backdrop-blur-sm px-2.5 py-1 rounded-full border border-yellow-600/50">
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
