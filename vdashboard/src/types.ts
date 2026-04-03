/**
 * @file types.ts
 * @description Sdílené TypeScript typy pro události přenášené přes Socket.IO.
 * Tento soubor definuje datové struktury, které server posílá klientovi
 * při detekci osob a průběžném reportování statistik zpracování videa.
 */

/**
 * Souřadnice a míra jistoty jedné detekované osoby v obraze.
 * Hodnoty `x1`, `y1`, `x2`, `y2` tvoří ohraničující obdélník (bounding box)
 * ve formátu levý-horní roh → pravý-dolní roh, vyjádřený v pixelech.
 */
export interface Detection {
  /** Horizontální souřadnice levého okraje ohraničujícího obdélníku (px). */
  x1: number;

  /** Vertikální souřadnice horního okraje ohraničujícího obdélníku (px). */
  y1: number;

  /** Horizontální souřadnice pravého okraje ohraničujícího obdélníku (px). */
  x2: number;

  /** Vertikální souřadnice dolního okraje ohraničujícího obdélníku (px). */
  y2: number;

  /**
   * Míra jistoty detekce v rozsahu 0–1.
   * Hodnota 1.0 znamená 100% jistotu modelu, že se jedná o osobu.
   */
  confidence: number;
}

/**
 * Událost vysílaná serverem po zpracování jednoho snímku videa.
 * Obsahuje metadata snímku i seznam všech detekcí osob v daném snímku.
 * Na straně klienta odpovídá Socket.IO události `"detection"`.
 */
export interface DetectionEvent {
  /** Pořadové číslo zpracovaného snímku (inkrementuje se od 0). */
  frame_id: number;

  /**
   * Časové razítko zpracování snímku ve formátu ISO 8601
   * (např. `"2024-06-01T12:00:00.000Z"`).
   */
  timestamp: string;

  /** Celkový počet osob detekovaných v tomto snímku. */
  person_count: number;

  /**
   * Pole jednotlivých detekcí – každá položka popisuje jednu
   * detekovanou osobu včetně jejího ohraničujícího obdélníku.
   * Pole je prázdné, pokud nebyla v snímku detekována žádná osoba.
   */
  detections: Detection[];
}

/**
 * Statistická událost vysílaná serverem v pravidelných intervalech.
 * Poskytuje přehled o výkonu pipeline a aktuálním stavu systému.
 * Na straně klienta odpovídá Socket.IO události `"stats"`.
 */
export interface StatsEvent {
  /** Aktuální počet zpracovaných snímků za sekundu (frames per second). */
  fps: number;

  /** Celkový počet snímků zpracovaných od spuštění pipeline. */
  total_frames: number;

  /** Celkový počet detekcí osob zaznamenaných od spuštění pipeline. */
  total_detections: number;

  /** Doba běhu pipeline v sekundách od jejího spuštění. */
  uptime_seconds: number;

  /**
   * Aktuální provozní stav pipeline. Možné hodnoty:
   * - `"idle"`         – pipeline je inicializována, ale zatím nespuštěna.
   * - `"starting"`     – pipeline se připravuje a startuje.
   * - `"connecting"`   – pipeline se pokouší připojit ke zdroji videa.
   * - `"streaming"`    – pipeline aktivně zpracovává video stream.
   * - `"reconnecting"` – došlo k výpadku spojení, probíhá opětovné připojení.
   * - `"stopped"`      – pipeline byla záměrně zastavena uživatelem nebo systémem.
   * - `"error"`        – pipeline se dostala do chybového stavu a vyžaduje zásah.
   */
  status:
    | "idle"
    | "starting"
    | "connecting"
    | "streaming"
    | "reconnecting"
    | "stopped"
    | "error";
}
