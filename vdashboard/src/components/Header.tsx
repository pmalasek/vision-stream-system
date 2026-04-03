import { useEffect, useState } from 'react'

interface HeaderProps {
  isConnected: boolean
}

export default function Header({ isConnected }: HeaderProps) {
  const [time, setTime] = useState(() => new Date())

  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  const formattedTime = time.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })

  const formattedDate = time.toLocaleDateString([], {
    weekday: 'short',
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  })

  return (
    <header className="bg-gray-800 border-b border-gray-700 px-6 py-3 flex items-center justify-between shadow-lg">
      {/* Left — branding */}
      <div className="flex items-center gap-3">
        <span className="text-2xl" role="img" aria-label="camera">
          📹
        </span>
        <div>
          <h1 className="text-lg font-bold text-white leading-tight tracking-wide">
            Vision Stream System
          </h1>
          <p className="text-xs text-gray-400 leading-tight">
            Real-time video monitoring dashboard
          </p>
        </div>
      </div>

      {/* Right — connection status + clock */}
      <div className="flex items-center gap-6">
        {/* Connection badge */}
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-2.5 w-2.5 rounded-full ${
              isConnected ? 'bg-green-400 shadow-[0_0_6px_#4ade80]' : 'bg-red-500'
            }`}
          />
          <span
            className={`text-sm font-medium ${
              isConnected ? 'text-green-400' : 'text-red-400'
            }`}
          >
            {isConnected ? 'Connected' : 'Disconnected'}
          </span>
        </div>

        {/* Divider */}
        <div className="h-8 w-px bg-gray-700" />

        {/* Clock */}
        <div className="text-right">
          <p className="text-sm font-mono font-semibold text-white tabular-nums">
            {formattedTime}
          </p>
          <p className="text-xs text-gray-400">{formattedDate}</p>
        </div>
      </div>
    </header>
  )
}
