// Package main implementuje RTSP relay server, který slouží jako simulovaná IP kamera –
// čte videosoubor ze souborového systému, překóduje ho pomocí ffmpeg a distribuuje jako
// živý RTSP stream. Interní architektura je dvouvrstvá:
//
//  1. ffmpeg běží jako podproces a publikuje RTP pakety na lokální RTSP server
//     (role: vydavatel/publisher).
//  2. gortsplib v4 tvoří RTSP server, který přijme publikaci od ffmpeg a současně
//     obsluhuje libovolný počet čtenářů (role: konzumenti, např. OpenCV/vprocessor).
//
// Výsledkem je, že pro vnější klienty vypadá server jako živá IP kamera přehrávající
// zvolený soubor ve smyčce (volitelně).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"sync"
	"syscall"
	"time"

	gortsplib "github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/pion/rtp"
)

// ---------------------------------------------------------------------------
// Konfigurace aplikace
// ---------------------------------------------------------------------------

// appConfig sdružuje všechny konfigurační hodnoty načtené z příkazového řádku.
// Použití dedikované struktury místo volných proměnných zajišťuje, že konfigurace
// je předávána jako jeden celek a je snadno testovatelná i rozšiřitelná.
type appConfig struct {
	// videoFile je cesta k videosouboru, který bude streamován přes RTSP.
	// Povinný parametr; soubor musí existovat a být čitelný ffmpeg.
	videoFile string

	// port je TCP port, na kterém bude naslouchat RTSP server.
	// Výchozí hodnota 8554 je standardní port pro RTSP (alternativa k 554,
	// která vyžaduje root oprávnění).
	port int

	// udpRTPPort je UDP port pro přenos RTP dat (samotné mediální pakety).
	// Hodnota 0 deaktivuje UDP transport; klienti pak musí použít TCP.
	udpRTPPort int

	// udpRTCPPort je UDP port pro přenos RTCP řídicích zpráv (statistiky kvality,
	// synchronizace). Musí být nenulový zároveň s udpRTPPort, jinak zůstane UDP
	// transport zakázán.
	udpRTCPPort int

	// streamPath je cesta streamu v RTSP URL, např. "live" → rtsp://host:port/live.
	streamPath string

	// loop určuje, zda má ffmpeg přehrávat soubor ve smyčce donekonečna.
	// Při hodnotě false stream skončí po přehrání souboru (jednorázové přehrání).
	loop bool
}

// ---------------------------------------------------------------------------
// serverHandler – RTSP relay handler
// ---------------------------------------------------------------------------

// serverHandler implementuje rozhraní handleru gortsplib v4.
// Plní roli reléového uzlu (relay): ffmpeg se připojí jako vydavatel (publisher) a
// odesílá RTP pakety, zatímco libovolný počet čtenářů (např. OpenCV/vprocessor)
// je odebírá prostřednictvím standardního RTSP protokolu.
type serverHandler struct {
	// server je ukazatel na instanci gortsplib RTSP serveru; ukládá se zde proto,
	// aby byl přístupný z handlerových metod (zejména při vytváření nového streamu
	// v OnAnnounce).
	server *gortsplib.Server

	// mutex chrání sdílená pole stream a publisher před souběžným přístupem
	// z různých goroutin (každé RTSP spojení běží v samostatné goroutině).
	// Používá se RWMutex: čtení (DESCRIBE, SETUP, předávání paketů) lze
	// paralelizovat; zápis (ANNOUNCE, SessionClose) vyžaduje exkluzivní přístup.
	mutex sync.RWMutex

	// stream reprezentuje aktivní serverový stream gortsplib, do nějž jsou zapisovány
	// RTP pakety od vydavatele a z nějž je gortsplib rozesílá všem připojeným čtenářům.
	// Hodnota nil znamená, že žádný vydavatel zatím nepublikuje.
	stream *gortsplib.ServerStream

	// publisher uchovává odkaz na RTSP session ffmpeg procesu (vydavatele).
	// Slouží k rozlišení, zda ukončená session patří vydavateli nebo čtenáři
	// (viz OnSessionClose), a k explicitnímu uzavření staré session při reconnectu.
	publisher *gortsplib.ServerSession

	// writeErrMu chrání přístup k polím writeErrLastLog a writeErrDropped, která
	// jsou sdílena mezi goroutinami gortsplib volajícími OnStreamWriteError.
	writeErrMu sync.Mutex

	// writeErrLastLog zaznamenává čas posledního výpisu varování o zahozených
	// paketech. Používá se pro rate-limiting logu: varování se vypisuje nejvýše
	// jednou za 5 sekund, aby záznamy zůstaly čitelné i při velkém počtu výpadků.
	writeErrLastLog time.Time

	// writeErrDropped počítá pakety zahozené od posledního výpisu varování.
	// Po každém výpisu se nuluje, takže vždy odráží počet zahozených paketů
	// v aktuálním 5sekundovém okně.
	writeErrDropped int
}

// ---------------------------------------------------------------------------
// Handlery RTSP událostí
// ---------------------------------------------------------------------------

// OnConnOpen je volána knihovnou gortsplib při otevření nového TCP spojení.
// Zaznamenává vzdálenou adresu klienta pro účely ladění a monitorování.
func (sh *serverHandler) OnConnOpen(ctx *gortsplib.ServerHandlerOnConnOpenCtx) {
	log.Printf("[INFO] connection opened from %v", ctx.Conn.NetConn().RemoteAddr())
}

// OnConnClose je volána při uzavření TCP spojení (ať už ze strany klienta nebo serveru).
// Chyba ctx.Error obsahuje důvod uzavření; hodnota nil znamená čisté uzavření.
func (sh *serverHandler) OnConnClose(ctx *gortsplib.ServerHandlerOnConnCloseCtx) {
	log.Printf("[INFO] connection closed (%v)", ctx.Error)
}

// OnSessionOpen je volána při vytvoření nové RTSP session (po úspěšném handshaku).
// Jedna TCP spojení může nést více sessions; tato callback slouží pouze k logování.
func (sh *serverHandler) OnSessionOpen(ctx *gortsplib.ServerHandlerOnSessionOpenCtx) {
	log.Printf("[INFO] session opened")
}

// OnSessionClose je volána při ukončení RTSP session (odhlášení klienta, chyba sítě apod.).
// Pokud ukončená session patří vydavateli (ffmpeg), je nutné uvolnit aktivní stream –
// jinak by čtenáři dostávali odpovědi na DESCRIBE ze zastaralého, již neaktivního streamu.
func (sh *serverHandler) OnSessionClose(ctx *gortsplib.ServerHandlerOnSessionCloseCtx) {
	log.Printf("[INFO] session closed")

	// Exkluzivní zámek: modifikujeme stream i publisher.
	sh.mutex.Lock()
	defer sh.mutex.Unlock()

	// Kontrola: uzavírá se právě session vydavatele (ffmpeg)?
	// Čtenáři mají jinou session, jejich odpojení stream nevyžaduje rušit.
	if sh.stream != nil && ctx.Session == sh.publisher {
		log.Printf("[INFO] publisher disconnected, closing stream")
		// Uzavření streamu uvolní všechny interní struktury gortsplib
		// a informuje připojené čtenáře o konci streamu.
		sh.stream.Close()
		sh.stream = nil
		sh.publisher = nil
	}
}

// OnDescribe je volána při přijetí RTSP požadavku DESCRIBE od klienta (čtenáře).
// Požadavek DESCRIBE je první krok protokolu RTSP: klient se dotazuje na SDP popis
// streamu (kodeky, formáty, porty). Pokud stream není k dispozici, vrátíme 404.
func (sh *serverHandler) OnDescribe(
	ctx *gortsplib.ServerHandlerOnDescribeCtx,
) (*base.Response, *gortsplib.ServerStream, error) {
	log.Printf("[INFO] DESCRIBE request from %v (path: %q)", ctx.Conn.NetConn().RemoteAddr(), ctx.Path)

	// Sdílený zámek pro čtení: více čtenářů může volat DESCRIBE souběžně.
	sh.mutex.RLock()
	defer sh.mutex.RUnlock()

	// Vydavatel se ještě nepřipojil nebo se odpojil – stream není k dispozici.
	// Vrátíme 404 Not Found, aby klient věděl, že má zkusit znovu později.
	if sh.stream == nil {
		log.Printf("[WARN] DESCRIBE received but no stream is available yet")
		return &base.Response{
			StatusCode: base.StatusNotFound,
		}, nil, nil
	}

	// Stream existuje – vrátíme 200 OK spolu s odkazem na aktivní ServerStream.
	// gortsplib z něj automaticky vygeneruje SDP odpověď pro klienta.
	return &base.Response{
		StatusCode: base.StatusOK,
	}, sh.stream, nil
}

// OnAnnounce je volána při přijetí RTSP požadavku ANNOUNCE od vydavatele (ffmpeg).
// Požadavek ANNOUNCE je první krok na straně vydavatele: ffmpeg oznamuje serveru,
// jaké média (video/audio stopy, kodeky) bude publikovat, a předává SDP popis.
// Na základě tohoto popisu vytvoříme nový ServerStream, do nějž bude ffmpeg zapisovat.
func (sh *serverHandler) OnAnnounce(
	ctx *gortsplib.ServerHandlerOnAnnounceCtx,
) (*base.Response, error) {
	log.Printf("[INFO] ANNOUNCE request from %v (path: %q)", ctx.Conn.NetConn().RemoteAddr(), ctx.Path)

	// Exkluzivní zámek: měníme stream i publisher.
	sh.mutex.Lock()
	defer sh.mutex.Unlock()

	// Pokud již existuje aktivní stream (např. z předchozího spuštění ffmpeg),
	// uzavřeme ho před nahrazením novým. Tak předejdeme úniku zdrojů a zajistíme,
	// že čtenáři dostanou aktualizovaný SDP popis.
	if sh.stream != nil {
		log.Printf("[INFO] replacing existing stream from previous publisher")
		// Uzavření starého streamu odpojí všechny stávající čtenáře.
		sh.stream.Close()
		if sh.publisher != nil {
			// Uzavření staré session vydavatele uvolní interní buffery gortsplib.
			sh.publisher.Close()
		}
	}

	// Vytvoříme nový ServerStream svázaný s tímto serverem a SDP popisem od ffmpeg.
	// Tento objekt bude sloužit jako distribuční bod pro všechny čtenáře.
	sh.stream = gortsplib.NewServerStream(sh.server, ctx.Description)
	// Uložíme referenci na session vydavatele pro pozdější identifikaci v OnSessionClose.
	sh.publisher = ctx.Session

	log.Printf("[INFO] publisher registered, stream created with %d media(s)",
		len(ctx.Description.Medias))

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnSetup je volána při přijetí RTSP požadavku SETUP.
// Požadavek SETUP slouží k vyjednání transportní metody (TCP/UDP) a portů pro RTP/RTCP.
// Chování se liší podle role volající session:
//   - Vydavatel (ffmpeg v režimu nahrávání, stav PreRecord): vrátíme 200 OK bez streamu,
//     protože stream je teprve připravován a přidělí se při RECORD.
//   - Čtenář: vrátíme 200 OK s odkazem na aktivní stream, nebo 404 pokud žádný není.
func (sh *serverHandler) OnSetup(
	ctx *gortsplib.ServerHandlerOnSetupCtx,
) (*base.Response, *gortsplib.ServerStream, error) {
	log.Printf("[INFO] SETUP request")

	// Vydavatel (ffmpeg) je ve stavu PreRecord – ještě nenahrává, pouze nastavuje transport.
	// V tomto bodě stream nevracíme; gortsplib ho přiřadí automaticky při RECORD.
	if ctx.Session.State() == gortsplib.ServerSessionStatePreRecord {
		return &base.Response{
			StatusCode: base.StatusOK,
		}, nil, nil
	}

	// Sdílený zámek pro čtení: jde o žádost čtenáře.
	sh.mutex.RLock()
	defer sh.mutex.RUnlock()

	// Stream zatím neexistuje – vydavatel se ještě nepřipojil.
	if sh.stream == nil {
		return &base.Response{
			StatusCode: base.StatusNotFound,
		}, nil, nil
	}

	// Vrátíme aktivní stream; gortsplib zajistí, že čtenář bude odebírat pakety
	// ze správného RTP toku odpovídajícího požadované médiové stopě.
	return &base.Response{
		StatusCode: base.StatusOK,
	}, sh.stream, nil
}

// OnPlay je volána při přijetí RTSP požadavku PLAY od čtenáře.
// Po úspěšné odpovědi začne gortsplib předávat RTP pakety z aktivního streamu
// do odchozí fronty dané čtenářské session.
func (sh *serverHandler) OnPlay(
	ctx *gortsplib.ServerHandlerOnPlayCtx,
) (*base.Response, error) {
	log.Printf("[INFO] PLAY request — reader starting playback")

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnRecord je volána při přijetí RTSP požadavku RECORD od vydavatele (ffmpeg).
// Po tomto okamžiku začne ffmpeg odesílat RTP pakety na server.
// Zde registrujeme callback OnPacketRTPAny, který každý příchozí paket okamžitě
// přeposílá do aktivního streamu (a tím ke všem připojeným čtenářům).
func (sh *serverHandler) OnRecord(
	ctx *gortsplib.ServerHandlerOnRecordCtx,
) (*base.Response, error) {
	log.Printf("[INFO] RECORD request — ffmpeg is now publishing RTP packets")

	// Registrace callbacku pro příchozí RTP pakety na libovolné mediální stopě.
	// OnPacketRTPAny je volána gortsplib v goroutině pro každý přijatý RTP paket,
	// takže musíme zajistit thread-safe přístup ke sdílenému poli stream.
	ctx.Session.OnPacketRTPAny(func(medi *description.Media, _ format.Format, pkt *rtp.Packet) {
		// Sdílený zámek: čteme pouze referenci na stream; zápis do streamu je
		// thread-safe na úrovni gortsplib (WritePacketRTP je reentrantní).
		sh.mutex.RLock()
		stream := sh.stream
		sh.mutex.RUnlock()

		// Pokud byl stream mezitím uzavřen (vydavatel se odpojil), pakety zahazujeme.
		if stream == nil {
			return
		}

		// Předáme paket aktivnímu streamu; gortsplib ho rozešle všem čtenářům.
		// Chyby zápisu (např. "write queue is full") jsou hlášeny přes callback
		// OnStreamWriteError níže – není třeba je logovat dvakrát.
		_ = stream.WritePacketRTP(medi, pkt)
	})

	return &base.Response{
		StatusCode: base.StatusOK,
	}, nil
}

// OnStreamWriteError je volána knihovnou gortsplib vždy, když se nepodaří doručit
// paket do odchozí fronty některé čtenářské session. Nejčastější příčina je, že
// fronta je plná, tj. konzument čte pomaleji, než vydavatel produkuje.
//
// Konkrétní scénář: vprocessor blokuje volání cap.read() po dobu inference YOLO
// (~10 FPS), zatímco RTSP zdroj produkuje snímky rychlostí ~30 FPS. Odchozí fronta
// čtenáře se zaplní a gortsplib začne přebytečné pakety zahazovat.
//
// Aby log zůstal čitelný i při masivním zahazování (stovky paketů za sekundu),
// aplikujeme rate-limiting: souhrnné varování se vypíše nejvýše jednou za 5 sekund
// s informací o celkovém počtu zahozených paketů v daném okně.
func (sh *serverHandler) OnStreamWriteError(ctx *gortsplib.ServerHandlerOnStreamWriteErrorCtx) {
	// Atomicky zvýšíme počítadlo zahozených paketů a přečteme aktuální stav
	// pro rozhodnutí, zda je čas vypsat varování.
	sh.writeErrMu.Lock()
	sh.writeErrDropped++
	count := sh.writeErrDropped
	lastLog := sh.writeErrLastLog
	sh.writeErrMu.Unlock()

	// Pokud uplynulo alespoň 5 sekund od posledního výpisu, vypíšeme souhrnné varování.
	if time.Since(lastLog) >= 5*time.Second {
		// Znovu zamkneme, abychom atomicky resetovali stav pro příští okno.
		sh.writeErrMu.Lock()
		sh.writeErrLastLog = time.Now()
		sh.writeErrDropped = 0
		sh.writeErrMu.Unlock()

		log.Printf("[WARN] slow RTSP reader – write queue full, %d packet(s) dropped "+
			"in the last 5 s. The consumer (vprocessor) is reading slower than the "+
			"stream is produced. Consider a larger WriteQueueSize or decoupling "+
			"RTSP reading from inference in vprocessor.", count)
	}
}

// ---------------------------------------------------------------------------
// ffmpeg helpers
// ---------------------------------------------------------------------------

// buildFFmpegArgs sestaví seznam argumentů pro příkaz ffmpeg tak, aby přečetl
// zadaný videosoubor a publikoval ho jako RTSP stream na lokální server.
// Argumenty jsou navrženy pro minimální latenci a maximální kompatibilitu
// s konzumenty jako OpenCV nebo VLC.
func buildFFmpegArgs(videoFile string, port int, streamPath string, loop bool) []string {
	args := []string{
		// -re: čti vstup v reálném čase (native frame rate).
		// Bez tohoto přepínače by ffmpeg poslal celý soubor co nejrychleji
		// a zahltil by odchozí frontu serveru. S -re je tempo přehrávání
		// synchronizováno s časovými razítky snímků ve zdrojovém souboru.
		"-re",
	}

	if loop {
		// -stream_loop -1: nekonečná smyčka vstupu.
		// Hodnota -1 znamená "opakuj donekonečna"; kladné číslo N by soubor
		// přehrálo N+1krát. Bez tohoto přepínače ffmpeg po skončení souboru skončí.
		args = append(args, "-stream_loop", "-1")
	}

	args = append(args,
		// -i <soubor>: cesta ke vstupnímu videosouboru (zdroj snímků).
		"-i", videoFile,

		// -c:v libx264: překóduj video stopu kodekem H.264 (libx264).
		// H.264 je nejlépe podporovaný videoformát v RTSP/RTP ekosystému;
		// OpenCV, VLC i gortsplib ho zpracují nativně bez dalších závislostí.
		"-c:v", "libx264",

		// -preset ultrafast: nejrychlejší enkódovací preset libx264.
		// Snižuje výpočetní náročnost enkódování na minimum na úkor kompresního
		// poměru – pro relay z lokálního souboru je to ideální kompromis,
		// protože nám jde o nízkou latenci, ne o velikost streamu.
		"-preset", "ultrafast",

		// -tune zerolatency: nastaví libx264 do režimu nulové latence.
		// Zakáže B-snímky a vyrovnávací buffery, které by jinak zvyšovaly
		// latenci o desítky až stovky milisekund. Klíčové pro použití s živým
		// zpracováním (inference v reálném čase).
		"-tune", "zerolatency",

		// -pix_fmt yuv420p: výstupní pixel formát YUV 4:2:0.
		// Jde o nejkompatibilnější formát pro H.264 – vyžaduje ho například
		// OpenCV při dekódování. Bez tohoto přepínače by libx264 mohl zvolit
		// jiný formát (např. yuv444p), který řada dekoderů nepodporuje.
		"-pix_fmt", "yuv420p",

		// -an: bez zvuku (audio not included).
		// Odstraní veškeré audio stopy ze výstupu. Vprocessor zvuk nepotřebuje
		// a jeho přenášení by zbytečně plýtvalo přenosovým pásmem a
		// komplikovalo SDP popis pro čtenáře.
		"-an",

		// -rtsp_transport tcp: použij TCP jako transportní protokol pro RTSP spojení
		// mezi ffmpeg a naším serverem.
		// Výchozí UDP transport je nespolehlivý v prostředí se ztrátami paketů;
		// TCP zaručuje pořadí a doručení, což je klíčové pro lokální loopback spojení.
		"-rtsp_transport", "tcp",

		// -f rtsp: výstupní formát je RTSP.
		// Říká ffmpeg, že má enkódované pakety zabalit do RTP/RTSP a odeslat
		// na zadanou URL (místo zápisu do souboru).
		"-f", "rtsp",

		// Cílová URL: lokální RTSP server na portu port, cesta streamPath.
		// ffmpeg se připojí jako vydavatel (ANNOUNCE + RECORD) a bude
		// odesílat RTP pakety do serverHandler.OnRecord.
		fmt.Sprintf("rtsp://127.0.0.1:%d/%s", port, streamPath),
	)

	return args
}

// runFFmpegLoop spouští ffmpeg jako podproces a v případě ukončení ho (pokud je
// povolena smyčka) restartuje. Funkce blokuje volající goroutinu až do zrušení
// kontextu ctx, které signalizuje požadavek na ukončení celé aplikace.
//
// Parametry:
//   - ctx: kontext životního cyklu aplikace; zrušení ukončí smyčku
//   - videoFile: cesta ke vstupnímu videosouboru
//   - port: port RTSP serveru, na který bude ffmpeg publikovat
//   - streamPath: cesta streamu (např. "live")
//   - loop: pokud true, ffmpeg se po skončení restartuje
func runFFmpegLoop(ctx context.Context, videoFile string, port int, streamPath string, loop bool) {
	for {
		// Před každým pokusem o spuštění zkontrolujeme, zda nebyl kontext zrušen.
		// Tím zabráníme zbytečnému spouštění ffmpeg po obdržení signálu ukončení.
		select {
		case <-ctx.Done():
			log.Printf("[INFO] ffmpeg loop stopping (context cancelled)")
			return
		default:
			// Kontext je stále aktivní, pokračujeme spuštěním ffmpeg.
		}

		// Sestavíme argumenty příkazu ffmpeg pro aktuální konfiguraci.
		args := buildFFmpegArgs(videoFile, port, streamPath, loop)
		log.Printf("[INFO] spawning ffmpeg with args: %v", args)

		// Vytvoříme příkaz svázaný s kontextem: jakmile bude ctx zrušen,
		// Go automaticky zašle SIGKILL podprocesu ffmpeg.
		cmd := exec.CommandContext(ctx, "ffmpeg", args...)

		// Přesměrujeme stdout a stderr ffmpeg na výstup tohoto procesu,
		// aby byla diagnostická hlášení ffmpeg viditelná v logu aplikace
		// (např. informace o kodekovém nastavení, varování o formátu apod.).
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr

		// Spustíme ffmpeg a čekáme na jeho ukončení (blokující volání).
		if err := cmd.Run(); err != nil {
			// Rozlišíme, zda ffmpeg skončil kvůli zrušení kontextu (normální ukončení)
			// nebo kvůli skutečné chybě (pád, neplatný vstup, síťová chyba apod.).
			select {
			case <-ctx.Done():
				// Kontext byl zrušen – ukončení ffmpeg je očekávané, není to chyba.
				log.Printf("[INFO] ffmpeg stopped (shutdown requested)")
				return
			default:
				// ffmpeg skončil neočekávaně – zalogujeme chybu a pokusíme se restartovat.
				log.Printf("[WARN] ffmpeg exited with error: %v", err)
			}
		} else {
			// ffmpeg skončil čistě (návratový kód 0) – typicky na konci souboru bez smyčky.
			log.Printf("[INFO] ffmpeg exited cleanly")
		}

		// Pokud je smyčka zakázána, po prvním skončení ffmpeg (ať úspěšném nebo ne)
		// celou goroutinu ukončíme – nebudeme restartovat.
		if !loop {
			log.Printf("[INFO] loop disabled — not restarting ffmpeg")
			return
		}

		// Před restartem chvíli počkáme, aby nedocházelo k příliš rychlým restartům
		// v případě opakovaných chyb (např. nedostupný server, poškozený soubor).
		// Čekáme rovněž na případné zrušení kontextu, abychom reagovali okamžitě.
		log.Printf("[INFO] restarting ffmpeg in 1 second...")
		select {
		case <-ctx.Done():
			// Požadavek na ukončení přišel během čekání na restart – končíme.
			return
		case <-time.After(1 * time.Second):
			// Uplynula 1 sekunda, spustíme ffmpeg znovu.
		}
	}
}

// ---------------------------------------------------------------------------
// Inicializační funkce – každá má jednu jasně vymezenou odpovědnost
// ---------------------------------------------------------------------------

// parseFlags definuje a parsuje všechny příkazové přepínače aplikace.
// Vrací naplněnou strukturu appConfig připravenou k dalšímu zpracování.
// Nastavuje také vlastní formát nápovědy (flag.Usage) pro konzistentní UX.
func parseFlags() *appConfig {
	cfg := &appConfig{}

	flag.StringVar(&cfg.videoFile, "video", "",
		"cesta k videosouboru, který bude streamován (povinné; podporuje MP4, MKV, AVI, MOV…)")
	flag.IntVar(&cfg.port, "port", 8554,
		"TCP port RTSP serveru (výchozí 8554; port 554 vyžaduje root)")
	flag.IntVar(&cfg.udpRTPPort, "udp-rtp", 8000,
		"UDP port pro RTP datové pakety (0 = UDP transport vypnut)")
	flag.IntVar(&cfg.udpRTCPPort, "udp-rtcp", 8001,
		"UDP port pro RTCP řídicí zprávy (0 = UDP transport vypnut)")
	flag.StringVar(&cfg.streamPath, "path", "live",
		"cesta streamu v URL, např. 'live' → rtsp://host:port/live")
	flag.BoolVar(&cfg.loop, "loop", true,
		"opakovat video ve smyčce po jeho skončení")

	// Vlastní nápověda: přidáme stručný popis použití před výpis přepínačů.
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "Použití: vstreamer --video <soubor> [přepínače]\n\n")
		fmt.Fprintf(os.Stderr, "Přepínače:\n")
		flag.PrintDefaults()
	}

	flag.Parse()
	return cfg
}

// validateConfig ověří, že konfigurace je úplná a konzistentní.
// Vrací chybu s popisem problému; volající je odpovědný za zobrazení a ukončení.
func validateConfig(cfg *appConfig) error {
	// Parametr --video je povinný; bez videosouboru nelze aplikaci spustit.
	if cfg.videoFile == "" {
		flag.Usage()
		return fmt.Errorf("přepínač --video je povinný")
	}

	// Ověříme, že zadaný soubor skutečně existuje na disku.
	// Tím předejdeme záhadným chybám ffmpeg, které by se projevily až při jeho spuštění.
	if _, err := os.Stat(cfg.videoFile); os.IsNotExist(err) {
		return fmt.Errorf("videosoubor nenalezen: %s", cfg.videoFile)
	}

	return nil
}

// logStartup vypíše kompletní přehled konfigurace do logu při startu aplikace.
// Díky tomu je vždy patrné, s jakými parametry aplikace běží – klíčové při diagnostice.
func logStartup(cfg *appConfig) {
	log.Printf("[INFO] starting vstreamer")
	log.Printf("[INFO]   video file    : %s", cfg.videoFile)
	log.Printf("[INFO]   RTSP port     : %d", cfg.port)
	log.Printf("[INFO]   UDP RTP port  : %d", cfg.udpRTPPort)
	log.Printf("[INFO]   UDP RTCP port : %d", cfg.udpRTCPPort)
	log.Printf("[INFO]   stream path   : %s", cfg.streamPath)
	log.Printf("[INFO]   loop          : %v", cfg.loop)
	// Pomocný tip pro ruční ověření streamu přehrávačem VLC.
	// Přepínač --no-satip-enable zabraňuje VLC v pokusu o SAT>IP protokol,
	// který by mohl interferovat s běžným RTSP připojením.
	log.Printf("[INFO] VLC tip: vlc --no-satip-enable rtsp://localhost:%d/%s",
		cfg.port, cfg.streamPath)
}

// buildRTSPServer vytvoří, nakonfiguruje a spustí gortsplib RTSP server.
// Přiřadí referenci na server do handleru (potřebnou v OnAnnounce) a nastaví
// UDP transport, pokud jsou zadány příslušné porty.
// Vrací ukazatel na běžící server nebo chybu, pokud Start() selže.
func buildRTSPServer(h *serverHandler, cfg *appConfig) (*gortsplib.Server, error) {
	srv := &gortsplib.Server{
		// Handler odkazuje na náš serverHandler implementující všechny RTSP callbacky.
		Handler: h,

		// RTSPAddress určuje adresu a port pro TCP naslouchání.
		// Prázdný host (":<port>") znamená naslouchání na všech síťových rozhraních.
		RTSPAddress: fmt.Sprintf(":%d", cfg.port),

		// WriteQueueSize nastavuje kapacitu odchozí fronty RTP paketů pro každého čtenáře.
		// Výchozí hodnota gortsplib je 256 paketů (~1,7 s při 30 FPS a průměrné
		// fragmentaci H.264 ~5 paketů/snímek).
		// Zvýšením na 1024 získáme ~6 sekund rezervy, která pokryje krátkodobé
		// výpadky způsobené těžkou YOLO inferencí v vprocessor (~100 ms/snímek).
		WriteQueueSize: 1024,
	}

	// UDP transport umožňuje klientům (VLC, ffplay) připojit se bez --rtsp-tcp.
	// Aktivuje se pouze tehdy, jsou-li oba porty (RTP i RTCP) nenulové.
	if cfg.udpRTPPort > 0 && cfg.udpRTCPPort > 0 {
		// Prázdný host = naslouchání na všech síťových rozhraních.
		srv.UDPRTPAddress = fmt.Sprintf(":%d", cfg.udpRTPPort)
		srv.UDPRTCPAddress = fmt.Sprintf(":%d", cfg.udpRTCPPort)
		log.Printf("[INFO] UDP transport enabled (RTP :%d, RTCP :%d)",
			cfg.udpRTPPort, cfg.udpRTCPPort)
	} else {
		log.Printf("[INFO] UDP transport disabled — clients must use TCP (e.g. vlc --rtsp-tcp …)")
	}

	// Uložíme referenci na server do handleru dříve, než zavoláme Start(),
	// aby byla dostupná ihned po prvním příchozím spojení (OnAnnounce).
	h.server = srv

	// Spustíme server; Start() otevře TCP/UDP porty a zahájí naslouchání.
	if err := srv.Start(); err != nil {
		return nil, fmt.Errorf("failed to start RTSP server: %w", err)
	}

	log.Printf("[INFO] RTSP server listening on :%d", cfg.port)
	log.Printf("[INFO] readers can connect to: rtsp://localhost:%d/%s", cfg.port, cfg.streamPath)

	return srv, nil
}

// listenForShutdown spustí goroutinu, která čeká na signál SIGINT (Ctrl+C) nebo
// SIGTERM (systemd/Docker stop) a po jeho přijetí zavolá cancel(), čímž zruší
// kontext a spustí řízené ukončení celé aplikace.
func listenForShutdown(cancel context.CancelFunc) {
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		sig := <-sigChan
		log.Printf("[INFO] received signal %v — initiating graceful shutdown", sig)
		// Zrušení kontextu způsobí ukončení runFFmpegLoop a odblokování hlavní smyčky.
		cancel()
	}()
}

// waitAndStartFFmpeg počká, až bude RTSP server plně připraven přijímat spojení,
// a pak spustí smyčku ffmpeg v samostatné goroutině.
//
// Krátká prodleva (500 ms) je nutná proto, aby ffmpeg při prvním pokusu o ANNOUNCE
// neobdržel "connection refused" – k tomu by mohlo dojít, pokud by se ffmpeg
// pokusil připojit okamžitě po volání srv.Start().
//
// Pokud je kontext zrušen během čekání, funkce vrátí context.Canceled a ffmpeg
// se nespustí. Volající by měl tento případ vyhodnotit jako čistý exit (bez chyby).
func waitAndStartFFmpeg(ctx context.Context, cfg *appConfig) error {
	log.Printf("[INFO] waiting 500ms for server to be ready before spawning ffmpeg...")

	select {
	case <-ctx.Done():
		// Signál ukončení přišel ještě před uplynutím čekací doby.
		return ctx.Err()
	case <-time.After(500 * time.Millisecond):
		// Server je připraven; pokračujeme spuštěním ffmpeg.
	}

	// Spustíme smyčku ffmpeg v samostatné goroutině, aby neblokovala hlavní vlákno.
	// Goroutina poběží až do zrušení kontextu (signál ukončení).
	go runFFmpegLoop(ctx, cfg.videoFile, cfg.port, cfg.streamPath, cfg.loop)

	return nil
}

// ---------------------------------------------------------------------------
// Orchestrátor životního cyklu
// ---------------------------------------------------------------------------

// run řídí kompletní životní cyklus aplikace: inicializaci, běh a řízené ukončení.
// Vrací chybu, pokud některý z kroků selže; nil při čistém ukončení.
//
// Pořadí kroků:
//  1. Parsování a validace konfigurace z CLI.
//  2. Logování konfigurace při startu.
//  3. Sestavení a spuštění RTSP serveru (defer zajistí jeho uzavření při ukončení).
//  4. Vytvoření shutdown kontextu a registrace handleru signálů.
//  5. Čekání na připravenost serveru a spuštění ffmpeg smyčky.
//  6. Blokování do přijetí signálu ukončení.
func run() error {
	// Nastavení formátu logů: datum + čas s mikrosekundovou přesností.
	// Mikrosekudy jsou užitečné při ladění latence a časování paketů.
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)

	// ── Konfigurace ────────────────────────────────────────────────────────────
	cfg := parseFlags()

	if err := validateConfig(cfg); err != nil {
		return err
	}

	logStartup(cfg)

	// ── RTSP server ────────────────────────────────────────────────────────────
	h := &serverHandler{}

	srv, err := buildRTSPServer(h, cfg)
	if err != nil {
		return err
	}
	// Defer zajistí uzavření serveru při libovolném způsobu ukončení run().
	defer func() {
		log.Printf("[INFO] shutting down RTSP server...")
		srv.Close()
		log.Printf("[INFO] vstreamer stopped")
	}()

	// ── Shutdown kontext a obsluha signálů ─────────────────────────────────────
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	listenForShutdown(cancel)

	// ── Spuštění ffmpeg ────────────────────────────────────────────────────────
	if err := waitAndStartFFmpeg(ctx, cfg); err != nil {
		if errors.Is(err, context.Canceled) {
			// Kontext byl zrušen ještě před spuštěním ffmpeg – čistý exit, ne chyba.
			log.Printf("[INFO] context cancelled before ffmpeg could start")
			return nil
		}
		return err
	}

	// ── Čekání na ukončení ─────────────────────────────────────────────────────
	// Hlavní goroutina blokuje, dokud kontext není zrušen (SIGINT/SIGTERM).
	// Po odblokování se provede defer srv.Close() výše.
	<-ctx.Done()

	return nil
}

// ---------------------------------------------------------------------------
// Vstupní bod
// ---------------------------------------------------------------------------

// main je minimální vstupní bod aplikace – deleguje veškerou logiku na run().
// Tento vzor (thin main + run() error) usnadňuje testování a umožňuje run()
// používat defer pro úklid bez nutnosti volat os.Exit() uvnitř logiky aplikace.
func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}
