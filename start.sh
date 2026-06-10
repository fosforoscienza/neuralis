#!/usr/bin/env bash
# Neuralis - avvio one-click per l'evento.
#
# Lancia il server, apre il VISUAL in Chrome kiosk (sulla TV) e la DASHBOARD
# operatore (sul Mac). Ctrl+C ferma tutto in modo pulito.
#
# Due modi di configurare:
#  1) Argomenti diretti al server (inoltrati cosi' come sono):
#       ./start.sh --simulate --port 8766
#       ./start.sh --simulate --printer "Canon_SELPHY"
#       ./start.sh --serial-port /dev/cu.usbmodem1411 --printer "Canon_SELPHY"
#  2) Senza argomenti: usa i default dalle variabili d'ambiente:
#       NEURALIS_SIMULATE=1        -> EEG simulato (0 = Muse reale)
#       NEURALIS_PRINTER=""        -> stampante CUPS (vuoto = salva solo PNG)
#       NEURALIS_PRINT_OPTIONS=... -> opzioni lp (default: pieno foglio 10x15 fullbleed)
#       NEURALIS_MAINS=50          -> frequenza di rete (50 IT / 60 US)
#       NEURALIS_PORT=8765         -> porta WebSocket
#       NEURALIS_EXTRA=""          -> argomenti extra al server
#
# Variabili sempre valide (anche con argomenti diretti):
#   NEURALIS_TV_POS=1728,0         -> posizione kiosk = origine della TV
#   NEURALIS_OPEN_BROWSERS=1       -> 0 per avviare solo il server
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
SIMULATE="${NEURALIS_SIMULATE:-1}"
PRINTER="${NEURALIS_PRINTER:-}"
PRINT_OPTIONS="${NEURALIS_PRINT_OPTIONS:--o media=Postcard.Fullbleed -o fit-to-page}"
MAINS="${NEURALIS_MAINS:-50}"
PORT="${NEURALIS_PORT:-8765}"
TV_POS="${NEURALIS_TV_POS:-1728,0}"
OPEN_BROWSERS="${NEURALIS_OPEN_BROWSERS:-1}"
EXTRA="${NEURALIS_EXTRA:-}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
KIOSK_PROFILE="/tmp/neuralis-kiosk-profile"

if [[ ! -x "$PYTHON" ]]; then
  echo "ERRORE: Python del venv non trovato in $PYTHON" >&2
  echo "Crea l'ambiente:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# Se l'utente passa --port a riga di comando, allinea la porta usata per
# l'attesa e per gli URL dei browser.
prev=""
for a in "$@"; do
  [[ "$prev" == "--port" ]] && PORT="$a"
  prev="$a"
done

# Argomenti del server: gli argomenti diretti hanno la precedenza; altrimenti
# si compongono dai default delle variabili d'ambiente.
if [[ $# -gt 0 ]]; then
  SERVER_ARGS=("$@")
else
  SERVER_ARGS=(--mains "$MAINS" --port "$PORT")
  [[ "$SIMULATE" == "1" ]] && SERVER_ARGS+=(--simulate)
  [[ -n "$PRINTER" ]] && SERVER_ARGS+=(--printer "$PRINTER" --print-options "$PRINT_OPTIONS")
  # shellcheck disable=SC2206
  [[ -n "$EXTRA" ]] && SERVER_ARGS+=($EXTRA)
fi

WS="ws://127.0.0.1:${PORT}"
VISUAL_URL="file://${SCRIPT_DIR}/neuralis_visual.html?ws=${WS}"
OPERATOR_URL="file://${SCRIPT_DIR}/neuralis_operator.html?ws=${WS}"

SERVER_PID=""
KIOSK_PID=""
cleanup() {
  echo ""
  echo "[neuralis] arresto..."
  [[ -n "$KIOSK_PID" ]] && kill "$KIOSK_PID" 2>/dev/null || true
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[neuralis] avvio server: ${SERVER_ARGS[*]}"
"$PYTHON" -u neuralis_server.py "${SERVER_ARGS[@]}" &
SERVER_PID=$!

# Attende che la porta WebSocket sia in ascolto (max ~10s).
echo "[neuralis] attendo la porta ${PORT} ..."
for _ in $(seq 1 40); do
  if "$PYTHON" -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1',${PORT}))==0 else 1)" 2>/dev/null; then
    break
  fi
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "ERRORE: il server si e' arrestato (porta ${PORT} occupata?)." >&2; exit 1; }
  sleep 0.25
done

if [[ "$OPEN_BROWSERS" != "1" ]]; then
  echo "[neuralis] server pronto (browser non avviati: NEURALIS_OPEN_BROWSERS=0)."
  echo "  Visual:    ${VISUAL_URL}"
  echo "  Operatore: ${OPERATOR_URL}"
else
  if [[ -x "$CHROME" ]]; then
    echo "[neuralis] apro il VISUAL in kiosk sulla TV (posizione ${TV_POS})..."
    "$CHROME" --user-data-dir="$KIOSK_PROFILE" --no-first-run --no-default-browser-check \
      --kiosk --app="$VISUAL_URL" --window-position="$TV_POS" \
      --autoplay-policy=no-user-gesture-required >/dev/null 2>&1 &
    KIOSK_PID=$!
    echo "[neuralis] apro la DASHBOARD operatore sul Mac..."
    open -a "Google Chrome" "$OPERATOR_URL"
  else
    echo "ATTENZIONE: Google Chrome non trovato. Apri manualmente:" >&2
    echo "  Visual (TV, kiosk):  $VISUAL_URL" >&2
    echo "  Operatore (Mac):     $OPERATOR_URL" >&2
  fi
fi

echo "[neuralis] sistema avviato. Premi Ctrl+C per fermare tutto."
wait "$SERVER_PID"
