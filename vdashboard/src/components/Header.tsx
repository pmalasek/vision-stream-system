/**
 * Modul: Header
 *
 * Horní lišta aplikace Vision Stream System.
 * Zobrazuje název aplikace (branding), indikátor stavu připojení k WebSocket
 * serveru a živé hodiny aktualizované každou sekundu.
 */

import { useEffect, useState } from "react";

/**
 * Props komponenty Header.
 *
 * @property isConnected - Příznak aktuálního stavu WebSocket připojení.
 *   `true`  → server je dostupný a stream běží,
 *   `false` → připojení bylo přerušeno nebo ještě nebylo navázáno.
 */
interface HeaderProps {
  isConnected: boolean;
}

/**
 * Komponenta Header
 *
 * Renderuje pevnou horní lištu dashboardu rozdělenou na dvě části:
 *  - **Levá část** – logo s emoji kamerou a název/podtitulek aplikace.
 *  - **Pravá část** – barevný badge stavu připojení + analogové/digitální hodiny.
 *
 * Hodiny jsou udržovány interním stavem `time`, který je každou sekundu
 * aktualizován pomocí `setInterval`. Interval je po odmontování komponenty
 * bezpečně vyčištěn, aby nedocházelo k memory leakům.
 */
export default function Header({ isConnected }: HeaderProps) {
  /**
   * Aktuální čas uložený ve stavu komponenty.
   *
   * Lazy inicializace `() => new Date()` zajišťuje, že `new Date()` se zavolá
   * pouze jednou při prvním renderu – nikoli při každém re-renderu, jak by
   * se stalo u `useState(new Date())`.
   */
  const [time, setTime] = useState(() => new Date());

  /**
   * Efekt spouštějící periodickou aktualizaci času.
   *
   * `setInterval` s intervalem 1 000 ms (1 sekunda) zaručuje, že zobrazené
   * hodiny tikají synchronně s reálným časem bez zbytečného zatížení CPU.
   *
   * Funkce vrácená z efektu (`cleanup`) volá `clearInterval(id)` a tím
   * zastaví interval ve chvíli, kdy je komponenta odmontována z DOM –
   * bez toho by interval dál běžel na pozadí a způsoboval memory leak.
   *
   * Prázdné pole závislostí `[]` znamená, že se efekt spustí pouze jednou
   * po prvním připojení komponenty do DOM.
   */
  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  /**
   * Naformátovaný čas ve formátu HH:MM:SS (podle lokálního nastavení prohlížeče).
   *
   * `toLocaleTimeString` s explicitními možnostmi `hour`, `minute`, `second`
   * zajistí konzistentní 2-místný výstup napříč různými lokalitami – např.
   * vždy „09:05:03", nikdy „9:5:3".
   */
  const formattedTime = time.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  /**
   * Naformátované datum včetně zkráceného názvu dne a měsíce
   * (např. „Po 9. čvn 2025") podle lokálního nastavení prohlížeče.
   *
   * `toLocaleDateString` s volbami `weekday`, `year`, `month`, `day`
   * generuje lidsky čitelný řetězec přizpůsobený jazyku uživatele.
   */
  const formattedDate = time.toLocaleDateString([], {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
  });

  return (
    <header className="bg-gray-800 border-b border-gray-700 px-6 py-3 flex items-center justify-between shadow-lg">
      {/* ── Levá část: branding ────────────────────────────────────────────
          Obsahuje emoji ikonu kamery (s ARIA popiskem pro screen readery)
          a dvouřádkový blok s názvem aplikace a krátkým podtitulkem.    */}
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

      {/* ── Pravá část: stav připojení + hodiny ────────────────────────── */}
      <div className="flex items-center gap-6">
        {/* Connection badge – indikátor stavu WebSocket připojení.
            Tečka (rounded-full span) mění barvu podle prop `isConnected`:
              • připojeno  → zelená tečka + zelený „glow" efekt dosažený
                             pomocí Tailwind arbitrary shadow utility
                             `shadow-[0_0_6px_#4ade80]`, která simuluje
                             světelné záření a vizuálně posiluje signál „OK".
              • odpojeno   → červená tečka bez glowu.
            Textový štítek vedle tečky odpovídajícím způsobem mění barvu. */}
        <div className="flex items-center gap-2">
          <span
            className={`inline-block h-2.5 w-2.5 rounded-full ${
              isConnected
                ? "bg-green-400 shadow-[0_0_6px_#4ade80]"
                : "bg-red-500"
            }`}
          />
          <span
            className={`text-sm font-medium ${
              isConnected ? "text-green-400" : "text-red-400"
            }`}
          >
            {isConnected ? "Connected" : "Disconnected"}
          </span>
        </div>

        {/* Oddělovač – tenká svislá čára (w-px) vizuálně odděluje
            connection badge od hodin, aby byly sekce zřetelně odděleny. */}
        <div className="h-8 w-px bg-gray-700" />

        {/* Hodiny – zobrazují aktuální čas a datum.
            Třída `tabular-nums` (font-variant-numeric: tabular-nums)
            zajišťuje, že všechny číslice mají stejnou šířku – díky tomu
            se čas „netřese" (nemění šířku layoutu) při každém tiknutí,
            protože např. „1" je normálně užší než „8". */}
        <div className="text-right">
          <p className="text-sm font-mono font-semibold text-white tabular-nums">
            {formattedTime}
          </p>
          <p className="text-xs text-gray-400">{formattedDate}</p>
        </div>
      </div>
    </header>
  );
}
