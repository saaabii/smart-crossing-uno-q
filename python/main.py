# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0
#
# BeSafe — entry point sulla scheda (App Lab). La logica sta in frame_data.py, i parametri in config.json.
#
#   camera ─► brick VideoObjectDetection ─► on_frame (a camera.analysis_hz) ─► CameraPipeline ─┐
#   sensori per lato (sensors.sources): "real" via Bridge get_sensor, "mock" MockSensors, "off" ┤
#                                                     FrameDataBuilder ◄────────────────────────┘
#   ciclo a camera.video_hz: "frame_data" + "frame" (video) alla pagina web
#   thread "bridge" a sensors.poll_hz: unico che usa il Bridge (get_sensor, set_state, set_counts)

import base64
import os
import threading
import time

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI
from arduino.app_bricks.video_objectdetection import VideoObjectDetection
from arduino.app_peripherals.camera import Camera
from arduino.app_utils.image.adjustments import compress_to_jpeg

from frame_data import (SIDES, SOURCES, CameraPipeline, FrameDataBuilder, MockSensors, load_config, load_zones,
                        parse_sensor_reply, read_json, save_config_value)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CFG = read_json(CONFIG_PATH)
CAM = CFG.get("camera", {})
SENS = CFG.get("sensors", {})
ANALYSIS_HZ = CAM.get("analysis_hz", 2.0)
VIDEO_HZ = CAM.get("video_hz", 10.0)
POLL_HZ = SENS.get("poll_hz", 20.0)
MODEL_LABEL = CAM.get("model_label", "YoloX")    # modello del brick (app.yaml: yolox-object-detection)

# Sorgente di ogni lato: "real" (sensore vero sullo sketch), "mock" (veicoli simulati), "off" (lato grigio)
_sorgenti = SENS.get("sources", {})
SOURCES_BY_SIDE = {s: _sorgenti.get(s, "mock") if _sorgenti.get(s, "mock") in SOURCES else "off" for s in SIDES}
REAL_SIDES = [s for s in SIDES if SOURCES_BY_SIDE[s] == "real"]
MOCK_SIDES = [s for s in SIDES if SOURCES_BY_SIDE[s] == "mock"]
print("Sensori:", SOURCES_BY_SIDE, "· scala", SENS.get("scale", 1.0))

# =====================================================================
# SETUP
# =====================================================================
ui = WebUI()
cam = Camera(resolution=tuple(CAM.get("resolution", (1640, 1232))), fps=CAM.get("fps", 15))
detection_stream = VideoObjectDetection(camera=cam, confidence=CAM.get("confidence", 0.5), debounce_sec=0)

builder = FrameDataBuilder(load_config(CONFIG_PATH), load_zones(CONFIG_PATH))
camera_pipeline = CameraPipeline(CFG.get("detection"), CFG.get("tracker"))
sensors_mock = MockSensors(CFG.get("demo_mode", "auto"), sides=MOCK_SIDES) if MOCK_SIDES else None
lock = threading.Lock()                # on_frame, ciclo principale e thread del Bridge condividono questi dati
letture_reali = {}                     # lato → ultima misura dei sensori veri (dal thread del Bridge)
da_inviare = {"state": None, "counts": None, "info": ""}   # cosa deve mostrare lo sketch

frame_size = list(CAM.get("resolution", (1640, 1232)))   # aggiornata dal primo frame catturato
ultima_analisi = 0.0
prima_detection = True


def on_set_thresholds(sid, msg):
    with lock:
        if builder.set_thresholds(msg):
            print("Soglie:", builder.thresholds())


def on_trigger_scenario(sid, msg):
    # Esempio di riferimento: solo con i sensori simulati (non in demo_mode "off"); la persona la vede la camera
    if sensors_mock is not None:
        with lock:
            ok = sensors_mock.trigger_scenario()
        print("Scenario: veicolo a 30 km/h che non frena, poi frena e si ferma" if ok
              else "Scenario ignorato: demo_mode \"off\" o nessun lato simulato")


def on_set_count_line(sid, msg):
    # Linea di conteggio scelta con due clic sul video: in uso subito e salvata in config.json
    with lock:
        ok = builder.set_count_line(msg)
        line = builder.zones["count_line"]
    if not ok:
        print("Linea di conteggio non valida:", msg)
        return
    try:
        save_config_value(["zones", "count_line"], line, CONFIG_PATH)
        print("Linea di conteggio salvata:", line)
    except OSError as e:
        print("Linea di conteggio in uso ma non salvata in config.json:", e)


def on_set_demo_mode(sid, msg):
    mode = msg.get("mode") if isinstance(msg, dict) else msg
    if sensors_mock is None:
        return
    with lock:
        ok = sensors_mock.set_mode(mode)
    if not ok:
        print("demo_mode non valido:", msg)
        return
    try:
        save_config_value(["demo_mode"], mode, CONFIG_PATH)
    except OSError as e:
        print("demo_mode in uso ma non salvato in config.json:", e)
    print("demo_mode:", mode)


ui.on_message("set_thresholds", on_set_thresholds)
ui.on_message("trigger_scenario", on_trigger_scenario)
ui.on_message("set_count_line", on_set_count_line)
ui.on_message("set_demo_mode", on_set_demo_mode)


def on_reset_counts(sid, msg):
    with lock:
        builder.reset_counts()
    print("Contatori azzerati (dashboard)")


ui.on_message("reset_counts", on_reset_counts)


# =====================================================================
# CAMERA: persone inquadrate (a camera.analysis_hz)
# =====================================================================
def on_frame(detections: dict):
    global ultima_analisi, prima_detection
    ora = time.time()
    if ora - ultima_analisi < 1 / ANALYSIS_HZ:
        return
    ultima_analisi = ora
    if prima_detection and detections:
        # Per verificare in console in che unità sono i box (pixel o 0-1) rispetto al frame
        print(f"Prima detection: {detections} · frame {frame_size[0]}x{frame_size[1]}")
        prima_detection = False
    with lock:
        oggetti = camera_pipeline.step(detections, frame_size[0], frame_size[1])
        builder.update_camera(ora, oggetti)


detection_stream.on_detect_all(on_frame)


# =====================================================================
# BRIDGE (thread dedicato): sensori veri + stato e conteggi allo sketch
# =====================================================================
STATE_CODES = {"RIPOSO": 0, "ATTENZIONE": 1, "PERICOLO": 2}
BRIDGE_REFRESH_S = 2.0       # anche senza cambi, lo sketch riceve stato e conteggi ogni 2 s (se si è riavviato)


def codice_stato(data):
    """set_state(int) = stato*10 + lato (0 N, 1 S, 2 E, 3 O, 9 nessuno). Un solo int, come set_alert."""
    side = data["danger_side"]
    return STATE_CODES[data["state"]] * 10 + (SIDES.index(side) if side else 9)


def codice_conteggi(counts):
    """set_counts(int) = pedoni*1000 + ciclisti rilevati (max 999 ciascuno), per l'LCD in RIPOSO."""
    det = counts["detected"]
    return min(999, det["pedone"]) * 1000 + min(999, det["ciclista"])


def ciclo_bridge():
    """Unico thread che usa il Bridge, così una chiamata lenta non ferma video e pagina.

    - sensori "real": Bridge.call("get_sensor", lato) → "d,v,a" (mm, mm/s, mm/s² del modellino); se lo sketch
      non risponde la misura è online = false (e comunque scade dopo sensors.stale_s)
    - sketch: set_state / set_counts quando cambiano, e ogni BRIDGE_REFRESH_S
    """
    inviato = {"state": None, "counts": None, "t": 0.0}
    ultimo_errore = ultima_stampa = 0.0
    prima_risposta = True
    while True:
        t0 = time.time()
        for side in REAL_SIDES:
            try:
                risposta = Bridge.call("get_sensor", SIDES.index(side))
            except Exception as e:
                risposta = None
                if t0 - ultimo_errore > 5:
                    print(f"Sensore {side}: nessuna risposta dallo sketch ({e})")
                    ultimo_errore = t0
            if prima_risposta and risposta is not None:
                print(f"Sensore {side}: prima risposta {risposta!r}")
                prima_risposta = False
            lettura = parse_sensor_reply(risposta, time.time())
            with lock:
                letture_reali[side] = lettura
            if t0 - ultima_stampa > 1.0:                      # per il test a mano, una riga al secondo
                ultima_stampa = t0
                print(f"Sensore {side}: {risposta!r} → {lettura}")

        with lock:
            richiesta = dict(da_inviare)
        if richiesta["state"] is not None:
            refresh = t0 - inviato["t"] > BRIDGE_REFRESH_S
            try:
                if richiesta["state"] != inviato["state"] or refresh:
                    Bridge.call("set_state", richiesta["state"])
                    if richiesta["state"] != inviato["state"]:
                        print(f"Sketch: set_state({richiesta['state']}) → {richiesta['info']}")
                    inviato["state"] = richiesta["state"]
                if richiesta["counts"] != inviato["counts"] or refresh:
                    Bridge.call("set_counts", richiesta["counts"])
                    inviato["counts"] = richiesta["counts"]
                if refresh:
                    inviato["t"] = t0
            except Exception as e:
                if t0 - ultimo_errore > 5:
                    print("Bridge non pronto:", e)
                    ultimo_errore = t0
        time.sleep(max(0.0, 1 / POLL_HZ - (time.time() - t0)))


threading.Thread(target=ciclo_bridge, name="bridge", daemon=True).start()


# =====================================================================
# CICLO CONTINUO (a camera.video_hz): regola + pagina web
# =====================================================================
ultimo_t = None
giorno = time.localtime().tm_yday


def ciclo():
    global ultimo_t, giorno
    t = time.time()
    dt = 0.0 if ultimo_t is None else min(t - ultimo_t, 0.5)
    ultimo_t = t

    with lock:
        if time.localtime(t).tm_yday != giorno:             # mezzanotte: contatori del nuovo giorno
            giorno = time.localtime(t).tm_yday
            builder.reset_counts()
            print("Contatori azzerati (mezzanotte)")
        # lati "off": nessun veicolo (ignorati dalla regola), la pagina scrive "veicolo non presente"
        sensori = {s: {"t": t, "online": True, "distance_m": None} for s in SIDES if SOURCES_BY_SIDE[s] == "off"}
        sensori.update(letture_reali)                       # lati "real" (misure del modellino)
        if sensors_mock is not None:                        # lati "mock"
            sensors_mock.step(dt, builder.people_exposed)
            sensori.update(sensors_mock.readings(t, builder.cfg.scale))
        data = builder.build(t, sensori)
        da_inviare.update(state=codice_stato(data), counts=codice_conteggi(data["counts"]),
                          info=f"{data['state']} {data['danger_side'] or ''}")
    data["mock_sensors"] = sensors_mock is not None     # la pagina mostra la demo solo con lati simulati
    data["demo_mode"] = sensors_mock.mode if sensors_mock is not None else None
    data["sources"] = SOURCES_BY_SIDE                   # real / mock / off per lato
    data["model"] = MODEL_LABEL                         # nella barra sotto il video

    ui.send_message("frame_data", message=data)
    if data["event"]:
        e = data["event"]
        print(f"⚠️  PERICOLO: veicolo da {e['side']} a {e['speed_kmh']} km/h, {e['distance_m']} m (arresto {e['d_stop_m']} m)")

    # Video per la pagina web: stesso meccanismo dell'esempio (JPEG in base64 nel messaggio "frame")
    frame = cam.capture()
    if frame is not None:
        shape = getattr(frame, "shape", None)
        if shape is not None and len(shape) >= 2:
            frame_size[0], frame_size[1] = shape[1], shape[0]
            builder.aspect = shape[1] / shape[0]
        jpg = compress_to_jpeg(frame)
        if jpg is not None:
            ui.send_message("frame", message={"img": base64.b64encode(jpg.tobytes()).decode("ascii")})

    time.sleep(max(0.0, 1 / VIDEO_HZ - (time.time() - t)))


App.run(user_loop=ciclo)
