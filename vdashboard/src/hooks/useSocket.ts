/**
 * @file useSocket.ts
 * @module hooks/useSocket
 *
 * Custom React hook pro správu Socket.IO spojení s backend procesorem.
 *
 * Tento soubor zajišťuje:
 *  - Vytvoření a udržení jediné (singleton) instance Socket.IO klienta
 *    po celou dobu životnosti stránky.
 *  - Reaktivní React stav (`isConnected`, `latestDetection`, `stats`),
 *    který se automaticky aktualizuje při příchodu událostí ze serveru.
 *  - Správnou práci v React 18 StrictMode (dvojité mountování efektů
 *    v development buildu) bez spuriózních reconnectů.
 */

import { useEffect, useState } from "react";
import { io, Socket } from "socket.io-client";
import type { DetectionEvent, StatsEvent } from "../types";

// ---------------------------------------------------------------------------
// Návratový typ hooku
// ---------------------------------------------------------------------------

/**
 * Hodnoty vrácené hookem `useSocket`.
 *
 * @property latestDetection - Poslední přijatá detekční událost ze serveru,
 *   nebo `null`, pokud ještě žádná nepřišla (počáteční stav / odpojení).
 *   Aktualizuje se při každé příchozí zprávě `"detection"`.
 *
 * @property stats - Poslední přijatá statistická událost ze serveru,
 *   nebo `null`, pokud ještě žádná nepřišla. Aktualizuje se při každé
 *   příchozí zprávě `"stats"`.
 *
 * @property isConnected - Příznak, zda je socket aktuálně připojen
 *   k serveru. `true` po úspěšném handshaku, `false` po odpojení
 *   nebo chybě spojení.
 */
interface UseSocketReturn {
  latestDetection: DetectionEvent | null;
  stats: StatsEvent | null;
  isConnected: boolean;
}

// ---------------------------------------------------------------------------
// Singleton socket na úrovni modulu
// ---------------------------------------------------------------------------
//
// PROČ je socket definován zde, mimo React komponentový strom?
// ─────────────────────────────────────────────────────────────
//
// 1. REACT 18 STRICTMODE – DVOJITÉ MOUNTOVÁNÍ EFEKTŮ
//    React 18 v development módu záměrně spouští každý useEffect dvakrát:
//      mount → cleanup → mount
//    Cílem je odhalit vedlejší efekty, které nejsou správně vyčištěny.
//    Problém nastane, pokud bychom socket VYTVÁŘELI UVNITŘ efektu:
//      - První mount:   socket se vytvoří a připojí  → server zaznamená připojení (total=1)
//      - První cleanup: socket.disconnect()           → server zaznamená odpojení  (total=0)
//      - Druhý mount:   socket se znovu vytvoří       → server zaznamená připojení (total=1)
//    Výsledkem jsou falešné páry connect/disconnect v serverových lozích
//    a zbytečné navazování nového TCP/WebSocket spojení.
//
// 2. ŘEŠENÍ – SINGLETON NA ÚROVNI MODULU
//    Proměnná `_socket` žije mimo React a přetrvá celý životní cyklus stránky:
//      - Cleanup efektu odstraní pouze event listenery (socket.off), NIKDY nedisconnectuje.
//      - Při druhém (skutečném) mountu StrictMode hook znovu zaregistruje
//        listenery na tentýž, již připojený socket.
//      - V produkčním buildu (bez dvojitého mountování) je chování identické.
//
// 3. FAST REFRESH (Vite / webpack HMR)
//    Při uložení souboru HMR znovu spustí efekty, ale modul-level proměnná
//    přežije refresh – socket zůstane připojen a komponenta plynule navazuje.
//
// ---------------------------------------------------------------------------

/** Interní singleton instance Socket.IO klienta. Přístup výhradně přes `getSocket()`. */
let _socket: Socket | null = null;

// ---------------------------------------------------------------------------
// Lazy-init factory funkce
// ---------------------------------------------------------------------------

/**
 * Vrátí existující singleton socket, nebo jej při prvním volání vytvoří
 * a nakonfiguruje (lazy-initialization pattern).
 *
 * Lazy init zajišťuje, že `io()` (a tedy HTTP upgrade požadavek na server)
 * proběhne až v okamžiku, kdy ho komponenta skutečně potřebuje – ne při
 * importu modulu. Díky tomu lze soubor importovat v testech bez vedlejších
 * síťových efektů.
 *
 * URL serveru se čte z proměnné prostředí `VITE_PROCESSOR_URL` (definované
 * v souboru `.env`). Pokud proměnná není nastavena, použije se origin
 * aktuální stránky (vhodné pro produkční nasazení, kde frontend a backend
 * sdílí stejnou doménu/port).
 *
 * @returns Singleton instance {@link Socket} připravená k použití.
 */
function getSocket(): Socket {
  // Pokud socket již existuje, vrátíme ho rovnou bez jakékoli reinicializace.
  if (_socket) return _socket;

  // Určení cílové URL backendu:
  //  - VITE_PROCESSOR_URL: explicitní adresa procesoru (např. při vývoji
  //    kdy frontend běží na :5173 a backend na :8000)
  //  - window.location.origin: fallback pro produkci (stejný server)
  const url =
    (import.meta.env.VITE_PROCESSOR_URL as string | undefined) ??
    window.location.origin;

  _socket = io(url, {
    // Cesta k Socket.IO endpointu na serveru.
    // Musí odpovídat hodnotě nastavené v `server.py` (výchozí je "/socket.io").
    path: "/socket.io",

    // Pořadí preferovaných transportních protokolů.
    // Socket.IO zkusí nejprve nativní WebSocket (nejnižší latence, full-duplex).
    // Pokud WebSocket selže (proxy, firewall), automaticky přepne na long-polling.
    transports: ["websocket", "polling"],

    // Povolit automatické znovupřipojení po výpadku spojení.
    reconnection: true,

    // Maximální počet pokusů o reconnect.
    // `Infinity` = klient se pokouší donekonečna – vhodné pro long-running dashboardy,
    // kde dočasný výpadek serveru nesmí trvale ukončit sledování.
    reconnectionAttempts: Infinity,

    // Základní prodleva (v ms) před prvním pokusem o reconnect.
    // Zabraňuje okamžitému „reconnect storm" po výpadku.
    reconnectionDelay: 1_000,

    // Maximální prodleva (v ms) mezi pokusy – horní mez exponenciálního
    // back-off algoritmu. Prodleva roste: 1 s → 2 s → 4 s → … → max 5 s.
    reconnectionDelayMax: 5_000,

    // Timeout (v ms) pro navázání počátečního spojení.
    // Pokud server do 10 s nepotvrdí handshake, socket vyvolá "connect_error".
    timeout: 10_000,
  });

  return _socket;
}

// ---------------------------------------------------------------------------
// Custom hook
// ---------------------------------------------------------------------------

/**
 * `useSocket` – custom React hook pro real-time komunikaci se serverem.
 *
 * Interně využívá singleton Socket.IO klienta (viz výše) a mapuje jeho
 * události na React stav. Komponenty konzumující tento hook se automaticky
 * překreslí při každé změně stavu spojení nebo příchodu nové události.
 *
 * Použití:
 * ```ts
 * const { isConnected, latestDetection, stats } = useSocket();
 * ```
 *
 * @returns Objekt {@link UseSocketReturn} s reaktivním stavem socketu.
 */
export function useSocket(): UseSocketReturn {
  /**
   * Příznak aktivního spojení se serverem.
   * Inicializován na `false` – před prvním `"connect"` eventem není
   * připojení potvrzeno.
   */
  const [isConnected, setIsConnected] = useState(false);

  /**
   * Poslední přijatá detekční událost (výsledek inference modelu).
   * `null` dokud server nepošle první zprávu `"detection"`.
   */
  const [latestDetection, setLatestDetection] = useState<DetectionEvent | null>(
    null,
  );

  /**
   * Poslední přijatá statistická zpráva (FPS, latence, apod.).
   * `null` dokud server nepošle první zprávu `"stats"`.
   */
  const [stats, setStats] = useState<StatsEvent | null>(null);

  useEffect(() => {
    // Získáme (nebo vytvoříme) singleton socket.
    // Při druhém mountu v StrictMode je socket již připojen – viz komentář výše.
    const socket = getSocket();

    // -----------------------------------------------------------------------
    // Pojmenované reference na handlery
    // -----------------------------------------------------------------------
    // Handlery jsou uloženy do pojmenovaných konstant (nikoli anonymních
    // arrow funkcí předávaných přímo do `socket.on`), protože:
    //
    //  - `socket.off("event", fn)` odstraní POUZE konkrétní funkci identifikovanou
    //    referencí. Anonymní funkce předaná inline do `.on()` by byla jiný objekt
    //    než ten předaný do `.off()` → listener by se nikdy neodstranil.
    //
    //  - Pojmenované reference garantují, že cleanup efektu odstraní přesně ty
    //    listenery, které tento hook zaregistroval – bez rizika, že omylem
    //    smaže listenery registrované jiným kódem (např. jinými hooky/komponentami).
    //
    const onConnect = () => setIsConnected(true);
    const onDisconnect = () => setIsConnected(false);
    const onConnectError = () => setIsConnected(false);
    const onDetection = (data: DetectionEvent) => setLatestDetection(data);
    const onStats = (data: StatsEvent) => setStats(data);

    // Registrace listenerů pro životní cyklus spojení.
    socket.on("connect", onConnect); // úspěšné navázání spojení
    socket.on("disconnect", onDisconnect); // ukončení spojení (server/klient)
    socket.on("connect_error", onConnectError); // chyba při navazování spojení

    // Registrace listenerů pro aplikační události ze serveru.
    socket.on("detection", onDetection); // výsledek detekce objektů
    socket.on("stats", onStats); // průběžné statistiky streamu

    // -----------------------------------------------------------------------
    // Synchronizace stavu při již připojeném socketu
    // -----------------------------------------------------------------------
    // Situace, kdy k tomu dochází:
    //  a) StrictMode: druhý mount efektu – socket se připojil při prvním mountu,
    //     takže `"connect"` event již proběhl před tím, než jsme zaregistrovali
    //     `onConnect`. Bez tohoto bloku by UI po druhém mountu zůstalo ve stavu
    //     `isConnected = false`, dokud nedojde k dalšímu reconnectu.
    //  b) Vite fast-refresh: HMR remountuje komponentu, ale socket zůstane
    //     připojen – stejný problém, stejné řešení.
    //
    // Okamžitou synchronizací zajistíme, že indikátor stavu v UI okamžitě
    // zobrazí správnou hodnotu bez falešného bliknutí „odpojeno".
    if (socket.connected) {
      setIsConnected(true);
    }

    // -----------------------------------------------------------------------
    // Cleanup funkce efektu
    // -----------------------------------------------------------------------
    return () => {
      // Odstraníme POUZE listenery zaregistrované výše v tomto efektu.
      // Přesné reference zajišťují, že neodstraníme cizí listenery.
      socket.off("connect", onConnect);
      socket.off("disconnect", onDisconnect);
      socket.off("connect_error", onConnectError);
      socket.off("detection", onDetection);
      socket.off("stats", onStats);

      // PROČ zde NEVOLÁME socket.disconnect():
      //  - Singleton musí přežít cleanup/remount cyklus StrictMode –
      //    odpojení by zneplatnilo socket i pro druhý (skutečný) mount.
      //  - Při fast-refresh bychom zbytečně přerušili aktivní spojení.
      //  - Socket je sdílený prostředek na úrovni modulu; komponenta,
      //    která se odmountuje, nesmí odpojit socket potenciálně
      //    využívaný jinde v aplikaci.
      //  - Spojení je žádoucí udržovat po celou dobu životnosti stránky –
      //    skutečný úklid (disconnect) by bylo vhodné řešit globálně,
      //    např. při události `beforeunload`.
    };
  }, []); // Prázdné pole závislostí = efekt se spustí jednou po prvním mountu
  // (resp. dvakrát v StrictMode dev buildu – viz komentář u singletonu).

  return { latestDetection, stats, isConnected };
}
