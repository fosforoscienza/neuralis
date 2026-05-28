#!/usr/bin/env bash
# Neuralis — avvio one-click per l'evento.
#
# Lancia il server, apre il VISUAL in Chrome kiosk (sulla TV) e la DASHBOARD
# operatore (sul Mac). Ctrl+C ferma tutto in modo pulito.
#
# Configurazione via variabili d'ambiente (con default):
#   NEURALIS_SIMULATE=1            -> usa EEG simulato (0 = Muse reale)
#   NEURALIS_PRINTER=""           -> nome stampante CUPS (vuoto = salva solo PNG)
#   NEURALIS_MAINS=50             -> frequenza di rete (50 IT / 60 US)
#   NEURALIS_PORT=8765            -> porta WebSocket
#   NEURALIS_TV_POS=1728,0        -> posizione finestra kiosk = origine della TV
#   NEURALIS_OPEN_BROWSERS=1      -> 0 per avviare solo il server (utile nei test)
#   NEURALIS_EXTRA=""             -> argomenti extra passati al server
#                                    (es. "--serial-port /dev/cu.usbmodem1411")
#
# Esempi:
#   ./start.sh                                   # demo simulata
#   NEURALIS_SIMULATE=0 NEURALIS_PRINTER="Canon_SELPHY" ./start.sh   # evento reale
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-$SCRIPT_DIR/.venv/bin/python}"
SIMULATE="${NEURALIS_SIMULATE:-1}"
PRINTER="${NEURALIS_PRINTER:-}"
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

# Compone gli argomenti del server.
SERVER_ARGS=(--mains "$MAINS" --port "$PORT")
[[ "$SIMULATE" == "1" ]] && SERVER_ARGS+=(--simulate)
[[ -n "$PRINTER" ]] && SERVER_ARGS+=(--printer "$PRINTER")
# shellcheck disable=SC2206
[[ -n "$EXTRA" ]] && SERVER_ARGS+=($EXTRA)

SERVER_PID=""
KIOSK_PID=""
cleanup() {
  echo ""
  echo "[neuralis] arresto…"
  [[ -n "$KIOSK_PID" ]] && kill "$KIOSK_PID" 2>/dev/null || true
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "[neuralis] avvio server: ${SERVER_ARGS[*]}"
"$PYTHON" -u neuralis_server.py "${SERVER_ARGS[@]}" &
SERVER_PID=$!

# Attende che la porta WebSocket sia in ascolto (max ~10s).
echo "[neuralis] attendo la porta $PORT…"
for _ in $(seq 1 40); do
  if "$PYTHON" -c "import socket,sys; s=socket.socket(); s.settimeout(0.3); sys.exit(0 if s.connect_ex(('127.0.0.1',$PORT))==0 else 1)" 2>/dev/null; then
    break
  fi
  # Se il server e' morto durante l'avvio, esci.
  kill -0 "$SERVER_PID" 2>/dev/null || { echo "ERRORE: il server si e' arrestato." >&2; exit 1; }
  sleep 0.25
done

if [[ "$OPEN_BROWSERS" != "1" ]]; then
  echo "[neuralis] server pronto (browser non avviati: NEURALIS_OPEN_BROWSERS=0)."
  echo "  Visual:    file://$SCRIPT_DIR/neuralis_visual.html"
  echo "  Operatore: file://$SCRIPT_DIR/neuralis_operator.html"
else
  VISUAL_URL="file://$SCRIPT_DIR/neuralis_visual.html"
  OPERATOR_URL="file://$SCRIPT_DIR/neuralis_operator.html"
  if [[ -x "$CHROME" ]]; then
    echo "[neuralis] apro il VISUAL in kiosk sulla TV (posizione $TV_POS)…"
    "$CHROME" --user-data-dir="$KIOSK_PROFILE" --no-first-run --no-default-browser-check \
      --kiosk --app="$VISUAL_URL" --window-position="$TV_POS" \
      --autoplay-policy=no-user-gesture-required >/dev/null 2>&1 &
    KIOSK_PID=$!
    echo "[neuralis] apro la DASHBOARD operatore sul Mac…"
    open -a "Google Chrome" "$OPERATOR_URL"
  else
    echo "ATTENZIONE: Google Chrome non trovato. Apri manualmente:" >&2
    echo "  Visual (TV, kiosk):  $VISUAL_URL" >&2
    echo "  Operatore (Mac):     $OPERATOR_URL" >&2
  fi
fi

echo "[neuralis] sistema avviato. Premi Ctrl+C per fermare tutto."
wait "$SERVER_PID"
