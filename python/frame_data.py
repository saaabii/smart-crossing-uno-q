"""
BeSafe — logica dell'app (unica fonte di verità della regola, vedi SPEC.md e CONTRATTO.md).

Solo Python standard: niente brick qui dentro, così si testa sul Mac.
    1. camera:   estrai_oggetti → fondi_ciclisti → Tracker.aggiorna_tracce → oggetti normalizzati
    2. sensori:  misure grezze per lato (dall'MCU o da MockSensors) → spazio di arresto, pericolo
    3. builder:  FrameDataBuilder unisce camera e sensori → messaggio "frame_data" per la dashboard

    builder = FrameDataBuilder(load_config(), load_zones())
    builder.update_camera(t_cam, oggetti)                    # a ogni analisi della camera
    ui.send_message("frame_data", message=builder.build(t, sensori))
"""

from __future__ import annotations

import json
import math
import os
import random
from itertools import count
from types import SimpleNamespace

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

VULNERABILI = {"pedone", "ciclista"}
SIDES = ("N", "S", "E", "O")
SIDE_NAMES = {"N": "NORD", "S": "SUD", "E": "EST", "O": "OVEST"}

# Parametri della regola: sezioni "rule" e "sensors" di config.json, appiattite
DEFAULT_CFG = {
    "t_reaction_s": 1.0, "decel_ms2": 6.0, "brake_threshold_ms2": 2.0, "attention_margin": 1.5,
    "state_hold_s": 2.0, "road_conditions": [[5.5, "asciutto"], [4.0, "bagnato"], [0.0, "scivoloso"]],
    "scale": 1.0, "stopped_kmh": 5.0, "stale_s": 0.5, "pass_distance_m": 2.0, "pass_rise_m": 3.0,
    "detection_timeout_s": 1.5,
}
# Solo la linea di conteggio: la camera è inclinata (da un palo), le zone a terra non servono.
# Si imposta dalla dashboard ("Imposta linea") e viene salvata in config.json.
DEFAULT_ZONES = {"count_line": [[0.30, 0.60], [0.70, 0.60]]}

# Campo del messaggio set_thresholds → (minimo, massimo)
THRESHOLD_FIELDS = {
    "t_reaction_s": (0.5, 2.0),
    "decel_ms2": (3.0, 8.0),
    "brake_threshold_ms2": (0.5, 4.0),
}


def read_json(path: str = CONFIG_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_config(path: str = CONFIG_PATH) -> SimpleNamespace:
    """Parametri della regola da config.json (sezioni "rule", "sensors", "camera"); i mancanti restano ai default."""
    data = read_json(path)
    cfg = dict(DEFAULT_CFG)
    for section in ("rule", "sensors", "camera"):
        cfg.update({k: v for k, v in data.get(section, {}).items() if k in DEFAULT_CFG})
    return SimpleNamespace(**cfg)


def load_zones(path: str = CONFIG_PATH) -> dict:
    zones = read_json(path).get("zones", {})
    return {"count_line": zones.get("count_line", DEFAULT_ZONES["count_line"])}


def save_config_value(keys: list, value, path: str = CONFIG_PATH) -> None:
    """Scrive un valore in config.json (es. ["zones", "count_line"]) lasciando invariato il resto."""
    data = read_json(path)
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)                       # scrittura atomica: config.json non resta mai a metà


def parse_count_line(msg) -> list | None:
    """Messaggio set_count_line {"count_line": [[x1, y1], [x2, y2]]} → linea valida (0-1) o None.

    I punti vengono ordinati da sinistra a destra, così "→" vuol dire sempre "verso l'alto
    dell'immagine" (lontano dalla camera) qualunque sia l'ordine dei due clic.
    """
    try:
        (x1, y1), (x2, y2) = (msg or {})["count_line"]
    except (KeyError, TypeError, ValueError):
        return None
    raw = (x1, y1, x2, y2)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw):
        return None
    pts = [[float(x1), float(y1)], [float(x2), float(y2)]]
    if any(not 0.0 <= v <= 1.0 for p in pts for v in p):
        return None
    if math.dist(pts[0], pts[1]) < 0.02:          # i due clic devono essere distinti
        return None
    if (pts[0][0], pts[0][1]) > (pts[1][0], pts[1][1]):
        pts.reverse()
    return [[round(v, 4) for v in p] for p in pts]


# ===================================================================== camera
# Formato del brick VideoObjectDetection (on_detect_all):
#   {label: [{"bounding_box_xyxy": [x1, y1, x2, y2], "confidence": float}, ...]}
KEEP = {"person", "bicycle", "motorcycle"}
RIDEABLE = {"bicycle", "motorcycle"}   # persona sovrapposta → un solo oggetto (ciclista / motociclista)
DISCARD = {"motorcycle"}               # dopo la fusione: il motociclista non è un pedone né un ciclista
CLS_NAMES = {"person": "pedone", "bicycle": "ciclista"}
DETECTION_DEFAULTS = {"min_conf": 0.3, "merge_threshold": 0.3, "merge_metric": "ios"}


def from_brick(detections: dict) -> list[dict]:
    """Uscita del brick → [{"cls", "bbox": (x1,y1,x2,y2), "conf"}]."""
    out = []
    for label, items in (detections or {}).items():
        if isinstance(items, dict):
            items = [items]
        for it in items:
            x1, y1, x2, y2 = (float(v) for v in it["bounding_box_xyxy"])
            out.append({"cls": str(label), "bbox": (x1, y1, x2, y2), "conf": float(it.get("confidence", 1.0))})
    return out


def area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersection(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def iou(a, b) -> float:
    inter = intersection(a, b)
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def ios(a, b) -> float:
    """Intersezione / area del box più piccolo: robusta per persona alta + bici larga."""
    small = min(area(a), area(b))
    return intersection(a, b) / small if small > 0 else 0.0


def union_box(a, b) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def estrai_oggetti(dets: list[dict], cfg: dict | None = None) -> list[dict]:
    """Tiene solo persone e bici (e moto, solo per assorbire il conducente) con confidence sufficiente."""
    c = {**DETECTION_DEFAULTS, **(cfg or {})}
    return [d for d in dets if d["cls"] in KEEP and d["conf"] >= c["min_conf"]]


def fondi_ciclisti(dets: list[dict], cfg: dict | None = None) -> list[dict]:
    """Fonde persona + bici/moto sovrapposte in un unico oggetto, poi scarta le moto.

    Così il ciclista (o il motociclista) non conta come pedone in attesa.
    """
    c = {**DETECTION_DEFAULTS, **(cfg or {})}
    overlap = iou if c["merge_metric"] == "iou" else ios
    people = [d for d in dets if d["cls"] == "person"]
    rides = [d for d in dets if d["cls"] in RIDEABLE]

    # Match greedy uno a uno persona ↔ bici/moto: prima le coppie più sovrapposte
    pairs = sorted(((overlap(p["bbox"], r["bbox"]), i, j)
                    for i, p in enumerate(people) for j, r in enumerate(rides)), reverse=True)
    used_p, used_r, riders = set(), set(), []
    for score, i, j in pairs:
        if score <= c["merge_threshold"]:
            break
        if i in used_p or j in used_r:
            continue
        used_p.add(i); used_r.add(j)
        p, r = people[i], rides[j]
        riders.append({"cls": r["cls"], "bbox": union_box(p["bbox"], r["bbox"]), "conf": max(p["conf"], r["conf"])})

    out = (riders
           + [p for i, p in enumerate(people) if i not in used_p]
           + [r for j, r in enumerate(rides) if j not in used_r])
    return [d for d in out if d["cls"] not in DISCARD]


def center_dist(a, b) -> float:
    """Distanza tra i centri dei box, in altezze medie dei due box."""
    ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    h = ((a[3] - a[1]) + (b[3] - b[1])) / 2
    return math.dist(ca, cb) / h if h > 0 else math.inf


class Tracker:
    """ID stabili (#1, #2…): prima per IoU, poi per distanza dei centri.

    A 2 Hz una persona vicina alla camera si sposta anche di mezzo box tra due analisi e l'IoU può
    scendere a 0: in quel caso si associa per distanza tra i centri, misurata in altezze del box.
    """

    def __init__(self, iou_threshold: float = 0.1, max_age: int = 5, max_center_dist: float = 1.0):
        self.iou_threshold = iou_threshold      # IoU minimo per associare
        self.max_age = max_age                  # analisi senza match prima di eliminare il track
        self.max_center_dist = max_center_dist  # ripiego senza IoU: distanza massima tra i centri (in altezze)
        self.tracks: dict[int, dict] = {}       # id → {"cls", "bbox", "age"}
        self._ids = count(1)

    def aggiorna_tracce(self, dets: list[dict]) -> list[dict]:
        matched_t, matched_d, out = set(), set(), []

        def match(pairs, accept):
            for score, tid, j in pairs:
                if not accept(score):
                    break
                if tid in matched_t or j in matched_d:
                    continue
                matched_t.add(tid); matched_d.add(j)
                self.tracks[tid].update(bbox=dets[j]["bbox"], age=0)
                out.append({"id": tid, "cls": dets[j]["cls"], "bbox": dets[j]["bbox"]})

        same_cls = [(tid, j) for tid, tr in self.tracks.items() for j, d in enumerate(dets) if d["cls"] == tr["cls"]]
        # 1) coppie (track, detection) della stessa classe per IoU decrescente
        match(sorted(((iou(self.tracks[tid]["bbox"], dets[j]["bbox"]), tid, j) for tid, j in same_cls), reverse=True),
              lambda s: s >= self.iou_threshold)
        # 2) ripiego per i rimasti: centri più vicini
        match(sorted((center_dist(self.tracks[tid]["bbox"], dets[j]["bbox"]), tid, j) for tid, j in same_cls
                     if tid not in matched_t and j not in matched_d),
              lambda s: s <= self.max_center_dist)

        # Track non associati: invecchiano, eliminati oltre max_age
        for tid in [t for t in self.tracks if t not in matched_t]:
            self.tracks[tid]["age"] += 1
            if self.tracks[tid]["age"] > self.max_age:
                del self.tracks[tid]

        # Detection non associate: nuovi ID
        for j, d in enumerate(dets):
            if j not in matched_d:
                tid = next(self._ids)
                self.tracks[tid] = {"cls": d["cls"], "bbox": d["bbox"], "age": 0}
                out.append({"id": tid, "cls": d["cls"], "bbox": d["bbox"]})
        return out


class CameraPipeline:
    """Uscita del brick → oggetti del contratto [{"id","cls","box" normalizzato,"dir"}]."""

    def __init__(self, detection_cfg: dict | None = None, tracker_cfg: dict | None = None):
        self.det_cfg = detection_cfg or {}
        self.tracker = Tracker(**(tracker_cfg or {}))
        self.prev_center: dict[int, tuple] = {}

    def step(self, detections: dict, width: float, height: float) -> list[dict]:
        """width/height = dimensione del frame su cui sono espressi i box (se i box sono già 0-1, si usano così)."""
        dets = fondi_ciclisti(estrai_oggetti(from_brick(detections), self.det_cfg), self.det_cfg)
        tracks = self.tracker.aggiorna_tracce(dets)
        objects, centers = [], {}
        for tr in tracks:
            x1, y1, x2, y2 = tr["bbox"]
            if max(x2, y2) > 1.5:                              # box in pixel → normalizzati
                x1, x2, y1, y2 = x1 / width, x2 / width, y1 / height, y2 / height
            c = ((x1 + x2) / 2 * width, (y1 + y2) / 2 * height)   # centro in pixel, per la direzione
            centers[tr["id"]] = c
            prev = self.prev_center.get(tr["id"])
            direction = [0.0, 0.0]
            if prev:
                dx, dy = c[0] - prev[0], c[1] - prev[1]
                n = math.hypot(dx, dy)
                if n > 0.01 * height:                          # sotto l'1% dell'altezza = fermo
                    direction = [round(dx / n, 3), round(dy / n, 3)]
            objects.append({"id": tr["id"], "cls": CLS_NAMES.get(tr["cls"], tr["cls"]),
                            "box": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)], "dir": direction})
        self.prev_center = centers
        return objects


# ==================================================================== sensori veri
SOURCES = ("real", "mock", "off")      # sensors.sources in config.json, per lato


def parse_sensor_reply(reply, t: float) -> dict:
    """Risposta di Bridge.call("get_sensor", lato) → misura grezza per FrameDataBuilder.build().

    Lo sketch risponde "d,v,a" in interi del modellino: d mm (-1 = nessun veicolo), v mm/s (positiva = si
    avvicina), a mm/s² (positiva = frena). "nd", None (sensore che non risponde) o testo non valido → offline.
    La scala (sensors.scale) la applica read_side, come per i sensori finti.
    """
    try:
        d, v, a = (float(x) for x in str(reply).split(","))
    except (TypeError, ValueError):
        return {"t": t, "online": False}
    return {"t": t, "online": True, "distance_m": None if d < 0 else d / 1000,
            "speed_kmh": v / 1000 * 3.6, "decel_ms2": a / 1000}


# ===================================================================== fisica
def d_stop(speed_kmh: float, t_reaction_s: float, decel_ms2: float) -> float:
    """Spazio di arresto: percorso nel tempo di reazione + frenata a decelerazione costante."""
    v = speed_kmh / 3.6
    return v * t_reaction_s + v * v / (2 * decel_ms2)


def braking_distance(speed_kmh: float, decel_ms2: float) -> float:
    """Dove si ferma un veicolo che sta già frenando con questa decelerazione."""
    v = speed_kmh / 3.6
    return v * v / (2 * decel_ms2)


OFFLINE = {"distance_m": None, "speed_kmh": 0.0, "decel_ms2": 0.0, "braking": False,
           "d_stop_m": None, "danger": False, "online": False, "status": "offline"}


def read_side(raw: dict | None, t: float, c) -> tuple[dict, float | None]:
    """Misura grezza di un sensore → (lato nel formato del contratto, gravità grezza 0-1 o None)."""
    if raw is None or not raw.get("online", True) or t - raw["t"] > c.stale_s:
        return dict(OFFLINE), None
    dist = raw.get("distance_m")
    dist = None if dist is None or dist < 0 else dist * c.scale
    speed = raw.get("speed_kmh", 0.0) * c.scale
    decel = raw.get("decel_ms2", 0.0) * c.scale
    braking = decel >= c.brake_threshold_ms2
    side = {"distance_m": None if dist is None else round(dist, 1), "speed_kmh": round(speed, 1),
            "decel_ms2": round(decel, 1), "braking": braking, "d_stop_m": None, "danger": False,
            "online": True, "status": "libero"}
    if dist is None or speed < c.stopped_kmh:
        return side, None

    # (a) non frena: spazio di arresto di riferimento; (b) frena: dove si ferma con la frenata misurata,
    # cioè pericoloso se decel < a_richiesta = v²/(2·d)
    ds = braking_distance(speed, decel) if braking else d_stop(speed, c.t_reaction_s, c.decel_ms2)
    side["d_stop_m"] = round(ds, 1)
    m = c.attention_margin
    if dist < ds:
        side["status"], side["danger"] = "pericolo", True
        return side, _clamp(1 - dist / ds)
    if not braking and dist < m * ds:
        side["status"] = "arrivo"
        return side, _clamp((m * ds - dist) / ((m - 1) * ds))
    return side, None


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


# ================================================================== geometria
def ground_point(box):
    """Punto a terra: centro del lato basso del box."""
    return ((box[0] + box[2]) / 2, box[3])


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segments_cross(p1, p2, q1, q2) -> bool:
    """True se il segmento p1-p2 attraversa il segmento q1-q2 (intersezione propria)."""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return d1 * d2 < 0 and d3 * d4 < 0


def crossing_direction(p_prev, p_cur, line, aspect: float) -> str:
    """"→" se il passaggio va verso la sinistra della linea guardando da a verso b (sullo schermo), "←" altrimenti."""
    lx, ly = (line[1][0] - line[0][0]) * aspect, line[1][1] - line[0][1]     # in pixel
    mx, my = (p_cur[0] - p_prev[0]) * aspect, p_cur[1] - p_prev[1]
    # sinistra sullo schermo (y verso il basso) = (ly, -lx)
    return "→" if mx * ly - my * lx > 0 else "←"


# ==================================================================== builder
class FrameDataBuilder:
    def __init__(self, cfg=None, zones: dict | None = None, aspect: float = 4 / 3):
        self.cfg = cfg if cfg is not None else load_config()   # unica variabile dei parametri
        self.zones = zones if zones is not None else load_zones()
        self.aspect = aspect                                   # larghezza/altezza del frame, per le direzioni
        self.objects: list[dict] = []
        self.camera_t = None
        self.people_exposed = False
        self.counts: dict[str, dict[str, int]] = {}            # attraversamenti della count_line {cls: {→, ←}}
        self.detected = {"pedone": 0, "ciclista": 0}          # persone rilevate (ID confermati)
        self.vehicles = {s: 0 for s in SIDES}                 # veicoli passati per lato
        self.camera_frame = 0                                 # numero dell'analisi della camera
        self.seen: dict[int, tuple] = {}                      # id → (ultima analisi, analisi consecutive, t)
        self.confirmed: set = set()                           # id già contati tra i rilevati
        self.armed: dict = {s: None for s in SIDES}           # distanza minima del veicolo vicino alla linea
        self.prev_ground: dict[int, tuple] = {}     # id → (t, punto a terra)
        self.counted: set = set()                   # id già contati
        self.last_pericolo = float("-inf")
        self.last_attenzione = float("-inf")
        self.last_raw = {"PERICOLO": 0.0, "ATTENZIONE": 0.0}     # gravità grezza dell'ultimo frame con la condizione
        self.last_side = {"PERICOLO": None, "ATTENZIONE": None}
        self.state = "RIPOSO"

    # ------------------------------------------------------- soglie ----
    def set_thresholds(self, msg: dict) -> bool:
        """Gestisce il messaggio set_thresholds. Ignora campi non validi; True se qualcosa è cambiato."""
        changed = False
        for key, (lo, hi) in THRESHOLD_FIELDS.items():
            v = (msg or {}).get(key) if isinstance(msg, dict) else None
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                continue
            setattr(self.cfg, key, float(v))
            changed = True
        return changed

    def condition(self) -> str:
        for min_decel, label in self.cfg.road_conditions:
            if self.cfg.decel_ms2 >= min_decel:
                return label
        return self.cfg.road_conditions[-1][1]

    def thresholds(self) -> dict:
        c = self.cfg
        return {"t_reaction_s": c.t_reaction_s, "decel_ms2": c.decel_ms2,
                "brake_threshold_ms2": c.brake_threshold_ms2, "condition": self.condition()}

    # ------------------------------------------------------- camera ----
    def update_camera(self, t: float, objects: list[dict]) -> None:
        """Nuova analisi della camera: oggetti [{"id","cls","box","dir"}] solo pedoni e ciclisti."""
        self.camera_t = t
        self.objects = objects
        # Camera inclinata: esposto = almeno un pedone o ciclista nell'inquadratura
        self.people_exposed = any(o["cls"] in VULNERABILI for o in objects)
        self._update_detected(t, objects)
        self._update_counts(t, objects)

    def _update_detected(self, t, objects):
        """Rilevati: +1 per ogni nuovo ID presente in almeno 2 analisi consecutive (niente falsi positivi da 1 frame)."""
        self.camera_frame += 1
        for o in objects:
            if o["cls"] not in VULNERABILI:
                continue
            last = self.seen.get(o["id"])
            streak = last[1] + 1 if last and last[0] == self.camera_frame - 1 else 1
            self.seen[o["id"]] = (self.camera_frame, streak, t)
            if streak >= 2 and o["id"] not in self.confirmed:
                self.confirmed.add(o["id"])
                self.detected[o["cls"]] += 1
        # Pulizia: ID spariti da più di 10 s (il tracker non riusa gli ID)
        for tid in [k for k, (_, _, tl) in self.seen.items() if t - tl > 10]:
            del self.seen[tid]
            self.confirmed.discard(tid)

    def reset_counts(self) -> None:
        """Messaggio reset_counts (pannello demo) e mezzanotte: azzera rilevati, attraversamenti e veicoli.
        Le persone già confermate non vengono ricontate."""
        self.counts = {}
        self.detected = {"pedone": 0, "ciclista": 0}
        self.vehicles = {s: 0 for s in SIDES}

    def set_count_line(self, msg) -> bool:
        """Messaggio set_count_line: nuova linea di conteggio (i contatori restano)."""
        line = parse_count_line(msg)
        if line is None:
            return False
        self.zones["count_line"] = line
        self.prev_ground.clear()                    # le posizioni precedenti erano rispetto alla vecchia linea
        return True

    def _update_counts(self, t, objects):
        line = self.zones["count_line"]
        for o in objects:
            if o["cls"] not in VULNERABILI:
                continue
            gp = ground_point(o["box"])
            prev = self.prev_ground.get(o["id"])
            self.prev_ground[o["id"]] = (t, gp)
            if prev is None or o["id"] in self.counted:
                continue
            if segments_cross(prev[1], gp, line[0], line[1]):
                self.counted.add(o["id"])
                d = crossing_direction(prev[1], gp, line, self.aspect)
                per_cls = self.counts.setdefault(o["cls"], {})
                per_cls[d] = per_cls.get(d, 0) + 1

        # Pulizia dei track spariti da più di 2 s
        for tid in [k for k, (tp, _) in self.prev_ground.items() if t - tp > 2]:
            del self.prev_ground[tid]
            self.counted.discard(tid)

    # ------------------------------------------------------ sensori ----
    def _count_vehicle(self, s: str, side: dict) -> None:
        """Veicolo passato: la distanza è scesa sotto pass_distance_m e poi il veicolo è sparito o la distanza
        è risalita di più di pass_rise_m (il sensore vede quello dietro). Chi si ferma prima non conta."""
        c = self.cfg
        if not side["online"]:
            self.armed[s] = None                     # sensore muto: non si sa se è passato
            return
        d, armed = side["distance_m"], self.armed[s]
        if armed is not None and (d is None or d > armed + c.pass_rise_m):
            self.vehicles[s] += 1
            self.armed[s] = armed = None
        if d is not None and d < c.pass_distance_m:
            self.armed[s] = d if armed is None else min(armed, d)

    def build(self, t: float, sensors_raw: dict) -> dict:
        """sensors_raw: {lato: {"t", "distance_m" (None = nessun veicolo), "speed_kmh", "decel_ms2", "online"}}."""
        c = self.cfg
        # Il brick chiama on_detect_all solo quando vede qualcosa: senza analisi recenti la scena è vuota
        if self.camera_t is not None and t - self.camera_t > c.detection_timeout_s and self.objects:
            self.objects = []
            self.people_exposed = False
        sensors, raw_per, raw_att = {}, {}, {}
        for s in SIDES:
            sensors[s], raw = read_side(sensors_raw.get(s), t, c)
            if raw is not None:
                (raw_per if sensors[s]["danger"] else raw_att)[s] = raw
            self._count_vehicle(s, sensors[s])

        exposed = self.people_exposed
        if exposed and raw_per:
            self.last_pericolo = t
            self.last_side["PERICOLO"] = max(raw_per, key=raw_per.get)
            self.last_raw["PERICOLO"] = raw_per[self.last_side["PERICOLO"]]
        if exposed and raw_att:
            self.last_attenzione = t
            self.last_side["ATTENZIONE"] = max(raw_att, key=raw_att.get)
            self.last_raw["ATTENZIONE"] = raw_att[self.last_side["ATTENZIONE"]]

        # Isteresi: lo stato resta attivo state_hold_s dopo che la condizione sparisce
        hold = c.state_hold_s
        prev_state = self.state
        if t - self.last_pericolo < hold:
            self.state = "PERICOLO"
        elif t - self.last_attenzione < hold:
            self.state = "ATTENZIONE"
        else:
            self.state = "RIPOSO"
        # Gravità nella fascia dello stato: RIPOSO 0, ATTENZIONE [0, 0.5], PERICOLO [0.5, 1]
        if self.state == "PERICOLO":
            severity = 0.5 + 0.5 * self.last_raw["PERICOLO"]
        elif self.state == "ATTENZIONE":
            severity = 0.5 * self.last_raw["ATTENZIONE"]
        else:
            severity = 0.0
        danger_side = self.last_side.get(self.state)

        event = None
        if self.state == "PERICOLO" and prev_state != "PERICOLO":
            s = sensors[danger_side]
            event = {"type": "pericolo", "t": t, "side": danger_side, "speed_kmh": s["speed_kmh"],
                     "distance_m": s["distance_m"], "d_stop_m": s["d_stop_m"]}

        return {
            "t": t,
            "camera_t": self.camera_t,
            "objects": self.objects,
            "people_exposed": exposed,
            "sensors": sensors,
            "state": self.state,
            "severity": round(severity, 2),
            "danger_side": danger_side,
            "counts": {**self.counts, "detected": dict(self.detected), "vehicles": dict(self.vehicles)},
            "zones": self.zones,
            "event": event,
            "thresholds": self.thresholds(),
        }


# ============================================================= mock sensori
# Sensori simulati sulla scheda (lati "mock" di sensors.sources), con demo_mode di config.json:
#   "auto":    quando la camera vede una persona arriva da un lato a caso un veicolo veloce che non frena
#              (scatta PERICOLO); almeno DEMO_PAUSE_S tra un evento e il successivo
#   "manuale": solo con il tasto "Genera pericolo" (esempio di riferimento di SPEC.md)
#   "off":     nessun veicolo, mai pericolo
DEMO_MODES = ("auto", "manuale", "off")
DEMO_PAUSE_S = 10.0          # pausa minima tra la fine di un evento e il successivo
DEMO_KMH = (40.0, 55.0)      # auto: velocità del veicolo che non frena
SENSOR_RANGE_M = 45.0        # portata simulata: il veicolo compare a questa distanza
STOP_HOLD_S = 4.0            # quanto resta fermo il veicolo dello scenario prima di ripartire...
CREEP_MS = 1.1               # ...a passo d'uomo (sotto stopped_kmh) finché non ha superato la linea
STOP_V_MS = 0.3              # sotto questa velocità chi frena si considera fermo
NOISE_M, NOISE_V, NOISE_A = 0.05, 0.02, 0.15   # rumore delle misure (m, frazione, m/s²)

# Esempio di riferimento di SPEC.md: veicolo da N a 30 km/h che non frena, poi frena a 9 m e si ferma
SCENARIO_SIDE, SCENARIO_KMH, SCENARIO_START_M = "N", 30.0, 40.0
SCENARIO_BRAKE_AT_M, SCENARIO_DECEL = 9.0, 6.0


class MockSensors:
    """I 4 sensori simulati: step(dt, persone_viste) e readings(t, scale) come li manderebbe l'MCU."""

    def __init__(self, mode: str = "auto", seed: int | None = None, sides=SIDES):
        self.rng = random.Random(seed)
        self.mode = mode if mode in DEMO_MODES else "auto"
        self.sides = tuple(s for s in SIDES if s in sides)   # solo i lati "mock" di sensors.sources
        self.vehicles: dict[str, list[dict]] = {s: [] for s in SIDES}
        self.sim_t = 0.0
        self.next_event = 0.0           # prima di questo istante la modalità auto non genera eventi

    def active(self) -> bool:
        return any(self.vehicles[s] for s in SIDES)

    def set_mode(self, mode: str) -> bool:
        """Messaggio set_demo_mode. "off" toglie subito i veicoli in arrivo."""
        if mode not in DEMO_MODES:
            return False
        self.mode = mode
        if mode == "off":
            self.vehicles = {s: [] for s in SIDES}
        return True

    def _add_vehicle(self, side, kmh, dist=SENSOR_RANGE_M, **extra):
        self.vehicles[side].append({"dist": dist, "v": kmh / 3.6, "decel": 0.0, "mode": "drive",
                                    "stopped_s": 0.0, **extra})

    def trigger_scenario(self) -> bool:
        """Esempio di riferimento (la persona la deve vedere la camera). Ignorato in modalità "off"."""
        if self.mode == "off":
            return False
        self.vehicles = {s: [] for s in SIDES}
        if not self.sides:
            return False
        # dal lato dell'esempio (N) se è simulato, altrimenti dal primo lato simulato
        side = SCENARIO_SIDE if SCENARIO_SIDE in self.sides else self.sides[0]
        self._add_vehicle(side, SCENARIO_KMH, dist=SCENARIO_START_M,
                          brake_at=SCENARIO_BRAKE_AT_M, brake_decel=SCENARIO_DECEL)
        return True

    def step(self, dt: float, people_seen: bool) -> None:
        self.sim_t += dt
        if (self.mode == "auto" and people_seen and self.sides and not self.active()
                and self.sim_t >= self.next_event):
            side = self.rng.choice(self.sides)                            # solo dai lati simulati
            self._add_vehicle(side, self.rng.uniform(*DEMO_KMH))          # veloce, non frena
        was_active = self.active()
        for s in SIDES:
            for o in self.vehicles[s]:
                self._drive(o, dt)
            self.vehicles[s] = [o for o in self.vehicles[s] if o["dist"] > -5]
        if was_active and not self.active():
            self.next_event = self.sim_t + DEMO_PAUSE_S                   # pausa dopo ogni evento

    def _drive(self, o, dt):
        """Velocità costante; il veicolo dello scenario frena a brake_at, resta fermo e riparte piano."""
        if o["mode"] == "drive" and "brake_at" in o and o["dist"] <= o["brake_at"]:
            o["mode"] = "brake"
        if o["mode"] == "brake":
            o["v"] -= o["brake_decel"] * dt
            o["decel"] = o["brake_decel"]
            if o["v"] < STOP_V_MS:
                o["v"], o["decel"], o["mode"] = 0.0, 0.0, "stopped"
        elif o["mode"] == "stopped":
            o["stopped_s"] += dt
            if o["stopped_s"] > STOP_HOLD_S:
                o["mode"], o["v"] = "creep", CREEP_MS
        o["dist"] -= o["v"] * dt

    def readings(self, t: float, scale: float = 1.0) -> dict:
        """Misure grezze dei 4 sensori (come dall'MCU, divise per la scala): veicolo più vicino davanti alla linea."""
        out = {}
        for s in self.sides:
            ahead = [o for o in self.vehicles[s] if 0 <= o["dist"] <= SENSOR_RANGE_M]
            if not ahead:
                out[s] = {"t": t, "distance_m": None, "speed_kmh": 0.0, "decel_ms2": 0.0, "online": True}
                continue
            o = min(ahead, key=lambda v: v["dist"])
            dist = max(0.0, o["dist"] + self.rng.uniform(-NOISE_M, NOISE_M))
            speed = o["v"] * 3.6 * self.rng.uniform(1 - NOISE_V, 1 + NOISE_V)
            decel = o["decel"] + self.rng.uniform(-NOISE_A, NOISE_A) if o["v"] > 0 else 0.0
            out[s] = {"t": t, "distance_m": round(dist / scale, 3), "speed_kmh": round(speed / scale, 2),
                      "decel_ms2": round(decel / scale, 2), "online": True}
        return out
