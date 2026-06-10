#!/bin/bash
# Crea (o aggiorna) l'icona "Neuralis.command" sul Desktop: con doppio click
# avvia tutto — server + quadro sulla TV (kiosk) + dashboard operatore — con la
# configurazione dell'evento (Muse 2 + stampante SELPHY CP1300).
#
# L'icona vive sul Desktop (fuori dal repo); questo script la rigenera quando
# serve, usando il percorso reale del progetto. Uso:  bash tools/install_desktop_icon.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ICON="$HOME/Desktop/Neuralis.command"

cat > "$ICON" <<EOF
#!/bin/bash
# Neuralis — avvio one-click per l'evento (Muse 2 + stampante SELPHY CP1300).
# Per fermare: pulsante ON-OFF nella dashboard, oppure Ctrl+C in questa finestra.
cd "$PROJECT_DIR" || { echo "Cartella Neuralis non trovata"; exit 1; }
NEURALIS_SIMULATE=0 NEURALIS_PRINTER="Canon_SELPHY_CP1300" NEURALIS_EXTRA="--board MUSE_2" ./start.sh
EOF

chmod +x "$ICON"
echo "Icona creata/aggiornata: $ICON"
echo "Progetto: $PROJECT_DIR"
echo "Doppio click sull'icona del Desktop per avviare Neuralis."
