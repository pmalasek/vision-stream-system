/**
 * @module DetectionPanel
 *
 * Komponenta postranního panelu zobrazující výsledky detekce osob v reálném čase.
 *
 * Zodpovědnosti:
 *  - Zobrazení aktuálního počtu detekovaných osob formou velkého "hero" čísla.
 *  - Rozpad aktuálního snímku (bounding-boxy + confidence skóre pro každou osobu).
 *  - Udržování posuvné historie posledních MAX_HISTORY událostí, aby uživatel
 *    viděl trend detekce i bez nutnosti procházet záznamy ručně.
 *  - Automatický scroll na vrchol seznamu při příchodu nové události.
 */

import { useEffect, useRef, useState } from "react";
import type { DetectionEvent } from "../types";

/**
 * Props komponenty DetectionPanel.
 *
 * @property detection - Poslední přijatá detekční událost ze streamu, nebo `null`
 *                       pokud ještě žádná událost nedorazila (iniciální stav).
 */
interface Props {
  detection: DetectionEvent | null;
}

/**
 * Maximální počet událostí uchovávaných v lokální historii.
 *
 * Proč limit? Každá položka historie je celý objekt `DetectionEvent` včetně
 * pole `detections` s bounding-boxy. Při vysokém FPS streamu by neomezená
 * historie rychle zaplnila paměť a zpomalila překreslování Reactu (diffing
 * velkého pole stavů). Hodnota 10 je kompromis mezi přehledností UI a
 * rozumnou spotřebou paměti — v praxi odpovídá zhruba 10 sekundám při 1 fps.
 */
const MAX_HISTORY = 10;

/**
 * Převede ISO 8601 timestamp na lidsky čitelný formát HH:MM:SS v lokálním čase.
 *
 * Zobrazujeme **pouze čas** (bez data), protože detekce probíhají v reálném čase
 * a uživatel implicitně ví, že jde o dnešní datum — zobrazení data by zabíralo
 * cenné místo v úzkém panelu.
 *
 * @param iso - Řetězec ve formátu ISO 8601 (např. "2024-05-01T14:32:07.123Z").
 * @returns    Naformátovaný čas jako řetězec, např. "14:32:07".
 *             Pokud `Date` konstruktor nebo `toLocaleTimeString` selže
 *             (neplatný vstup), vrátí se původní `iso` string jako fallback,
 *             aby UI nezobrazilo prázdný nebo chybový řetězec.
 */
function formatTimestamp(ts: string | number): string {
  try {
    const raw = typeof ts === "string" ? ts.trim() : ts;
    const numeric =
      typeof raw === "number"
        ? raw
        : raw !== "" && Number.isFinite(Number(raw))
          ? Number(raw)
          : null;

    const date =
      numeric !== null
        ? // Heuristika: hodnoty < 1e12 bereme jako sekundy, jinak ms.
          new Date(numeric < 1_000_000_000_000 ? numeric * 1000 : numeric)
        : new Date(ts);

    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    // Fallback: vrátíme surový string, aby komponenta nespadla a něco zobrazila.
    return String(ts);
  }
}

/**
 * Vizuální procentuální pruh (progress bar) zobrazující míru jistoty detekce.
 *
 * Barevné pásmo vychází z obecně přijímaných prahů pro detekci objektů:
 *  - **≥ 80 %** → zelená  (`bg-green-400`): vysoká jistota, detekce je spolehlivá.
 *  - **≥ 50 %** → žlutá   (`bg-yellow-400`): střední jistota, může jít o falešnou detekci.
 *  - **< 50 %**  → červená (`bg-red-400`): nízká jistota, výsledek je nespolehlivý.
 *
 * @param confidence - Desetinné číslo v rozsahu [0, 1] reprezentující skóre modelu.
 */
function ConfidenceBar({ confidence }: { confidence: number }) {
  // Převedeme desetinné skóre [0, 1] na celé procento [0, 100] pro použití
  // v CSS vlastnosti `width` a v textovém popisku.
  const pct = Math.round(confidence * 100);

  // Výběr barvy podle prahových hodnot (ternární řetěz = čitelný "if-else if-else").
  const color =
    pct >= 80 ? "bg-green-400" : pct >= 50 ? "bg-yellow-400" : "bg-red-400";

  return (
    <div className="flex items-center gap-2">
      {/* Šedý podkladový pruh — tvoří "prázdnou" část baru */}
      <div className="flex-1 h-1.5 bg-gray-700 rounded-full overflow-hidden">
        {/*
         * Barevná výplň pruhu; šířka je řízena inline stylem místo Tailwind třídy,
         * protože Tailwind purge nedokáže generovat dynamické hodnoty jako `w-[73%]`.
         *
         * `transition-all duration-300` zajišťuje plynulou animaci při každé změně
         * hodnoty confidence mezi snímky — bez toho by pruh "skákal" bez přechodu.
         */}
        <div
          className={`h-full rounded-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {/* Číselný popisek vpravo; pevná šířka `w-8` brání skákání layoutu */}
      <span className="text-xs text-gray-400 w-8 text-right">{pct}%</span>
    </div>
  );
}

/**
 * Hlavní komponenta panelu detekce.
 *
 * Přijímá nejnovější `DetectionEvent` jako prop a interně si udržuje posuvnou
 * historii posledních {@link MAX_HISTORY} událostí. Panel se skládá ze čtyř
 * vizuálních sekcí:
 *  1. **Hlavička** — nadpis + počet uchovaných událostí.
 *  2. **Hero číslo** — výrazný počet aktuálně detekovaných osob.
 *  3. **Breakdown snímku** — tabulka bounding-boxů a confidence barů.
 *  4. **Historie** — scrollovatelný seznam nedávných událostí.
 *
 * @param props - Viz {@link Props}.
 */
export default function DetectionPanel({ detection }: Props) {
  /**
   * Lokální posuvná historie detekčních událostí.
   * Nová událost se vkládá na **začátek** pole (index 0), starší události
   * se posouvají doprava. Pole je ořezáváno na MAX_HISTORY prvků.
   */
  const [history, setHistory] = useState<DetectionEvent[]>([]);

  /**
   * Ref na scrollovatelný `<div>` seznamu historie.
   * Umožňuje imperativně nastavit `scrollTop = 0` a posunout seznam na vrchol
   * bez nutnosti řešit tuto logiku přes stav Reactu.
   */
  const listRef = useRef<HTMLDivElement>(null);

  /**
   * Efekt č. 1 — přidání nové detekce do historie.
   *
   * Spouští se pokaždé, když se změní prop `detection` (nový snímek ze streamu).
   * Pokud je `detection` null (iniciální stav nebo reset), efekt se neprovedou.
   *
   * Logika aktualizace stavu:
   *  - Nová událost se vloží na pozici 0 (`[detection, ...prev]`), aby byl
   *    nejnovější záznam vždy nahoře v historii i v DOM.
   *  - `slice(0, MAX_HISTORY)` ořízne pole na maximální povolený počet prvků,
   *    čímž zabrání neomezenému růstu paměti (viz komentář u MAX_HISTORY).
   */
  useEffect(() => {
    if (!detection) return;
    setHistory((prev) => {
      // Nová událost na začátek, pak ořez na limit.
      const next = [detection, ...prev];
      return next.slice(0, MAX_HISTORY);
    });
  }, [detection]);

  /**
   * Efekt č. 2 — automatický scroll na vrchol seznamu historie.
   *
   * Spouští se po každé změně pole `history` (tj. vždy po přidání nové události).
   * Nastavením `scrollTop = 0` zajistíme, že uživatel vždy vidí nejnovější
   * událost (která je na indexu 0) bez nutnosti ručního scrollování nahoru.
   * Tento efekt záměrně neobsahuje `detection` v závislostech — zajímá nás
   * až moment, kdy je stav `history` skutečně aktualizován.
   */
  useEffect(() => {
    if (listRef.current) {
      listRef.current.scrollTop = 0;
    }
  }, [history]);

  /**
   * Počet osob v aktuálně zobrazeném snímku.
   * Pokud ještě žádná detekce nedorazila (`detection` je null), použijeme 0
   * jako výchozí hodnotu, aby hero číslo zobrazilo "0" místo prázdného místa.
   */
  const personCount = detection?.person_count ?? 0;

  /**
   * Naformátovaný čas poslední detekce pro zobrazení pod hero číslem.
   * Je `null`, pokud ještě žádná detekce nedorazila — v takovém případě
   * se celý řádek s časem v JSX vůbec nerendí (podmíněné renderování).
   */
  const timestamp = detection?.timestamp
    ? formatTimestamp(detection.timestamp)
    : null;

  // Ochrana proti backend payloadům, kde `detections` může být null.
  const currentDetections = detection?.detections ?? [];

  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 p-5 flex flex-col gap-5">
      {/* ------------------------------------------------------------------ */}
      {/* Hlavička panelu                                                      */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-widest">
          Detections
        </h2>
        {/* Počet uchovaných událostí — zobrazuje se jen pokud máme alespoň jednu */}
        {history.length > 0 && (
          <span className="text-xs text-gray-500">
            last {history.length} event{history.length !== 1 ? "s" : ""}
          </span>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Hero sekce — výrazné číslo s počtem detekovaných osob               */}
      {/* ------------------------------------------------------------------ */}
      <div className="flex flex-col items-center gap-1 py-2">
        {/*
         * Velké číslo — podmíněné barvy:
         *  - `text-green-400` pokud je detekována alespoň 1 osoba (aktivní stav).
         *  - `text-gray-600`  pokud není detekována žádná osoba (neaktivní/klidový stav).
         * `transition-colors duration-300` zajišťuje plynulý přechod barev mezi stavy.
         * `tabular-nums` zabraňuje poskakování layoutu při změně čísla (monospace číslice).
         */}
        <span
          className={`text-7xl font-extrabold tabular-nums transition-colors duration-300 ${
            personCount > 0 ? "text-green-400" : "text-gray-600"
          }`}
        >
          {personCount}
        </span>
        {/*
         * Popisek pod číslem — singulár/plurál:
         *  - Přesně 1 osoba → "person detected"
         *  - 0 nebo více osob → "persons detected"
         */}
        <span className="text-sm text-gray-400 font-medium">
          {personCount === 1 ? "person detected" : "persons detected"}
        </span>
        {/* Čas poslední detekce — skryje se, pokud timestamp není k dispozici */}
        {timestamp && (
          <span className="text-xs text-gray-500 mt-1">
            last seen at {timestamp}
          </span>
        )}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Breakdown aktuálního snímku — bounding-boxy + confidence bary       */}
      {/* Sekce se zobrazí pouze pokud přišla detekce s alespoň jednou osobou */}
      {/* ------------------------------------------------------------------ */}
      {detection && currentDetections.length > 0 && (
        <div className="bg-gray-700/50 rounded-lg p-3 flex flex-col gap-2">
          {/* Nadpis sekce s číslem snímku */}
          <p className="text-xs font-medium text-gray-400 uppercase tracking-wider mb-1">
            Frame #{detection.frame_id} &mdash; confidence
          </p>
          {/* Scrollovatelný seznam osob — max. výška 48 (12 rem) zabraňuje přetečení */}
          <div className="flex flex-col gap-2 max-h-48 overflow-y-auto pr-1">
            {currentDetections.map((det, idx) => (
              // `key={idx}` je zde bezpečné, protože se jedná o statický seznam
              // jednoho snímku, který se celý nahrazuje při každé nové detekci.
              <div key={idx} className="flex flex-col gap-0.5">
                <div className="flex items-center justify-between">
                  {/* Pořadové označení osoby (1-based pro uživatele) */}
                  <span className="text-xs text-gray-300">
                    Person {idx + 1}
                  </span>
                  {/*
                   * Souřadnice bounding-boxu ve formátu [x1, y1] → [x2, y2]
                   * kde (x1, y1) je levý horní roh a (x2, y2) pravý dolní roh.
                   * Souřadnice jsou v pixelech relativních k rozlišení snímku.
                   */}
                  <span className="text-xs text-gray-500">
                    [{det.x1}, {det.y1}] → [{det.x2}, {det.y2}]
                  </span>
                </div>
                {/* Vizuální pruh míry jistoty detekce pro tuto osobu */}
                <ConfidenceBar confidence={det.confidence} />
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Oddělovač mezi breakdownem a historií                               */}
      {/* Renderuje se pouze tehdy, existuje-li alespoň jedna položka historie,*/}
      {/* aby se zbytečný border nezobrazoval v prázdném stavu panelu.         */}
      {/* ------------------------------------------------------------------ */}
      {history.length > 0 && <div className="border-t border-gray-700" />}

      {/* ------------------------------------------------------------------ */}
      {/* Sekce historie nedávných událostí                                   */}
      {/* ------------------------------------------------------------------ */}
      {history.length > 0 ? (
        <div>
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-2">
            Recent events
          </p>
          {/*
           * Scrollovatelný kontejner historie; `ref={listRef}` umožňuje efektu č. 2
           * imperativně resetovat scroll na začátek při příchodu nové události.
           */}
          <div
            ref={listRef}
            className="flex flex-col gap-2 max-h-52 overflow-y-auto pr-1
                       scrollbar-thin scrollbar-thumb-gray-600 scrollbar-track-transparent"
          >
            {history.map((evt, idx) => (
              /*
               * Klíč je kompozitní `frame_id-idx`:
               *  - Samotné `frame_id` by nestačilo, pokud by backend re-odeslal
               *    stejný snímek (duplikát by mohl mít stejný klíč).
               *  - `idx` zajišťuje unikátnost v rámci aktuálního pole historie.
               *
               * Zvýraznění nejnovějšího záznamu (idx === 0):
               *  - `bg-gray-700 border-gray-600`    — aktivní (nejnovější) záznam.
               *  - `bg-gray-800 border-gray-700/50` — starší záznamy (méně výrazné).
               */
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
                  {/*
                   * Kruhová bublina s počtem osob:
                   *  - Zelená (`bg-green-400/20 text-green-400`) pokud je alespoň 1 osoba.
                   *  - Šedá   (`bg-gray-700 text-gray-500`)      pro nulový počet.
                   */}
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
                    {/* Slovní popis počtu osob — singulár/plurál */}
                    <span className="text-xs text-gray-300 font-medium">
                      {evt.person_count === 1
                        ? "1 person"
                        : `${evt.person_count} persons`}
                    </span>
                    {/* Číslo snímku jako sekundární informace */}
                    <span className="text-xs text-gray-600">
                      frame #{evt.frame_id}
                    </span>
                  </div>
                </div>
                {/* Čas události zarovnaný vpravo */}
                <span className="text-xs text-gray-500">
                  {formatTimestamp(evt.timestamp)}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : (
        /* ---------------------------------------------------------------- */
        /* Prázdný stav — žádná detekce zatím nedorazila                    */
        /* ---------------------------------------------------------------- */
        <div className="flex flex-col items-center justify-center py-6 gap-2 text-gray-600">
          <span className="text-3xl">🔍</span>
          <span className="text-sm">Waiting for detections…</span>
        </div>
      )}
    </div>
  );
}
