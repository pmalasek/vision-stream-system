/**
 * @file StatsPanel.tsx
 * @description Panel statistik pro Vision Stream System.
 *
 * Renderuje mřížku metrik (FPS, snímky, detekce, uptime) a stavový odznak
 * streamu. Pokud ještě nedorazila první SSE událost se statistikami,
 * zobrazí tzv. skeleton placeholdery – animované šedé bloky, které
 * vizuálně naznačují budoucí obsah a zabraňují "skákání" layoutu.
 */

import { StatsEvent } from "../types";

// ---------------------------------------------------------------------------
// Rozhraní
// ---------------------------------------------------------------------------

/**
 * Props hlavní komponenty `StatsPanel`.
 */
interface StatsPanelProps {
  /** Poslední přijatá SSE událost se statistikami, nebo `null` dokud nepřijde první zpráva. */
  stats: StatsEvent | null;
  /** Indikuje, zda je SSE připojení k backendu aktuálně aktivní. */
  isConnected: boolean;
}

/**
 * Props jedné metrické karty `MetricCard`.
 */
interface MetricCardProps {
  /** Popisný štítek karty zobrazený malým písmem nahoře (např. "FPS"). */
  label: string;
  /** Hlavní hodnota karty – může být číslo nebo již naformátovaný řetězec. */
  value: string | number;
  /** Volitelný doplňující popisek zobrazený pod hodnotou (např. "frames / second"). */
  sub?: string;
  /**
   * Příznak zvýraznění hodnoty zelenou barvou (`text-green-400`).
   * Používá se pro klíčové metriky jako FPS.
   * Výchozí hodnota je `false` – hodnota se pak zobrazí bílou barvou.
   */
  accent?: boolean;
}

// ---------------------------------------------------------------------------
// Pomocné komponenty
// ---------------------------------------------------------------------------

/**
 * `MetricCard` – jedna dlaždice s metrikou v mřížce statistik.
 *
 * Zobrazuje štítek, hlavní hodnotu a nepovinný popisek.
 * Prop `accent` přepíná barvu hodnoty mezi bílou a zelenou,
 * čímž lze vizuálně zvýraznit nejdůležitější metriku (FPS).
 */
function MetricCard({ label, value, sub, accent = false }: MetricCardProps) {
  return (
    <div className="bg-gray-700 rounded-lg p-4 flex flex-col gap-1 border border-gray-600">
      {/* Štítek metriky – záměrně malý, verzálky, pro vizuální hierarchii */}
      <span className="text-gray-400 text-xs font-medium uppercase tracking-wider">
        {label}
      </span>

      {/*
       * Hlavní hodnota metriky.
       *
       * `tabular-nums` (Tailwind: font-variant-numeric: tabular-nums) zajišťuje,
       * aby všechny číslice měly stejnou šířku. Díky tomu se čísla při
       * každé aktualizaci neposouvají ani nemění šířku prvku – důležité
       * zejména pro živě se měnící hodnoty jako FPS nebo uptime.
       *
       * `accent` přepíná barvu: zelená pro zvýrazněnou metriku, bílá jinak.
       */}
      <span
        className={`text-2xl font-bold tabular-nums leading-tight ${
          accent ? "text-green-400" : "text-white"
        }`}
      >
        {value}
      </span>

      {/* Doplňující popisek (pod-label) – renderuje se pouze pokud je předán */}
      {sub && <span className="text-gray-500 text-xs">{sub}</span>}
    </div>
  );
}

/**
 * `StatusBadge` – barevný odznak aktuálního stavu streamu.
 *
 * Každý možný stav (`StatsEvent["status"]`) má v lookup tabulce `config`
 * přiřazenou sadu Tailwind tříd pro pozadí, tečku a text.
 * Použití `Record<StatsEvent["status"], ...>` zaručuje, že TypeScript
 * ohlásí chybu, pokud by byl přidán nový stav do union typu, ale
 * nebyl by přidán do tabulky – jde o vyčerpávající mapování (exhaustive map).
 */
function StatusBadge({ status }: { status: StatsEvent["status"] }) {
  /**
   * Lookup tabulka: stav → vizuální konfigurace.
   *
   * Typ `Record<StatsEvent["status"], {...}>` zajišťuje, že jsou pokryty
   * VŠECHNY hodnoty union typu `status`. Pokud by byl do backendu přidán
   * nový stav, TypeScript zde vyhodí chybu kompilace.
   */
  const config: Record<
    StatsEvent["status"],
    { bg: string; dot: string; label: string; textColor: string }
  > = {
    // Aktivní přenos – zelená, pulzující tečka
    streaming: {
      bg: "bg-green-900/40 border-green-700",
      dot: "bg-green-400 animate-pulse",
      label: "Streaming",
      textColor: "text-green-300",
    },
    // Navazování připojení – žlutá, pulzující tečka
    connecting: {
      bg: "bg-yellow-900/40 border-yellow-700",
      dot: "bg-yellow-400 animate-pulse",
      label: "Connecting",
      textColor: "text-yellow-300",
    },
    // Opětovné připojování po výpadku – stejná vizuální sada jako connecting
    reconnecting: {
      bg: "bg-yellow-900/40 border-yellow-700",
      dot: "bg-yellow-400 animate-pulse",
      label: "Reconnecting",
      textColor: "text-yellow-300",
    },
    // Inicializace systému – modrá, pulzující tečka
    starting: {
      bg: "bg-blue-900/40 border-blue-700",
      dot: "bg-blue-400 animate-pulse",
      label: "Starting",
      textColor: "text-blue-300",
    },
    // Nečinný stav – šedá, statická tečka (bez pulzování)
    idle: {
      bg: "bg-gray-700/60 border-gray-600",
      dot: "bg-gray-400",
      label: "Idle",
      textColor: "text-gray-300",
    },
    // Stream byl zastaven – červená, statická tečka
    stopped: {
      bg: "bg-red-900/40 border-red-700",
      dot: "bg-red-500",
      label: "Stopped",
      textColor: "text-red-400",
    },
    // Chybový stav – sytě červená, pulzující tečka (upozornění)
    error: {
      bg: "bg-red-900/60 border-red-600",
      dot: "bg-red-400 animate-pulse",
      label: "Error",
      textColor: "text-red-300",
    },
  };

  /**
   * Bezpečný fallback pro případ, že backend pošle stav, který není
   * v union typu `StatsEvent["status"]` – např. po aktualizaci API.
   * Operátor `??` zajistí, že odznak bude vždy vykreslen, i když
   * tabulka `config` daný klíč neobsahuje.
   */
  const { bg, dot, label, textColor } = config[status] ?? {
    bg: "bg-gray-700/60 border-gray-600",
    dot: "bg-gray-400",
    label: status, // zobrazíme surový řetězec stavu jako nouzový popisek
    textColor: "text-gray-300",
  };

  return (
    <span
      className={`inline-flex items-center gap-2 px-3 py-1 rounded-full border text-sm font-medium ${bg}`}
    >
      {/* Barevná indikační tečka – u aktivních stavů pulzuje pomocí animate-pulse */}
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      {/* Textový popisek stavu s odpovídající barvou */}
      <span className={textColor}>{label}</span>
    </span>
  );
}

/**
 * Formátuje dobu běhu (uptime) ze sekund do čitelného řetězce.
 *
 * Pravidla formátování:
 * - méně než 60 s → zobrazí jen sekundy, např. `"42s"`
 * - 60 s až 3599 s → zobrazí minuty a sekundy, např. `"5m 30s"`
 * - 3600 s a více → zobrazí hodiny, minuty a sekundy, např. `"1h 2m 15s"`
 *
 * @param seconds - Celkový počet sekund od spuštění streamu.
 * @returns Naformátovaný řetězec s dobou běhu.
 */
function formatUptime(seconds: number): string {
  // Krátká doba běhu – zobrazíme pouze sekundy, hodiny ani minuty by nedávaly smysl
  if (seconds < 60) return `${Math.floor(seconds)}s`;

  // Rozložení na složky: celé hodiny, zbývající minuty, zbývající sekundy
  const h = Math.floor(seconds / 3600); // celé hodiny
  const m = Math.floor((seconds % 3600) / 60); // minuty po odečtení hodin
  const s = Math.floor(seconds % 60); // sekundy po odečtení hodin i minut

  // Pokud uplynula alespoň 1 hodina, zahrneme hodiny do výpisu
  if (h > 0) return `${h}h ${m}m ${s}s`;

  // Jinak stačí minuty a sekundy
  return `${m}m ${s}s`;
}

// ---------------------------------------------------------------------------
// Hlavní komponenta
// ---------------------------------------------------------------------------

/**
 * `StatsPanel` – panel statistik umístěný v bočním sloupci dashboardu.
 *
 * Chování podle stavu dat:
 * - `stats !== null` → zobrazí skutečné metriky v mřížce 2×2 a stavový odznak.
 * - `stats === null` → zobrazí skeleton placeholdery (4 animované karty)
 *   a záhlaví s textem „Waiting for data…" nebo „Disconnected".
 *
 * Skeleton placeholdery (`animate-pulse`) jsou záměrně tvarově shodné
 * s budoucími kartami – zabraňují skokovému překreslení layoutu (CLS)
 * a dávají uživateli vizuální zpětnou vazbu, že data se načítají.
 */
export default function StatsPanel({ stats, isConnected }: StatsPanelProps) {
  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 p-4 flex flex-col gap-4">
      {/* ------------------------------------------------------------------ */}
      {/* Záhlaví panelu: název sekce + stavový odznak / čekací hlášení       */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex items-center justify-between">
        <h2 className="text-white font-semibold text-base flex items-center gap-2">
          <span>📊</span>
          <span>Statistics</span>
        </h2>

        {/*
         * Pokud máme data, zobrazíme plnohodnotný `StatusBadge` s barvou
         * odpovídající aktuálnímu stavu streamu.
         * Jinak zobrazíme neutrální odznak se stavovým textem:
         *   - „Waiting for data…" – připojeni, čekáme na první SSE zprávu
         *   - „Disconnected"      – SSE kanál není aktivní
         */}
        {stats ? (
          <StatusBadge status={stats.status} />
        ) : (
          <span className="inline-flex items-center gap-2 px-3 py-1 rounded-full border border-gray-600 bg-gray-700 text-sm font-medium text-gray-400">
            <span className="w-2 h-2 rounded-full bg-gray-500" />
            {isConnected ? "Waiting for data…" : "Disconnected"}
          </span>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Mřížka metrik nebo skeleton placeholdery                           */}
      {/* ------------------------------------------------------------------ */}
      {stats ? (
        /*
         * Reálná data – mřížka 2×2 s kartami MetricCard.
         * Prop `accent` je předán pouze kartě FPS, aby ji vizuálně odlišil
         * jako nejdůležitější metriku.
         */
        <div className="grid grid-cols-2 gap-3">
          {/* Snímky za sekundu – zvýrazněno zelenou barvou */}
          <MetricCard
            label="FPS"
            value={stats.fps.toFixed(1)}
            sub="frames / second"
            accent
          />
          {/* Celkový počet zpracovaných snímků – lokalizovaný formát čísla */}
          <MetricCard
            label="Total Frames"
            value={stats.total_frames.toLocaleString()}
            sub="processed"
          />
          {/* Celkový počet detekcí osob */}
          <MetricCard
            label="Total Detections"
            value={stats.total_detections.toLocaleString()}
            sub="persons detected"
          />
          {/* Doba běhu streamu od spuštění, formátovaná funkcí formatUptime */}
          <MetricCard
            label="Uptime"
            value={formatUptime(stats.uptime_seconds)}
            sub="since start"
          />
        </div>
      ) : (
        /*
         * Skeleton placeholdery – zobrazují se dokud nedorazí první data.
         *
         * Každý placeholder je tvarově shodný s MetricCard (stejný padding,
         * border-radius, výška). Tři šedé pruhy uvnitř napodobují štítek,
         * hodnotu a pod-label. Třída `animate-pulse` přidává sinusoidální
         * animaci opacity (Tailwind: opacity 1→0.5→1), která signalizuje
         * načítání bez rušivého spinneru.
         *
         * `Array.from({ length: 4 })` generuje pole 4 prvků bez nutnosti
         * předdefinovaných dat – klíče `i` jsou dostačující, protože
         * pořadí ani obsah placeholderů se nikdy nemění.
         */
        <div className="grid grid-cols-2 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="bg-gray-700 rounded-lg p-4 border border-gray-600 flex flex-col gap-2"
            >
              {/* Placeholder pro štítek (label) */}
              <div className="h-3 w-16 bg-gray-600 rounded animate-pulse" />
              {/* Placeholder pro hlavní hodnotu (value) */}
              <div className="h-7 w-24 bg-gray-600 rounded animate-pulse" />
              {/* Placeholder pro doplňující popisek (sub) – lehce průhledný */}
              <div className="h-2.5 w-20 bg-gray-600/60 rounded animate-pulse" />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
