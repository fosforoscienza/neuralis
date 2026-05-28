# Neuralis

> Brain waves, real art.

Installazione artistica da evento: un sensore EEG **Muse** legge l'attività
cerebrale in tempo reale e la traduce in un **quadro astratto generativo**
mostrato su una **TV 55"** e **stampabile** a fine sessione. Replica concettuale
del prodotto commerciale **BrainArt®**.

## Architettura

Un **server Python** parla via **WebSocket** con due pagine browser:

```
        server Python (Muse/--simulate + DSP + WebSocket + stampa CUPS)
          │
   ┌──────┴───────┐
   ▼              ▼
neuralis_visual.html      neuralis_operator.html
   (TV, solo quadro)         (dashboard sul Mac)
```

- **`neuralis_server.py`** — acquisizione Muse via BrainFlow (o `--simulate`),
  DSP (notch, band-pass, PSD/bande), calcolo feature, firma individuale,
  broadcast WebSocket ~10 Hz, ricezione comandi, stampa via `lp` (CUPS).
- **`neuralis_visual.html`** — visual p5.js fullscreen, nessun HUD; mappa le
  feature in colore/linee/spirale; export PNG hi-res su comando.
- **`neuralis_operator.html`** — stato Muse, qualità segnale per canale,
  parametri live, pulsanti operatore (CONGELA → STAMPA, NUOVA SESSIONE, PULISCI).

## Mapping segnale → immagine

| Visivo | Significato | Feature EEG |
|---|---|---|
| Colore caldo ↔ freddo | emotivo/spontaneo ↔ analitico | rapporto alpha/beta (`warmth`) |
| Linee distese ↔ serrate | bassa ↔ alta attivazione | engagement beta/(alpha+theta) (`activation`) |
| Spirale/vortici (struttura) | "firma" individuale stabile | seed da IAF + asimmetria AF7/AF8 |

## Requisiti

- macOS (Apple Silicon), Python 3.10+
- Muse S Gen 2 / Muse 2 + dongle BLED112 (solo per l'hardware reale)
- Stampante fotografica via CUPS (es. Canon SELPHY); in assenza, fallback su PNG

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Avvio one-click

```bash
./start.sh                 # demo simulata (server + visual in kiosk + dashboard)
```

Per l'evento reale (Muse + stampante):

```bash
NEURALIS_SIMULATE=0 NEURALIS_PRINTER="Canon_SELPHY" ./start.sh
```

`start.sh` avvia il server, apre il **visual in Chrome kiosk** sulla TV e la
**dashboard** sul Mac, e ferma tutto in modo pulito con Ctrl+C. Variabili utili:

| Variabile | Default | Significato |
|---|---|---|
| `NEURALIS_SIMULATE` | `1` | `0` = Muse reale |
| `NEURALIS_PRINTER` | _(vuota)_ | nome stampante CUPS; vuota = salva solo PNG |
| `NEURALIS_MAINS` | `50` | frequenza di rete (50 IT / 60 US) |
| `NEURALIS_TV_POS` | `1728,0` | origine della finestra kiosk = posizione della TV |
| `NEURALIS_OPEN_BROWSERS` | `1` | `0` = avvia solo il server |
| `NEURALIS_EXTRA` | _(vuota)_ | argomenti extra al server (es. `--serial-port /dev/cu.usbmodemXXX`) |

## Avvio manuale

```bash
source .venv/bin/activate
python neuralis_server.py --simulate            # oppure: --printer "<Nome>" per stampare
```

Poi aprire `neuralis_operator.html` sul Mac e `neuralis_visual.html` sulla TV
(in Chrome kiosk). Le pagine accettano `?ws=ws://host:porta` per puntare a un
server diverso da `ws://127.0.0.1:8765`.

## Stato del progetto

Completo e funzionante in `--simulate`: server, dashboard, visual e
stampa/salvataggio del quadro. Resta da validare l'**hardware Muse reale**
(ordine canali EEG e soglie di qualità) con device + dongle BLED112 collegati.

Decisioni: stampa a due pulsanti (CONGELA → STAMPA), anteprima solo su TV,
stampa 10×15 cm orizzontale @300 DPI, notch 50 Hz (rete IT).
