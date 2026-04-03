import Header from "./components/Header";
import VideoStream from "./components/VideoStream";
import StatsPanel from "./components/StatsPanel";
import DetectionPanel from "./components/DetectionPanel";
import { useSocket } from "./hooks/useSocket";

const PROCESSOR_BASE =
  (import.meta.env.VITE_PROCESSOR_URL as string | undefined) ??
  "http://localhost:8000";

const STREAM_URL = `${PROCESSOR_BASE}/stream`;

export default function App() {
  const { latestDetection, stats, isConnected } = useSocket();

  return (
    <div className="min-h-screen bg-gray-900 text-white flex flex-col">
      <Header isConnected={isConnected} />

      <main className="flex-1 p-4 lg:p-6">
        <div className="max-w-screen-2xl mx-auto grid grid-cols-1 lg:grid-cols-3 gap-4 lg:gap-6 h-full">
          {/* ── Left column: video stream (takes 2/3 on large screens) ── */}
          <div className="lg:col-span-2 flex flex-col gap-4">
            <VideoStream
              streamUrl={STREAM_URL}
              isConnected={isConnected}
              stats={stats}
            />
          </div>

          {/* ── Right column: stats + detections ── */}
          <div className="flex flex-col gap-4">
            <StatsPanel stats={stats} isConnected={isConnected} />
            <DetectionPanel detection={latestDetection} />
          </div>
        </div>
      </main>

      <footer className="text-center text-gray-600 text-xs py-3 border-t border-gray-800">
        Vision Stream System &mdash; vdashboard
      </footer>
    </div>
  );
}
