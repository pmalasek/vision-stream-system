/**
 * Vite konfigurace pro vývojový server (development mode)
 *
 * V produkčním nasazení tuto konfiguraci nepotřebujeme – statické soubory
 * obsluhuje nginx a proxy pravidla jsou definována v nginx.conf.
 * Zde popsaná proxy slouží výhradně pro lokální vývoj, kdy Vite dev server
 * běží na portu 5173 a backend (vprocessor) na portu 8000.
 */

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [
    /**
     * Oficiální Vite plugin pro React.
     * Zajišťuje dvě klíčové funkce:
     *   1. Fast Refresh (HMR) – změny v komponentách se projeví okamžitě
     *      bez plného reloadu stránky a bez ztráty stavu aplikace.
     *   2. Automatický JSX transform – není třeba importovat React v každém
     *      souboru; Babel/esbuild transformaci zajistí plugin sám.
     */
    react(),
  ],
  server: {
    /**
     * Port vývojového serveru.
     * Hodnota 5173 je výchozí pro Vite; explicitně ji uvádíme, aby byl
     * port předvídatelný napříč vývojovými prostředími a aby ho bylo možné
     * snadno změnit na jednom místě.
     */
    port: 5173,

    /**
     * Proxy pravidla pro vývojový server.
     *
     * Proč proxy existuje:
     *   Prohlížeč blokuje cross-origin požadavky z bezpečnostních důvodů
     *   (politika CORS). Vite dev server běží na http://localhost:5173,
     *   zatímco backend (vprocessor) běží na http://localhost:8000 – jiný
     *   port znamená jiný origin. Aby nebylo nutné na backendu konfigurovat
     *   CORS hlavičky pro development, přesměrujeme vybrané URL cesty přes
     *   Vite proxy. Z pohledu prohlížeče pak veškerá komunikace probíhá
     *   se stejným originem (localhost:5173).
     *
     *   V produkci tento problém nevzniká – nginx obsluhuje frontend i
     *   backend pod stejnou doménou a portem, takže proxy pravidla jsou
     *   definována přímo v nginx.conf.
     */
    proxy: {
      /**
       * Proxy pro MJPEG video stream.
       *
       * Zkrácený tvar (pouze cílová URL jako řetězec) postačuje, protože
       * nepotřebujeme žádná extra nastavení. Vite provede přímé přesměrování
       * požadavku GET /stream → http://localhost:8000/stream.
       *
       * MJPEG stream je speciální případ HTTP odpovědi: server vrátí hlavičku
       * Content-Type: multipart/x-mixed-replace a poté odesílá nepřetržitý
       * proud JPEG snímků. Spojení je teoreticky nekonečné – proto je důležité,
       * aby proxy nezaváděla zbytečný buffering (Vite to v dev režimu řeší
       * transparentně; v produkci to explicitně vypínáme v nginx.conf).
       */
      "/stream": "http://localhost:8000",

      /**
       * Proxy pro Socket.IO (real-time stavové události).
       *
       * Socket.IO začíná jako standardní HTTP požadavek a následně požádá
       * o upgrade na WebSocket protokol (hlavičky Upgrade: websocket a
       * Connection: Upgrade). Proto musí být nastaveno `ws: true`, aby
       * Vite proxy zpracoval i tento WebSocket upgrade handshake a udržoval
       * trvalé obousměrné WS spojení mezi prohlížečem a backendem.
       *
       * `changeOrigin: true` – přepíše hlavičku Host v proxy požadavku na
       * hodnotu cílového serveru (localhost:8000). Bez tohoto nastavení by
       * vprocessor mohl požadavek odmítnout, protože by viděl Host: localhost:5173.
       */
      "/socket.io": {
        target: "http://localhost:8000",
        ws: true, // povolí WebSocket upgrade (nutné pro Socket.IO transport)
        changeOrigin: true, // přepíše hlavičku Host na cílový server
      },

      /**
       * Proxy pro REST API endpointy vprocessoru.
       *
       * Všechny požadavky začínající /api/ (např. GET /api/status,
       * POST /api/start) jsou přesměrovány na backend. Používáme
       * `changeOrigin: true` ze stejného důvodu jako u Socket.IO výše.
       *
       * Na rozdíl od /stream zde není potřeba speciální nastavení pro
       * streaming – jde o standardní krátkodobé JSON požadavky/odpovědi.
       */
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true, // přepíše hlavičku Host na cílový server
      },

      // WebRTC signaling (SDP offer/answer)
      "/webrtc": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
