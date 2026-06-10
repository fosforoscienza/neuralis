#!/usr/bin/env python3
"""Diagnostica connessione Muse via BrainFlow (test hardware "Fase 5").

Isola la sola connessione al sensore: si collega, avvia lo stream EEG (+ PPG sul
Muse 2) per qualche secondo e riporta sampling rate, ordine dei canali, ampiezza
per elettrodo e battito cardiaco stimato. Serve a validare in un colpo:
  - la connessione BLE al Muse (con o senza dongle),
  - l'ordine dei canali EEG (atteso TP9/AF7/AF8/TP10),
  - la qualita' del contatto (ampiezza plausibile per ciascun canale),
  - il sensore cardiaco PPG (config 'p50' + battito via get_heart_rate).

Non avvia ne' WebSocket ne' browser: e' un test a se' stante.

Uso:
  python tools/test_muse.py                       # Muse 2, BLE nativo
  python tools/test_muse.py --board MUSE_S
  python tools/test_muse.py --serial-port /dev/cu.usbmodem1411   # dongle BLED112
  python tools/test_muse.py --seconds 10          # piu' tempo = battito piu' stabile
"""
import argparse
import sys
import time

import numpy as np

EXPECTED = ["TP9", "AF7", "AF8", "TP10"]


def estimate_bpm(ppg_ir, fs):
    """BPM dalla frequenza dominante del PPG (FFT zero-padded), come nel server.

    Robusto su finestre corte: la get_heart_rate di BrainFlow pretende >=1024
    campioni (16 s @ 64 Hz), qui bastano pochi secondi.
    """
    x = np.asarray(ppg_ir, dtype=float)
    if len(x) < int(fs * 2):
        return None
    x = x - np.mean(x)
    if np.std(x) < 1e-6:
        return None
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n=8192))
    freqs = np.fft.rfftfreq(8192, d=1.0 / fs)
    band = (freqs >= 0.7) & (freqs <= 3.5)          # 42-210 bpm
    if not band.any() or spec[band].max() <= 0:
        return None
    return float(freqs[band][np.argmax(spec[band])] * 60.0)


def main():
    ap = argparse.ArgumentParser(description="Check connessione Muse (BrainFlow)")
    ap.add_argument("--board", default="MUSE_2", choices=["MUSE_2", "MUSE_S"])
    ap.add_argument("--serial-port", default=None, help="porta del dongle BLED112")
    ap.add_argument("--mac", default=None, help="MAC address del Muse (opzionale)")
    ap.add_argument("--seconds", type=float, default=6.0, help="durata acquisizione")
    ap.add_argument("--timeout", type=int, default=15, help="timeout scan BLE (s)")
    args = ap.parse_args()

    from brainflow.board_shim import (BoardShim, BrainFlowInputParams, BoardIds,
                                      BrainFlowPresets)
    from brainflow.exit_codes import BrainFlowError

    BoardShim.enable_dev_board_logger()   # log dettagliato della connessione

    board_map = {"MUSE_S": BoardIds.MUSE_S_BOARD, "MUSE_2": BoardIds.MUSE_2_BOARD}
    board_id = board_map[args.board].value
    ANC = BrainFlowPresets.ANCILLARY_PRESET   # preset PPG sul Muse

    params = BrainFlowInputParams()
    params.timeout = args.timeout
    if args.serial_port:
        params.serial_port = args.serial_port
    if args.mac:
        params.mac_address = args.mac

    fs = BoardShim.get_sampling_rate(board_id)
    eeg = BoardShim.get_eeg_channels(board_id)
    try:
        names = BoardShim.get_eeg_names(board_id)
    except Exception:
        names = []
    try:
        fs_ppg = BoardShim.get_sampling_rate(board_id, ANC)
        ppg = BoardShim.get_ppg_channels(board_id, ANC)
    except Exception:
        fs_ppg, ppg = 0, []
    print(f"[info] board={args.board} id={board_id} fs={fs} Hz")
    print(f"[info] righe EEG nel buffer={eeg}  nomi={names}")
    print(f"[info] PPG: canali={ppg} fs={fs_ppg} Hz")
    if args.serial_port:
        print(f"[info] connessione via dongle: {args.serial_port}")
    else:
        print("[info] connessione via BLE nativo (nessun dongle)")

    board = BoardShim(board_id, params)
    print(f"\n[1/5] prepare_session — scan BLE (timeout {args.timeout}s). "
          "Tieni il Muse acceso e vicino al Mac…")
    t0 = time.time()
    try:
        board.prepare_session()
    except BrainFlowError as e:
        print(f"\n[FAIL] connessione non riuscita: {e}")
        print("  Possibili cause:")
        print("   • Muse spento, lontano o gia' connesso ad un'altra app (Muse app sul telefono)")
        print("   • permesso Bluetooth mancante per il processo che esegue lo script")
        print("   • modello board errato (riprova con --board MUSE_S)")
        sys.exit(2)
    print(f"[ok] connesso in {time.time() - t0:.1f}s")

    # Abilita PPG (e 5° canale EEG) sul Muse: comando 'p50'. Non bloccante.
    print("[2/5] config_board('p50') — abilito il sensore cardiaco PPG…")
    try:
        board.config_board("p50")
        print("[ok] PPG abilitato")
    except BrainFlowError as e:
        print(f"[avviso] PPG non abilitato ({e}) — proseguo col solo EEG")

    print("[3/5] start_stream…")
    board.start_stream(45000)
    print(f"[4/5] acquisizione {args.seconds:.0f}s — resta fermo e rilassato…")
    time.sleep(args.seconds)
    data = board.get_board_data()                       # EEG (preset default)
    try:
        data_ppg = board.get_board_data(preset=ANC)     # PPG (preset ancillary)
    except Exception:
        data_ppg = None
    print("[5/5] stop_stream + release_session")
    board.stop_stream()
    board.release_session()

    if data.size == 0 or data.shape[1] == 0:
        print("\n[FAIL] nessun campione ricevuto: streaming assente o contatto nullo.")
        sys.exit(3)

    n = data.shape[1]
    print(f"\n[dati] righe totali={data.shape[0]}  campioni={n}  "
          f"(~{n / fs:.1f}s @ {fs}Hz, atteso ~{int(args.seconds * fs)})")

    # Etichette: usa i nomi reali di BrainFlow se disponibili.
    labels = names[:4] if len(names) >= 4 else EXPECTED
    print("\n  canale |   media |     std |     min |     max   (uV)")
    print("  -------+---------+---------+---------+---------")
    stds = []
    for i, ch in enumerate(eeg[:4]):
        x = data[ch].astype(float)
        stds.append(float(np.std(x)))
        lbl = labels[i] if i < len(labels) else f"row{ch}"
        print(f"  {lbl:>6} | {np.mean(x):7.1f} | {np.std(x):7.1f} | "
              f"{np.min(x):7.0f} | {np.max(x):7.0f}")

    # Validazione ordine canali (la nota "Fase 5" del server).
    if names[:4] == EXPECTED:
        print(f"\n[ok] ordine canali = {EXPECTED} (combacia con il server)")
    elif names:
        print(f"\n[ATTENZIONE] ordine canali BrainFlow = {names[:4]} "
              f"≠ atteso {EXPECTED}: verificare il mapping nel server.")

    # Verdetto qualita' EEG: ampiezza plausibile ~ 2..200 uV.
    alive = sum(1 for s in stds if 2.0 < s < 200.0)
    print(f"\n[verdetto EEG] {alive}/4 canali con ampiezza plausibile (std 2..200 uV)")
    if alive >= 3:
        print("[PASS] connessione e segnale EEG OK.")
    elif alive >= 1:
        print("[PARZIALE] contatto debole su alcuni canali: inumidisci/sistema la fascia.")
    else:
        print("[FLAT] segnale piatto o saturo: controlla appoggio elettrodi e accensione.")

    # --- Battito cardiaco (PPG) ---
    print("\n[PPG / battito]")
    if data_ppg is None or data_ppg.size == 0 or data_ppg.shape[1] == 0 or len(ppg) < 3:
        print("  nessun dato PPG (config 'p50' non attivo o device senza sensore cardiaco).")
        print("  Sul Muse 2 il PPG è sotto i sensori frontali: assicura buon contatto sulla fronte.")
    else:
        npp = data_ppg.shape[1]
        print(f"  campioni PPG={npp} (~{npp / max(fs_ppg, 1):.1f}s @ {fs_ppg}Hz)")
        # I 3 canali PPG del Muse sono [ambient, IR, red]; per il battito basta l'IR.
        bpm = estimate_bpm(data_ppg[ppg[1]], fs_ppg)
        if bpm and 30 <= bpm <= 220:
            print(f"  ❤️  battito stimato ≈ {bpm:.0f} bpm")
        elif bpm:
            print(f"  battito fuori range ({bpm:.0f}) — resta fermo e riprova.")
        else:
            print("  segnale PPG troppo debole per il battito — migliora il contatto frontale.")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
