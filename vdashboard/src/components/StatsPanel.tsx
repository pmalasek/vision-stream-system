import { StatsEvent } from "../types";

interface StatsPanelProps {
  stats: StatsEvent | null;
  isConnected: boolean;
}

interface MetricCardProps {
  label: string;
  value: string | number;
  sub?: string;
  accent?: boolean;
}

function MetricCard({ label, value, sub, accent = false }: MetricCardProps) {
  return (
    <div className="bg-gray-700 rounded-lg p-4 flex flex-col gap-1 border border-gray-600">
      <span className="text-gray-400 text-xs font-medium uppercase tracking-wider">
        {label}
      </span>
      <span
        className={`text-2xl font-bold tabular-nums leading-tight ${
          accent ? "text-green-400" : "text-white"
        }`}
      >
        {value}
      </span>
      {sub && <span className="text-gray-500 text-xs">{sub}</span>}
    </div>
  );
}

function StatusBadge({ status }: { status: StatsEvent["status"] }) {
  const config: Record<
    StatsEvent["status"],
    { bg: string; dot: string; label: string; textColor: string }
  > = {
    streaming: {
      bg: "bg-green-900/40 border-green-700",
      dot: "bg-green-400 animate-pulse",
      label: "Streaming",
      textColor: "text-green-300",
    },
    connecting: {
      bg: "bg-yellow-900/40 border-yellow-700",
      dot: "bg-yellow-400 animate-pulse",
      label: "Connecting",
      textColor: "text-yellow-300",
    },
    reconnecting: {
      bg: "bg-yellow-900/40 border-yellow-700",
      dot: "bg-yellow-400 animate-pulse",
      label: "Reconnecting",
      textColor: "text-yellow-300",
    },
    starting: {
      bg: "bg-blue-900/40 border-blue-700",
      dot: "bg-blue-400 animate-pulse",
      label: "Starting",
      textColor: "text-blue-300",
    },
    idle: {
      bg: "bg-gray-700/60 border-gray-600",
      dot: "bg-gray-400",
      label: "Idle",
      textColor: "text-gray-300",
    },
    stopped: {
      bg: "bg-red-900/40 border-red-700",
      dot: "bg-red-500",
      label: "Stopped",
      textColor: "text-red-400",
    },
    error: {
      bg: "bg-red-900/60 border-red-600",
      dot: "bg-red-400 animate-pulse",
      label: "Error",
      textColor: "text-red-300",
    },
  };

  const { bg, dot, label, textColor } = config[status] ?? {
    bg: "bg-gray-700/60 border-gray-600",
    dot: "bg-gray-400",
    label: status,
    textColor: "text-gray-300",
  };

  return (
    <span
      className={`inline-flex items-center gap-2 px-3 py-1 rounded-full border text-sm font-medium ${bg}`}
    >
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      <span className={textColor}>{label}</span>
    </span>
  );
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m ${s}s`;
  return `${m}m ${s}s`;
}

export default function StatsPanel({ stats, isConnected }: StatsPanelProps) {
  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 p-4 flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-white font-semibold text-base flex items-center gap-2">
          <span>📊</span>
          <span>Statistics</span>
        </h2>
        {stats ? (
          <StatusBadge status={stats.status} />
        ) : (
          <span className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-gray-600 bg-gray-700 text-sm font-medium text-gray-400">
            <span className="w-2 h-2 rounded-full bg-gray-500" />
            {isConnected ? "Waiting for data…" : "Disconnected"}
          </span>
        )}
      </div>

      {/* Metric grid */}
      {stats ? (
        <div className="grid grid-cols-2 gap-3">
          <MetricCard
            label="FPS"
            value={stats.fps.toFixed(1)}
            sub="frames / second"
            accent
          />
          <MetricCard
            label="Total Frames"
            value={stats.total_frames.toLocaleString()}
            sub="processed"
          />
          <MetricCard
            label="Total Detections"
            value={stats.total_detections.toLocaleString()}
            sub="persons detected"
          />
          <MetricCard
            label="Uptime"
            value={formatUptime(stats.uptime_seconds)}
            sub="since start"
          />
        </div>
      ) : (
        /* Skeleton placeholders while waiting for first stats event */
        <div className="grid grid-cols-2 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="bg-gray-700 rounded-lg p-4 border border-gray-600 flex flex-col gap-2"
            >
              <div className="h-3 w-16 bg-gray-600 rounded animate-pulse" />
              <div className="h-7 w-24 bg-gray-600 rounded animate-pulse" />
              <div className="h-2.5 w-20 bg-gray-600/60 rounded animate-pulse" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
