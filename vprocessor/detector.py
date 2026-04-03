"""
Modul detector.py – detekce osob pomocí modelu YOLOv8.

Poskytuje třídu PersonDetector, která obaluje model YOLOv8 z knihovny
Ultralytics a filtruje detekované objekty výhradně na osoby (třída 0).
Pro každý zpracovaný snímek vrací anotovanou kopii obrazu (s vykreslnými
ohraničujícími rámečky a popisky) a strukturovaná metadata jednotlivých detekcí.

Kompatibilita:
    - PyTorch >= 2.6 (ošetřena změna výchozí hodnoty `weights_only` v torch.load)
    - Ultralytics YOLO >= 8.x
"""

import logging

import cv2
import numpy as np
import torch
from ultralytics import YOLO

# Standardní logger pojmenovaný podle aktuálního modulu (např. vprocessor.detector).
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanty pro detekci a vizualizaci
# ---------------------------------------------------------------------------

# ID třídy „osoba" v datové sadě COCO, na které jsou modely YOLOv8 trénovány.
PERSON_CLASS_ID = 0

# Barva ohraničujícího rámečku detekované osoby ve formátu BGR.
BOX_COLOR = (0, 255, 0)  # zelená

# Tloušťka čáry ohraničujícího rámečku v pixelech.
BOX_THICKNESS = 2

# Barva pozadí popisku (štítku) ve formátu BGR.
LABEL_BG_COLOR = (0, 0, 0)  # černá

# Barva textu popisku ve formátu BGR.
LABEL_TEXT_COLOR = (0, 255, 0)  # zelená

# Font použitý pro vykreslení textu popisku (Hershey Simplex – standardní OpenCV font).
LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX

# Velikost (měřítko) fontu popisku.
LABEL_FONT_SCALE = 0.55

# Tloušťka tahu fontu popisku v pixelech.
LABEL_FONT_THICKNESS = 1

# Vnitřní odsazení (padding) okolo textu popisku v pixelech.
LABEL_PADDING = 4


class PersonDetector:
    """Obaluje model YOLOv8 a filtruje detekce výhradně na osoby (třída 0).

    Třída se stará o:
      - bezpečné načtení vah modelu (kompatibilita s PyTorch >= 2.6),
      - spuštění inference na předaném snímku,
      - vykreslení ohraničujících rámečků a popisků do kopie snímku,
      - vrácení strukturovaných metadat všech detekovaných osob.
    """

    def __init__(self, model_path: str, confidence: float) -> None:
        """Načte model YOLOv8 ze souboru vah a připraví detektor.

        Args:
            model_path: Cesta k souboru .pt s vahami modelu nebo název modelu,
                        např. 'yolov8n.pt'. Ultralytics při prvním spuštění
                        váhy automaticky stáhne, pokud nejsou přítomny lokálně.
            confidence: Minimální práh spolehlivosti detekce (hodnota 0.0 – 1.0).
                        Detekce s nižší hodnotou jsou zahozeny.
        """
        # Uložení prahu spolehlivosti pro pozdější použití při inferenci.
        self.confidence = confidence

        logger.info(
            "Načítám model YOLO ze souboru '%s' (confidence=%.2f) …",
            model_path,
            confidence,
        )

        # ---------------------------------------------------------------
        # Bezpečná deserializace PyTorch modelů (kompatibilita >= 2.6)
        # ---------------------------------------------------------------
        # PyTorch >= 2.6 změnil výchozí hodnotu parametru `weights_only`
        # v `torch.load` z False na True. To způsobuje selhání při načítání
        # .pt souborů Ultralytics, protože tyto soubory obsahují libovolné
        # Python třídy (nejde jen o tenzory).
        #
        # Řešení: registrovat známé Ultralytics třídy do „bezpečného
        # deserializátoru" PyTorch pomocí `torch.serialization.add_safe_globals`.
        # Tím povolíme jejich deserializaci, aniž bychom museli zcela
        # vypnout bezpečnostní kontrolu (weights_only=False).
        try:
            # Pokus o import všech standardních tříd modelů z aktuální verze
            # Ultralytics. Importujeme je přímo zde (PLC0415), aby byl import
            # podmíněný a nedošlo k chybě při chybějícím modulu na úrovni modulu.
            from ultralytics.nn.tasks import (  # noqa: PLC0415
                ClassificationModel,
                DetectionModel,
                PoseModel,
                SegmentationModel,
                WorldModel,
            )

            # Registrace všech dostupných tříd jako „bezpečných globálů"
            # – PyTorch torch.load je pak smí deserializovat i při weights_only=True.
            torch.serialization.add_safe_globals(
                [
                    DetectionModel,
                    SegmentationModel,
                    PoseModel,
                    ClassificationModel,
                    WorldModel,
                ]
            )
        except (ImportError, AttributeError):
            # Starší verze Ultralytics nemusí obsahovat všechny výše uvedené
            # třídy (např. WorldModel přibyl až v pozdějších verzích).
            # V takovém případě se pokusíme zaregistrovat alespoň ty třídy,
            # které jsou dostupné, a ostatní tiše přeskočíme.

            # Seznam plně kvalifikovaných názvů kandidátních tříd ke kontrole.
            _candidate_classes = [
                "ultralytics.nn.tasks.DetectionModel",
                "ultralytics.nn.tasks.SegmentationModel",
                "ultralytics.nn.tasks.PoseModel",
                "ultralytics.nn.tasks.ClassificationModel",
            ]
            import importlib  # noqa: PLC0415

            # Seznam tříd, které se podařilo úspěšně importovat.
            _safe: list = []
            for _cls_path in _candidate_classes:
                # Rozdělení cesty na název modulu a název třídy.
                _mod_name, _cls_name = _cls_path.rsplit(".", 1)
                try:
                    # Dynamický import modulu a získání atributu třídy.
                    _mod = importlib.import_module(_mod_name)
                    _safe.append(getattr(_mod, _cls_name))
                except (ImportError, AttributeError):
                    # Třída v této verzi Ultralytics neexistuje – přeskočíme ji.
                    pass

            # Registrujeme pouze skutečně nalezené třídy (seznam může být prázdný,
            # pokud je verze Ultralytics velmi stará – pak se nespoléháme na
            # bezpečné globály a necháme torch.load selhat přirozeně).
            if _safe:
                torch.serialization.add_safe_globals(_safe)

        # Načtení modelu YOLO – Ultralytics interně zavolá torch.load.
        self.model = YOLO(model_path)
        logger.info("Model YOLO byl úspěšně načten.")

    # ------------------------------------------------------------------
    # Veřejné rozhraní (Public API)
    # ------------------------------------------------------------------

    def detect(self, frame: np.ndarray) -> tuple[np.ndarray, list[dict]]:
        """Spustí inferenci na snímku a vrátí anotovanou kopii + metadata detekcí.

        Args:
            frame: Snímek ve formátu BGR jako pole NumPy (výška × šířka × 3).

        Returns:
            Dvojice (annotated_frame, detections):
              - annotated_frame: Kopie snímku *frame* s vykreslnými rámečky
                                 a popisky detekovaných osob.
              - detections: Seznam slovníků, každý s klíči
                  ``x1``, ``y1``, ``x2``, ``y2`` (souřadnice rohů rámečku)
                  a ``confidence`` (spolehlivost detekce) – vše jako float.
        """
        # Pracujeme s kopií snímku, aby původní data zůstala nezměněna.
        annotated = frame.copy()

        # Seznam pro shromáždění metadat všech detekovaných osob v tomto snímku.
        detections: list[dict] = []

        # Spuštění inference modelu YOLO:
        #   - conf: minimální práh spolehlivosti (detekce pod touto hodnotou jsou zahozeny)
        #   - classes: omezení inference pouze na třídu 0 (osoba) – snižuje zbytečnou práci
        #   - verbose: potlačení výpisu Ultralytics do konzole při každém snímku
        results = self.model(
            frame,
            conf=self.confidence,
            classes=[PERSON_CLASS_ID],
            verbose=False,
        )

        # Iterace přes výsledky – model vrací seznam, typicky jeden prvek pro jeden snímek.
        for result in results:
            boxes = (
                result.boxes
            )  # Objekt obsahující detekované rámečky tohoto výsledku.

            # Pokud nebyly nalezeny žádné rámečky, přeskočíme tento výsledek.
            if boxes is None:
                continue

            # Zpracování každého detekovaného rámečku.
            for box in boxes:
                # Získání ID třídy detekovaného objektu (mělo by být vždy 0,
                # protože jsme filtrovali na úrovni modelu, ale ověříme pro jistotu).
                class_id = int(box.cls[0])
                if class_id != PERSON_CLASS_ID:
                    # Přeskočit objekty, které nejsou osoby (obranná kontrola).
                    continue

                # Spolehlivost detekce jako float v rozsahu 0.0 – 1.0.
                conf = float(box.conf[0])

                # Souřadnice ohraničujícího rámečku ve formátu xyxy (levý horní
                # a pravý dolní roh) – převedeny na celá čísla pro práci s pixely.
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])

                # Uložení metadat detekce do výsledného seznamu.
                # Spolehlivost zaokrouhlujeme na 4 desetinná místa pro úsporný přenos.
                detections.append(
                    {
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                        "confidence": round(conf, 4),
                    }
                )

                # Vykreslení ohraničujícího rámečku detekované osoby do anotovaného snímku.
                cv2.rectangle(annotated, (x1, y1), (x2, y2), BOX_COLOR, BOX_THICKNESS)

                # Sestavení textového popisku se spolehlivostí v procentech.
                label = f"Person {conf * 100:.1f}%"

                # Vykreslení popisku (s barevným pozadím) nad rámeček.
                self._draw_label(annotated, label, x1, y1)

        return annotated, detections

    # ------------------------------------------------------------------
    # Privátní pomocné metody (Private helpers)
    # ------------------------------------------------------------------

    def _draw_label(
        self,
        frame: np.ndarray,
        label: str,
        box_x1: int,
        box_y1: int,
    ) -> None:
        """Vykreslí popisek s plným pozadím nad levý horní roh ohraničujícího rámečku.

        Pokud by popisek přesahoval horní okraj snímku, umístí se dovnitř
        rámečku (pod jeho horní hranu), aby zůstal vždy viditelný.

        Args:
            frame:   Snímek, do kterého se popisek vykreslí (modifikován in-place).
            label:   Text popisku, který se má zobrazit.
            box_x1:  X-ová souřadnice levého horního rohu ohraničujícího rámečku.
            box_y1:  Y-ová souřadnice levého horního rohu ohraničujícího rámečku.
        """
        # Změření rozměrů vykresleného textu pomocí aktuálního nastavení fontu.
        # Vrací: (šířka textu, výška textu) a baseline (spodní přesah znaků).
        (text_w, text_h), baseline = cv2.getTextSize(
            label, LABEL_FONT, LABEL_FONT_SCALE, LABEL_FONT_THICKNESS
        )

        # Výpočet svislé polohy popisku:
        #   label_y_bottom – spodní hrana textu (Y souřadnice základní linky textu)
        #   label_y_top    – horní hrana celého bloku popisku (pozadí + padding)
        # Standardní poloha: popisek těsně NAD horní hranou rámečku.
        label_y_bottom = box_y1 - LABEL_PADDING
        label_y_top = label_y_bottom - text_h - LABEL_PADDING

        if label_y_top < 0:
            # Popisek by přesahoval horní okraj snímku – přesuneme ho DOVNITŘ rámečku
            # těsně pod jeho horní hranu, aby byl stále viditelný.
            label_y_top = box_y1 + LABEL_PADDING
            label_y_bottom = label_y_top + text_h + LABEL_PADDING

        # Souřadnice obdélníkového pozadí popisku:
        #   bg_x1/bg_y1 – levý horní roh pozadí
        #   bg_x2/bg_y2 – pravý dolní roh pozadí (zahrnuje padding a baseline)
        bg_x1 = box_x1
        bg_y1 = label_y_top
        bg_x2 = box_x1 + text_w + LABEL_PADDING * 2
        bg_y2 = label_y_bottom + baseline

        # Omezení souřadnic pozadí na hranice snímku, aby nevznikaly artefakty
        # při detekci u pravého nebo horního okraje obrazu.
        h, w = frame.shape[:2]
        bg_x2 = min(bg_x2, w - 1)  # pravý okraj nesmí přesáhnout šířku snímku
        bg_y1 = max(bg_y1, 0)  # horní okraj nesmí být záporný

        # Vykreslení plného (vyplněného) obdélníku jako pozadí popisku.
        cv2.rectangle(frame, (bg_x1, bg_y1), (bg_x2, bg_y2), LABEL_BG_COLOR, cv2.FILLED)

        # Souřadnice počátečního bodu textu (levý dolní roh prvního znaku).
        # Horizontálně odsazeno o LABEL_PADDING od levého okraje pozadí.
        text_origin = (box_x1 + LABEL_PADDING, label_y_bottom)

        # Vykreslení textu popisku přes připravené pozadí.
        # cv2.LINE_AA zajišťuje vyhlazení (anti-aliasing) hran textu.
        cv2.putText(
            frame,
            label,
            text_origin,
            LABEL_FONT,
            LABEL_FONT_SCALE,
            LABEL_TEXT_COLOR,
            LABEL_FONT_THICKNESS,
            cv2.LINE_AA,
        )
