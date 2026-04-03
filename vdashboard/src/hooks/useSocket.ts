import { useEffect, useState } from "react";
import { io, Socket } from "socket.io-client";
import type { DetectionEvent, StatsEvent } from "../types";

interface UseSocketReturn {
  latestDetection: DetectionEvent | null;
  stats: StatsEvent | null;
  isConnected: boolean;
}

// ---------------------------------------------------------------------------
// Module-level singleton
// ---------------------------------------------------------------------------
// The socket is created once per page load, outside the React component tree.
// This is intentional: React 18 StrictMode double-invokes every useEffect
// (mount → cleanup → mount) in development to help surface side-effect bugs.
// If we created the socket *inside* the effect and called socket.disconnect()
// in the cleanup, StrictMode's first-run cleanup would disconnect a socket
// that is still needed — producing the spurious "connected (total=1) →
// disconnected (total=0)" pair visible in the server logs.
//
// By lifting the socket here:
//   • The cleanup only removes event listeners (no disconnect).
//   • On the second (real) mount the same, already-connected socket is reused
//     and listeners are reattached.
//   • In production (no double-invoke) the behaviour is identical.
// ---------------------------------------------------------------------------

let _socket: Socket | null = null;

function getSocket(): Socket {
  if (_socket) return _socket;

  const url =
    (import.meta.env.VITE_PROCESSOR_URL as string | undefined) ??
    window.location.origin;

  _socket = io(url, {
    path: "/socket.io",
    transports: ["websocket", "polling"],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1_000,
    reconnectionDelayMax: 5_000,
    timeout: 10_000,
  });

  return _socket;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useSocket(): UseSocketReturn {
  const [isConnected, setIsConnected] = useState(false);
  const [latestDetection, setLatestDetection] = useState<DetectionEvent | null>(
    null,
  );
  const [stats, setStats] = useState<StatsEvent | null>(null);

  useEffect(() => {
    const socket = getSocket();

    // Named handler references so we can remove them precisely in cleanup
    // without accidentally stripping listeners registered by other code.
    const onConnect = () => setIsConnected(true);
    const onDisconnect = () => setIsConnected(false);
    const onConnectError = () => setIsConnected(false);
    const onDetection = (data: DetectionEvent) => setLatestDetection(data);
    const onStats = (data: StatsEvent) => setStats(data);

    socket.on("connect", onConnect);
    socket.on("disconnect", onDisconnect);
    socket.on("connect_error", onConnectError);
    socket.on("detection", onDetection);
    socket.on("stats", onStats);

    // If the socket was already connected before this effect ran (e.g. on the
    // StrictMode second mount, or a fast-refresh), sync state immediately so
    // the UI doesn't flash a stale "disconnected" indicator.
    if (socket.connected) {
      setIsConnected(true);
    }

    return () => {
      // Remove only the listeners registered above.  Do NOT call
      // socket.disconnect() here — the singleton must stay alive across
      // StrictMode's cleanup/remount cycle and across future fast-refreshes.
      socket.off("connect", onConnect);
      socket.off("disconnect", onDisconnect);
      socket.off("connect_error", onConnectError);
      socket.off("detection", onDetection);
      socket.off("stats", onStats);
    };
  }, []);

  return { latestDetection, stats, isConnected };
}
