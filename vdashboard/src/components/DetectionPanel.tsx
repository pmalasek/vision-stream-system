import { useEffect, useRef, useState } from "react";
import type { DetectionEvent } from "../types";

interface Props {
  detection: DetectionEvent | null;
}

const MAX_HISTORY = 10;

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const color =
    pct >= 80 ? "bg-green-400" : pct >= 50 ? "bg-yellow-400" : "bg-red-400";

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs text-gray-400 w-8 text-right">{pct}%</span>
    </div>
  );
}

export default function DetectionPanel({ detection }: Props) {
  const [history, setHistory] = useState<DetectionEvent[]>([]);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!detection) return;
    setHistory((prev) => {
      const next = [detection, ...prev];
      return next.slice(0, MAX_HISTORY);
    });
  }, [detection]);

  // Auto-scroll list to top when new detection arrives
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = 0;
    }
  }, [history]);

  const personCount = detection?.person_count ?? 0;
  const timestamp = detection?.timestamp
    ? formatTimestamp(detection.timestamp)
    : null;

  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 p-5 flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest">
          Detections
        </h2>
        {history.length > 0 && (
          <span className="text-xs text-gray-500">
            last {history.length} event{history.length !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* Person count hero */}
      <div className="flex flex-col items-center gap-1 py-2">
        <span
          className={`text-7xl font-extrabold tabular-nums transition-colors duration-300 ${
            personCount > 0 ? "text-green-400" : "text-gray-600"
          }`}
        >
          {personCount}
        </span>
        <span className="text-sm text-gray-400 font-medium">
          {personCount === 1 ? "person detected" : "persons detected"}
        </span>
        {timestamp && (
          <span className="text-xs text-gray-500 mt-1">
            last seen at {timestamp}
          </span>
        )}
      </div>

      {/* Current detections breakdown */}
      {detection && detection.detections.length > 0 && (
        <div className="bg-gray-700/50 rounded-lg p-3 flex flex-col gap-2">
          <p className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-1">
            Frame #{detection.frame_id} &mdash; confidence
          </p>
          <div className="flex flex-col gap-2 max-h-48 overflow-y-auto pr-1">
            {detection.detections.map((det, idx) => (
              <div key={idx} className="flex flex-col gap-0.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-300">
                    Person {idx + 1}
                  </span>
                  <span className="text-xs text-gray-500">
                    [{det.x1}, {det.y1}] → [{det.x2}, {det.y2}]
                  </span>
                </div>
                <ConfidenceBar confidence={det.confidence} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Divider */}
      {history.length > 0 && <div className="border-t border-gray-700" />}

      {/* History list */}
      {history.length > 0 ? (
        <div>
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-2">
            Recent events
          </p>
          <div
            ref={listRef}
            className="flex flex-col gap-2 max-h-52 overflow-y-auto pr-1
                       scrollbar-thin scrollbar-thumb-gray-600 scrollbar-track-transparent"
          >
            {history.map((evt, idx) => (
              <div
                key={`${evt.frame_id}-${idx}`}
                className={`flex items-center justify-between rounded-lg px-3 py-2
                            border transition-colors duration-200
                            ${
                              idx === 0
                                ? "bg-gray-700 border-gray-600"
                                : "bg-gray-800 border-gray-700/50"
                            }`}
              >
                <div className="flex items-center gap-2">
                  {/* Person count bubble */}
                  <span
                    className={`inline-flex items-center justify-center w-6 h-6 rounded-full text-xs font-bold
                                ${
                                  evt.person_count > 0
                                    ? "bg-green-400/20 text-green-400"
                                    : "bg-gray-700 text-gray-500"
                                }`}
                  >
                    {evt.person_count}
                  </span>
                  <div className="flex flex-col">
                    <span className="text-xs text-gray-300 font-medium">
                      {evt.person_count === 1
                        ? "1 person"
                        : `${evt.person_count} persons`}
                    </span>
                    <span className="text-xs text-gray-600">
                      frame #{evt.frame_id}
                    </span>
                  </div>
                </div>
                <span className="text-xs text-gray-500">
                  {formatTimestamp(evt.timestamp)}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-6 gap-2 text-gray-600">
          <span className="text-3xl">🔍</span>
          <span className="text-sm">Waiting for detections…</span>
        </div>
      )}
    </div>
  );
}
