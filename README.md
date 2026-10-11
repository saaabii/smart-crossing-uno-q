# BeSafe – Smart Crossing 🚦

Sistema per attraversamenti pedonali più sicuri basato su **Arduino UNO Q**, con computer vision e AI che girano **in locale sulla board**.

Progetto sviluppato per la challenge **Smart Crossing** (hackathon, ottobre 2026).

## Il problema
Agli attraversamenti pedonali molti incidenti avvengono perché pedoni, ciclisti e veicoli non si accorgono l'uno dell'altro in tempo.

## La nostra soluzione
BeSafe osserva l'attraversamento con una telecamera e:
- **riconosce** pedoni, ciclisti e veicoli
- **stima velocità e traiettorie** per prevedere un possibile conflitto *prima* che avvenga
- **segnala il pericolo** in tempo reale con avvisi luminosi e sonori
- **conta gli attraversamenti** per direzione

Il sistema è stato testato su un attraversamento reale a Torino.

## Dashboard web
L'app include un'interfaccia web in tempo reale con:
- video live con i riconoscimenti
- stato di pericolo
- soglie regolabili
- contatori degli attraversamenti

## Struttura del progetto
```
smart-crossing/
├── app.yaml            # configurazione dell'app Arduino App Lab
├── sketch/
│   └── sketch.ino      # codice del microcontrollore (sensori, LED, buzzer, display)
├── python/
│   ├── main.py         # logica principale: riconoscimento e regole di pericolo
│   ├── frame_data.py   # gestione dei dati dei frame video
│   └── config.json     # parametri e soglie
└── assets/
    ├── index.html      # dashboard
    ├── segnale.html    # pagina del segnale
    ├── app.js
    └── style.css
```

## Come avviarlo
1. Collega la Arduino UNO Q e la telecamera
2. Apri il progetto in **Arduino App Lab**
3. Premi **Run**
4. Apri la dashboard dal link mostrato da App Lab

## Tecnologie
- Arduino UNO Q (Linux + microcontrollore)
- Arduino App Lab
- Python, C++ (Arduino)
- HTML, CSS, JavaScript, Socket.IO

## Team
- Sofia Grumelli
- Sabina Frigerio
- Valerio Galasso
