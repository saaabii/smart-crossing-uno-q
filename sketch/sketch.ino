// BeSafe — sketch MCU dell'Arduino UNO Q.
//
// Sensore (fase 2): un Modulino Distance (VL53L4CD) è il lato NORD. Letture a frequenza fissa, velocità di
// avvicinamento con regressione lineare sulle ultime N letture, decelerazione filtrata con EMA. Tutto in
// unità del modellino (mm, mm/s, mm/s²): la scala la applica Linux.
//   get_sensor(lato) → String "d,v,a"   d distanza mm (-1 = nessun veicolo), v mm/s (positiva = si avvicina),
//                                        a mm/s² (positiva = frena); "nd" = lato senza sensore o sensore guasto
//
// Uscite (SPEC.md): LED rossi lampeggianti pin 3 e 4 (pin 4: 3 LED aggiuntivi), buzzer a intermittenza pin 5,
// Grove LCD RGB 16x2.
// Lo stato lo decide Linux (main.py, con l'isteresi di 2 s) e lo manda via Bridge:
//   set_state(codice)   codice = stato*10 + lato   stato 0 RIPOSO, 1 ATTENZIONE, 2 PERICOLO
//                                                  lato 0 N, 1 S, 2 E, 3 O, 9 nessuno
//   set_counts(codice)  codice = pedoni*1000 + ciclisti rilevati (per l'LCD in RIPOSO)
//   set_alert(0|1)      compatibilità con la versione precedente: 1 = PERICOLO senza lato
//
// NOTA: nessuna funzione deve stare prima di "struct Stima", altrimenti Arduino genera i prototipi
// automatici prima della struct e la compilazione fallisce.
#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include <Modulino.h>
#include "rgb_lcd.h"

// più pin LED, comandati insieme
const int PIN_LED[] = {3, 4};
const int N_LED = sizeof(PIN_LED) / sizeof(PIN_LED[0]);
const int PIN_BUZZER = 5;

// ---- sensore
const int LATO_SENSORE = 0;          // il Modulino è il lato NORD (0 N, 1 S, 2 E, 3 O)
const int FREQ_HZ = 25;              // letture al secondo: il VL53L4CD misura ogni 20 ms (max ~50 Hz),
                                     // sotto i 50 Hz ogni lettura è una misura nuova
const int RANGE_MAX_MM = 1000;       // oltre: nessun veicolo
const int N_REGRESSIONE = 5;         // letture usate per la velocità
const float ALPHA_DECEL = 0.3;       // EMA della decelerazione
const int DECEL_BASE_MS = 500;       // la decelerazione confronta la velocità con quella di 0,5 s prima:
                                     // con 160 ms il rumore (±3 mm) dava falsi "frena" fino a 4 m/s² reali
const unsigned long STAMPA_MS = 200; // stampa in console per il test a mano

// ===== STIMA: inizio (C++ puro, compilato e testato sul Mac da tests/test_sketch_stima.py) =====
const int STIMA_MAX = 32;
const unsigned long STIMA_BUCO_MS = 300;   // letture valide più distanti di così: si riparte da capo

struct Stima {
  int n;                                   // letture per la regressione
  float alpha;                             // EMA della decelerazione
  unsigned long base_ms;                   // intervallo su cui si misura la variazione di velocità
  unsigned long t[STIMA_MAX];              // istanti delle ultime letture valide (ms), dalla più vecchia
  float d[STIMA_MAX];                      // distanze (mm)
  int count;
  unsigned long tv[STIMA_MAX];             // istanti delle ultime stime di velocità
  float vh[STIMA_MAX];                     // ultime stime di velocità (mm/s)
  int count_v;
  float distanza;                          // mm, -1 = nessun veicolo
  float v;                                 // mm/s, positiva = si avvicina
  float a;                                 // mm/s², positiva = frena
};

void stima_reset(Stima& s) {
  s.count = 0;
  s.count_v = 0;
  s.distanza = -1;
  s.v = 0;
  s.a = 0;
}

void stima_init(Stima& s, int n, float alpha, unsigned long base_ms) {
  s.n = n < 2 ? 2 : (n > STIMA_MAX ? STIMA_MAX : n);
  s.alpha = alpha;
  s.base_ms = base_ms;
  stima_reset(s);
}

// Lettura valida: distanza d_mm all'istante t_ms. (Le letture non valide o fuori portata chiamano stima_reset.)
void stima_aggiungi(Stima& s, unsigned long t_ms, float d_mm) {
  if (s.count > 0 && t_ms - s.t[s.count - 1] > STIMA_BUCO_MS) stima_reset(s);
  if (s.count == s.n) {
    for (int i = 1; i < s.n; i++) { s.t[i - 1] = s.t[i]; s.d[i - 1] = s.d[i]; }
    s.count--;
  }
  s.t[s.count] = t_ms;
  s.d[s.count] = d_mm;
  s.count++;
  s.distanza = d_mm;
  if (s.count < s.n) return;               // servono n letture per la velocità

  // Regressione lineare d = q + m·t sulle ultime n letture (t in s dalla più vecchia): v = -m
  float tm = 0, dm = 0;
  for (int i = 0; i < s.n; i++) { tm += (s.t[i] - s.t[0]) / 1000.0f; dm += s.d[i]; }
  tm /= s.n;
  dm /= s.n;
  float num = 0, den = 0;
  for (int i = 0; i < s.n; i++) {
    float dt = (s.t[i] - s.t[0]) / 1000.0f - tm;
    num += dt * (s.d[i] - dm);
    den += dt * dt;
  }
  if (den <= 0) return;
  float v_nuova = -num / den;

  // Decelerazione: variazione della velocità rispetto alla stima di base_ms fa, poi EMA.
  // Positiva se la velocità di avvicinamento cala.
  if (s.count_v == STIMA_MAX) {
    for (int i = 1; i < STIMA_MAX; i++) { s.tv[i - 1] = s.tv[i]; s.vh[i - 1] = s.vh[i]; }
    s.count_v--;
  }
  s.tv[s.count_v] = t_ms;
  s.vh[s.count_v] = v_nuova;
  s.count_v++;
  while (s.count_v >= 2 && t_ms - s.tv[1] >= s.base_ms) {   // tiene la stima più recente vecchia almeno base_ms
    for (int i = 1; i < s.count_v; i++) { s.tv[i - 1] = s.tv[i]; s.vh[i - 1] = s.vh[i]; }
    s.count_v--;
  }
  if (t_ms - s.tv[0] >= s.base_ms) {
    float a_grezza = -(v_nuova - s.vh[0]) / ((t_ms - s.tv[0]) / 1000.0f);
    s.a = s.alpha * a_grezza + (1 - s.alpha) * s.a;
  }
  s.v = v_nuova;
}
// ===== STIMA: fine =====

ModulinoDistance distance;
rgb_lcd lcd;
Stima stima;
bool sensore_ok = false;
unsigned long t_lettura = 0, t_stampa = 0, t_statistica = 0;
int letture_s = 0, ripetute_s = 0;          // statistiche dell'ultimo secondo, per sapere quanto regge il sensore
int letture_n = 0, ripetute_n = 0;
float d_precedente = -2;

// ---- stato ricevuto da Linux
const int RIPOSO = 0, ATTENZIONE = 1, PERICOLO = 2;
const char* LATI[] = {"NORD", "SUD", "EST", "OVEST"};
volatile int codice_stato = 9;         // RIPOSO, nessun lato
volatile int codice_conteggi = 0;
int stato_mostrato = -1;
int conteggi_mostrati = -1;
bool lampeggio_on = false;
unsigned long t_lampeggio = 0;

void mostra_conteggi();

void set_state(int codice) { codice_stato = codice; }
void set_counts(int codice) { codice_conteggi = codice; }
void set_alert(int stato) { codice_stato = stato ? PERICOLO * 10 + 9 : 9; }

// "d,v,a" in interi (mm, mm/s, mm/s²); "nd" se il lato non ha un sensore funzionante
String get_sensor(int lato) {
  if (lato != LATO_SENSORE || !sensore_ok) return String("nd");
  return String((int)stima.distanza) + "," + String((int)stima.v) + "," + String((int)stima.a);
}

void leggi_sensore() {
  unsigned long ora = millis();
  if (ora - t_lettura < 1000 / FREQ_HZ) return;
  t_lettura = ora;
  if (!sensore_ok) return;
  letture_n++;
  if (distance.available()) {              // false se la misura non è valida (range_status != 0)
    float d = distance.get();
    if (d == d_precedente) ripetute_n++;   // stessa identica misura: forse il sensore non ne ha di nuove
    d_precedente = d;
    if (d > 0 && d <= RANGE_MAX_MM) {
      stima_aggiungi(stima, ora, d);
      return;
    }
  }
  stima_reset(stima);                      // fuori portata o non valida: nessun veicolo
}

void stampa_sensore() {
  unsigned long ora = millis();
  if (ora - t_statistica >= 1000) {
    letture_s = letture_n; ripetute_s = ripetute_n;
    letture_n = ripetute_n = 0;
    t_statistica = ora;
  }
  if (ora - t_stampa < STAMPA_MS) return;
  t_stampa = ora;
  if (!sensore_ok) {
    Monitor.println("NORD: sensore non trovato");
    return;
  }
  Monitor.print("NORD d=");
  Monitor.print((int)stima.distanza);
  Monitor.print(" mm  v=");
  Monitor.print((int)stima.v);
  Monitor.print(" mm/s  a=");
  Monitor.print((int)stima.a);
  Monitor.print(" mm/s2  | ");
  Monitor.print(letture_s);
  Monitor.print(" letture/s, ripetute ");
  Monitor.println(ripetute_s);
}

// Seconda riga dell'LCD: "Veicolo da NORD" (mai "Auto": il sensore non classifica)
void riga_lato(int lato) {
  lcd.setCursor(0, 1);
  if (lato >= 0 && lato <= 3) {
    lcd.print("Veicolo da ");
    lcd.print(LATI[lato]);
  } else {
    lcd.print("Veicolo vicino");
  }
}

void mostra_stato(int stato, int lato) {
  lcd.clear();
  lcd.setCursor(0, 0);
  if (stato == PERICOLO) {
    lcd.setRGB(255, 0, 0);             // rosso
    lcd.print("    WARNING");
    riga_lato(lato);
  } else if (stato == ATTENZIONE) {
    lcd.setRGB(255, 150, 0);           // giallo
    lcd.print("   ATTENZIONE");
    riga_lato(lato);
  } else {
    lcd.setRGB(0, 200, 60);            // verde
    lcd.print("     BeSafe");
    mostra_conteggi();
  }
}

// RIPOSO: "Ped 12   Cic 3"
void mostra_conteggi() {
  int pedoni = codice_conteggi / 1000;
  int ciclisti = codice_conteggi % 1000;
  lcd.setCursor(0, 1);
  lcd.print("                ");
  lcd.setCursor(0, 1);
  lcd.print("Ped ");
  lcd.print(pedoni);
  lcd.print("   Cic ");
  lcd.print(ciclisti);
  conteggi_mostrati = codice_conteggi;
}

// Accende o spegne tutti i LED insieme (pin 3 e pin 4)
void scriviLed(int valore) {
  for (int i = 0; i < N_LED; i++) {
    digitalWrite(PIN_LED[i], valore);
  }
}

void aggiorna_segnalazione() {
  int codice = codice_stato;
  int stato = codice / 10;
  int lato = codice % 10;
  if (codice != stato_mostrato) {
    mostra_stato(stato, lato);
    stato_mostrato = codice;
  } else if (stato == RIPOSO && codice_conteggi != conteggi_mostrati) {
    mostra_conteggi();
  }

  if (stato == PERICOLO) {
    // LED e buzzer a intermittenza (200 ms)
    if (millis() - t_lampeggio > 200) {
      lampeggio_on = !lampeggio_on;
      t_lampeggio = millis();
      scriviLed(lampeggio_on ? HIGH : LOW);
      if (lampeggio_on) tone(PIN_BUZZER, 2000); else noTone(PIN_BUZZER);
    }
  } else {
    scriviLed(LOW);
    noTone(PIN_BUZZER);
    lampeggio_on = false;
  }
}

void setup() {
  for (int i = 0; i < N_LED; i++) {
    pinMode(PIN_LED[i], OUTPUT);
  }
  scriviLed(LOW);
  pinMode(PIN_BUZZER, OUTPUT);
  lcd.begin(16, 2);
  mostra_stato(RIPOSO, 9);
  Monitor.begin(115200);
  Modulino.begin();
  sensore_ok = distance.begin();
  stima_init(stima, N_REGRESSIONE, ALPHA_DECEL, DECEL_BASE_MS);
  Bridge.begin();
  Bridge.provide("set_state", set_state);
  Bridge.provide("set_counts", set_counts);
  Bridge.provide("set_alert", set_alert);
  Bridge.provide_safe("get_sensor", get_sensor);   // nel thread del loop: legge la stima sempre intera
}

void loop() {
  leggi_sensore();
  stampa_sensore();
  aggiorna_segnalazione();
}