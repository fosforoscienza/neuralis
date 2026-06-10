#!/bin/bash
# Crea (o aggiorna) sul Desktop l'icona "Neuralis - Stop.command": un kill switch
# che termina TUTTI i processi Neuralis — il server e le finestre del browser
# (TV kiosk + dashboard) — utile se qualcosa si blocca e il pulsante On-Off non
# risponde. Mirato sui processi Neuralis (server + profili Chrome dedicati): NON
# chiude il Chrome personale.  Uso:  bash tools/install_stop_icon.sh
set -euo pipefail

ICON="$HOME/Desktop/Neuralis - Stop.command"

cat > "$ICON" <<'EOF'
#!/bin/bash
# Neuralis — STOP forzato: chiude server e finestre del browser (TV + dashboard).
echo "Chiusura forzata di Neuralis…"
for p in neuralis_server.py neuralis-kiosk-profile neuralis-dashboard-profile \
         neuralis_visual.html neuralis_operator.html; do
  if pkill -f "$p" 2>/dev/null; then echo "  terminato: $p"; fi
done
echo "Fatto — Neuralis è stato chiuso. Puoi chiudere questa finestra."
EOF

chmod +x "$ICON"
echo "Icona creata/aggiornata: $ICON"
echo "Doppio click sul Desktop per fermare Neuralis in qualsiasi momento."
