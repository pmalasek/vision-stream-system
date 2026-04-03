/**
 * @file main.tsx
 * @description Vstupní bod React aplikace vdashboard.
 *
 * Tento soubor je zodpovědný za inicializaci celé aplikace:
 *  1. Nalezne kořenový DOM element (`#root`) definovaný v `index.html`.
 *  2. Vytvoří React root pomocí moderního `createRoot` API (React 18+).
 *  3. Obalí aplikaci do `<StrictMode>` a spustí renderování.
 *
 * Je importován jako vstupní bod builderu (Vite) – viz `index.html`
 * a `vite.config.ts`.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

// Najdi kořenový DOM element, do kterého bude React aplikace připojena.
// Element s id="root" je definován v souboru index.html.
const rootElement = document.getElementById("root");

/**
 * Explicitní kontrola existence kořenového elementu.
 *
 * Pokud element `#root` v DOM chybí (např. byl omylem smazán z index.html
 * nebo je skript spuštěn mimo očekávané prostředí), React by jinak selhal
 * s méně srozumitelnou chybou hluboko ve svém kódu.
 *
 * Vyhozením výjimky na tomto místě:
 *  - Okamžitě zastavíme inicializaci aplikace.
 *  - Vývojář dostane jasnou a jednoznačnou chybovou zprávu přímo u zdroje.
 *  - Předcházíme tichému selhání, které by bylo obtížné diagnostikovat.
 */
if (!rootElement) throw new Error("Root element not found");

createRoot(rootElement).render(
  /**
   * React.StrictMode – vývojový pomocník bez vizuálního výstupu.
   *
   * Ve vývojovém módu (development):
   *  - Záměrně volá render funkce a lifecycle metody dvakrát, aby odhalil
   *    vedlejší efekty, které by neměly být závislé na pořadí volání.
   *  - Upozorňuje na použití zastaralých API (deprecated lifecycle metody,
   *    starý Context API, string refs apod.).
   *  - Detekuje neočekávané vedlejší efekty v `useEffect` a podobných hookách.
   *
   * V produkčním módu (production):
   *  - StrictMode je zcela ignorován – nemá žádný vliv na výkon ani chování.
   *  - Veškeré duplikované volání jsou optimalizovány pryč při buildu.
   */
  <StrictMode>
    <App />
  </StrictMode>,
);
