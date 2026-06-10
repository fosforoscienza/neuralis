#!/usr/bin/env python3
"""Neuralis — server Python.

Acquisisce l'EEG dal Muse (via BrainFlow) o lo simula, calcola le feature
(warmth/activation/iaf/asymmetry) e la "firma" individuale, e le trasmette via
WebSocket a due client browser:

  - neuralis_visual.html  (la TV: mostra il quadro)
  - neuralis_operator.html (la dashboard sul Mac: stato + pulsanti)

Riceve dai client operatore i comandi freeze/print/clear/new_session e, alla
richiesta di stampa, fa da relay verso il visual per ottenere il PNG hi-res.

La modalità --simulate genera un EEG plausibile senza hardware: e' il modo
principale di sviluppo e di demo a freddo. DEVE restare sempre funzionante.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import numpy as np
from scipy import signal as sps

import websockets

# np.trapz e' rimosso in NumPy 2.x -> preferire np.trapezoid con fallback.
try:
    from numpy import trapezoid as _trapz
except ImportError:  # NumPy < 2.0
    from numpy import trapz as _trapz


# --------------------------------------------------------------------------- #
# Costanti
# --------------------------------------------------------------------------- #
CHANNELS = ["TP9", "AF7", "AF8", "TP10"]          # ordine canali EEG del Muse
BANDS = {                                          # bande in Hz (lo, hi)
    "delta": (1.0, 4.0),
    "theta": (4.0, 8.0),
    "alpha": (8.0, 13.0),
    "beta": (13.0, 30.0),
    "gamma": (30.0, 45.0),
}
WINDOW_SEC = 1.5            # finestra di analisi DSP
BROADCAST_HZ = 10          # frequenza di broadcast delle feature
EMA_ALPHA = 0.2            # smoothing esponenziale delle feature
MAX_WS_BYTES = 32 * 1024 * 1024   # alzato per i PNG hi-res in base64 (~MB)
SIGNATURE_AFTER_WINDOWS = 12      # finestre valide prima di fissare la firma


# --------------------------------------------------------------------------- #
# DSP
# --------------------------------------------------------------------------- #
class DSP:
    """Filtri e analisi spettrale per finestra."""

    def __init__(self, fs: int, mains: int):
        self.fs = fs
        self.mains = mains
        nyq = fs / 2.0
        # Notch sulla frequenza di rete (50 Hz IT / 60 Hz US).
        if mains < nyq:
            self.notch_b, self.notch_a = sps.iirnotch(w0=mains, Q=30.0, fs=fs)
        else:
            self.notch_b = self.notch_a = None
        # Band-pass 1-45 Hz (SOS per stabilita').
        hi = min(45.0, nyq - 1.0)
        self.bp_sos = sps.butter(4, [1.0, hi], btype="band", fs=fs, output="sos")
        self.nperseg = min(256, int(fs * WINDOW_SEC))

    def filter(self, x: np.ndarray) -> np.ndarray:
        x = x - np.mean(x)                       # de-trend (rimuove offset DC)
        if self.notch_a is not None:
            x = sps.filtfilt(self.notch_b, self.notch_a, x)
        x = sps.sosfiltfilt(self.bp_sos, x)
        return x

    def psd(self, x: np.ndarray):
        f, p = sps.welch(x, fs=self.fs, nperseg=min(self.nperseg, len(x)))
        return f, p

    @staticmethod
    def band_power(f, p, lo, hi) -> float:
        idx = (f >= lo) & (f <= hi)
        if idx.sum() < 2:
            return 0.0
        return float(_trapz(p[idx], f[idx]))


def channel_quality(x: np.ndarray) -> float:
    """Euristica 0..1 sulla bonta' del contatto di un canale.

    Senza canali ausiliari del Muse, stimiamo dalla forma del segnale:
    troppo piatto = niente contatto; troppo grande/saturo = movimento/rumore.
    Le soglie sono pensate per ampiezze tipo microvolt e andranno ritarate
    sul device reale in Fase 5.
    """
    x = np.asarray(x, dtype=float)
    x = x - np.mean(x)
    amp = float(np.std(x))
    # Frazione di campioni "railed" (saturazione).
    sat = float(np.mean(np.abs(x) > 350.0))
    if sat > 0.02:
        return max(0.0, 0.2 - sat)
    # Ampiezza plausibile EEG ~ 3..60 (unita' arbitrarie/uV).
    if amp < 1.0:
        return 0.0
    if amp < 3.0:
        return float(np.clip((amp - 1.0) / 2.0, 0.0, 1.0)) * 0.6
    if amp <= 60.0:
        return 1.0
    if amp <= 150.0:
        return float(np.clip((150.0 - amp) / 90.0, 0.0, 1.0))
    return 0.0


def estimate_bpm(ppg_ir: np.ndarray, fs: float):
    """Stima il battito (bpm) dalla frequenza dominante del PPG in banda cardiaca.

    Usa scipy (non BrainFlow) cosi' funziona identico per Muse reale e --simulate.
    Banda 0.7-3.5 Hz = 42-210 bpm. Ritorna None se il segnale non e' utilizzabile.
    """
    if ppg_ir is None or len(ppg_ir) < int(fs * 2):
        return None
    x = np.asarray(ppg_ir, dtype=float)
    x = x - np.mean(x)
    if np.std(x) < 1e-6:
        return None
    # FFT con finestra di Hann e zero-padding: risoluzione fine (~0.5 bpm),
    # contro i ~15 bpm/bin di welch su finestre corte.
    xw = x * np.hanning(len(x))
    nfft = 8192
    spec = np.abs(np.fft.rfft(xw, n=nfft))
    freqs = np.fft.rfftfreq(nfft, d=1.0 / fs)
    band = (freqs >= 0.7) & (freqs <= 3.5)        # 42-210 bpm
    if not band.any() or spec[band].max() <= 0:
        return None
    fpeak = float(freqs[band][np.argmax(spec[band])])
    return fpeak * 60.0


# --------------------------------------------------------------------------- #
# Motore delle feature
# --------------------------------------------------------------------------- #
class FeatureEngine:
    def __init__(self, fs: int, mains: int):
        self.dsp = DSP(fs, mains)
        self.warmth = 0.5
        self.activation = 0.4
        self.iaf = 10.0
        self.asymmetry = 0.0
        self.signature = None        # fissata una volta a inizio sessione
        self._valid_windows = 0

    def reset_session(self):
        """Reset firma + stato: chiamato da new_session."""
        self.signature = None
        self._valid_windows = 0

    @staticmethod
    def _make_seed(iaf: float, asym: float) -> int:
        """Seed deterministico dalla firma EEG (IAF + asimmetria)."""
        qi = int(round(iaf * 10))                 # risoluzione 0.1 Hz
        qa = int(round((asym + 1.0) * 500))       # -1..1 -> 0..1000
        return ((qi * 73856093) ^ (qa * 19349663)) & 0xFFFFFFFF

    def process(self, window: np.ndarray, quality: dict) -> None:
        """window: array (4, N). Aggiorna le feature con smoothing EMA."""
        powers_per_ch = []
        psd_post = []          # PSD dei canali posteriori (TP9/TP10) per l'IAF
        alpha_af = {}          # alpha frontale per l'asimmetria
        for i, ch in enumerate(CHANNELS):
            xf = self.dsp.filter(window[i])
            f, p = self.dsp.psd(xf)
            bp = {b: DSP.band_power(f, p, lo, hi) for b, (lo, hi) in BANDS.items()}
            powers_per_ch.append(bp)
            if ch in ("TP9", "TP10"):
                psd_post.append((f, p))
            if ch in ("AF7", "AF8"):
                alpha_af[ch] = bp["alpha"]

        # Medie di banda sui 4 canali.
        avg = {b: float(np.mean([pc[b] for pc in powers_per_ch])) for b in BANDS}
        alpha, beta, theta = avg["alpha"], avg["beta"], avg["theta"]

        # warmth = alpha/(alpha+beta): alpha-dominante -> caldo (->1).
        warmth = alpha / (alpha + beta + 1e-9)
        # activation: indice di engagement beta/(alpha+theta), limitato a [0,1].
        activation = beta / (beta + alpha + theta + 1e-9)

        # IAF: frequenza del picco alpha sui canali posteriori.
        iaf = self.iaf
        if psd_post:
            f = psd_post[0][0]
            p = np.mean([p for _, p in psd_post], axis=0)
            band = (f >= BANDS["alpha"][0]) & (f <= BANDS["alpha"][1])
            if band.any() and p[band].max() > 0:
                iaf = float(f[band][np.argmax(p[band])])

        # Asimmetria emisferica alpha AF8 (destra) vs AF7 (sinistra).
        a7 = alpha_af.get("AF7", 0.0)
        a8 = alpha_af.get("AF8", 0.0)
        asym = (a8 - a7) / (a8 + a7 + 1e-9)

        # Smoothing EMA per movimento fluido nel visual.
        self.warmth = (1 - EMA_ALPHA) * self.warmth + EMA_ALPHA * float(np.clip(warmth, 0, 1))
        self.activation = (1 - EMA_ALPHA) * self.activation + EMA_ALPHA * float(np.clip(activation, 0, 1))
        self.iaf = (1 - EMA_ALPHA) * self.iaf + EMA_ALPHA * iaf
        self.asymmetry = (1 - EMA_ALPHA) * self.asymmetry + EMA_ALPHA * float(np.clip(asym, -1, 1))

        # Firma: fissata una volta dopo qualche finestra con contatto decente.
        if self.signature is None:
            good = np.mean(list(quality.values())) if quality else 0.0
            if good >= 0.5:
                self._valid_windows += 1
                if self._valid_windows >= SIGNATURE_AFTER_WINDOWS:
                    self.signature = {
                        "seed": self._make_seed(self.iaf, self.asymmetry),
                        "iaf": round(self.iaf, 2),
                        "asymmetry": round(self.asymmetry, 3),
                    }

        self._last_powers = avg

    def snapshot(self) -> dict:
        total = sum(self._last_powers.values()) + 1e-9 if hasattr(self, "_last_powers") else 1.0
        powers = (
            {b: round(v / total, 4) for b, v in self._last_powers.items()}
            if hasattr(self, "_last_powers")
            else {b: 0.0 for b in BANDS}
        )
        return {
            "warmth": round(self.warmth, 4),
            "activation": round(self.activation, 4),
            "iaf": round(self.iaf, 2),
            "asymmetry": round(self.asymmetry, 4),
            "powers": powers,
            "signature": self.signature,
        }


# --------------------------------------------------------------------------- #
# Acquisizione
# --------------------------------------------------------------------------- #
class Acquirer:
    """Interfaccia comune. state: disconnected|connecting|connected."""

    fs = 256
    fs_ppg = 64                  # sampling rate del PPG (sensore cardiaco)

    def __init__(self):
        self.state = "disconnected"
        self.ppg_channels = None

    async def start(self):
        raise NotImplementedError

    def get_window(self, n: int) -> np.ndarray | None:
        """Restituisce un array (4, n) o None se i dati non sono pronti."""
        raise NotImplementedError

    def get_ppg_window(self, n: int) -> np.ndarray | None:
        """Array (3, n) [ambient, IR, red] o None. Default: device senza PPG."""
        return None

    async def reconnect(self):
        """Riavvia la connessione (fallback per device perso). Default: ri-start."""
        await self.start()

    def stop(self):
        pass


class SimulatedAcquirer(Acquirer):
    """Genera 4 canali EEG plausibili con stato mentale che deriva lentamente."""

    def __init__(self, mains: int):
        super().__init__()
        self.fs = 256
        self.fs_ppg = 64
        self.hr_hz = 1.1              # battito simulato ~66 bpm
        self.mains = mains
        self.t0 = time.time()
        self.iaf_true = 10.2          # picco alpha individuale "vero"
        self.phase = {ch: np.random.uniform(0, 2 * np.pi) for ch in CHANNELS}

    async def start(self):
        self.state = "connecting"
        await asyncio.sleep(1.5)       # finta fase di handshake
        self.state = "connected"
        self.t0 = time.time()

    def get_window(self, n: int) -> np.ndarray:
        if self.state != "connected":
            return None
        now = time.time()
        t = now - (np.arange(n)[::-1] / self.fs)     # tempi crescenti
        # Stato mentale lento (periodo ~45s): m alto = calmo/alpha, basso = attivo/beta.
        m = 0.5 + 0.5 * np.sin(2 * np.pi * (now - self.t0) / 45.0)
        alpha_amp = 18.0 * (0.25 + 0.75 * m)
        beta_amp = 14.0 * (0.25 + 0.75 * (1 - m))
        theta_amp = 8.0
        data = np.zeros((4, n))
        for i, ch in enumerate(CHANNELS):
            asym = 1.0
            if ch == "AF7":
                asym = 0.85
            elif ch == "AF8":
                asym = 1.15
            x = (
                alpha_amp * asym * np.sin(2 * np.pi * self.iaf_true * t + self.phase[ch])
                + beta_amp * np.sin(2 * np.pi * 20.0 * t)
                + theta_amp * np.sin(2 * np.pi * 6.0 * t)
                + 7.0 * np.random.randn(n)
            )
            # Un filo di rumore di rete per esercitare il notch.
            x += 2.0 * np.sin(2 * np.pi * self.mains * t)
            data[i] = x
        return data

    def get_ppg_window(self, n: int) -> np.ndarray | None:
        if self.state != "connected":
            return None
        now = time.time()
        t = now - (np.arange(n)[::-1] / self.fs_ppg)
        # Onda PPG plausibile: fondamentale del battito + 2a armonica, IR e red simili.
        wave = (np.sin(2 * np.pi * self.hr_hz * t)
                + 0.4 * np.sin(2 * np.pi * 2 * self.hr_hz * t))
        amb = 10.0 + 1.0 * np.random.randn(n)
        ir = 1000.0 + 50.0 * wave + 4.0 * np.random.randn(n)
        red = 900.0 + 40.0 * wave + 4.0 * np.random.randn(n)
        return np.vstack([amb, ir, red])


class MuseAcquirer(Acquirer):
    """Acquisizione reale via BrainFlow. brainflow e' importato in modo lazy."""

    def __init__(self, board: str, serial_port: str | None, mac: str | None, mains: int):
        super().__init__()
        self.board_name = board
        self.serial_port = serial_port
        self.mac = mac
        self.board = None
        self.board_id = None
        self.eeg_channels = None
        self._anc = None
        self._fail_count = 0

    async def start(self):
        self.state = "connecting"
        # Import lazy: BrainFlow non serve in --simulate.
        from brainflow.board_shim import (BoardShim, BrainFlowInputParams, BoardIds,
                                          BrainFlowPresets)

        board_map = {"MUSE_S": BoardIds.MUSE_S_BOARD, "MUSE_2": BoardIds.MUSE_2_BOARD}
        self.board_id = board_map.get(self.board_name, BoardIds.MUSE_S_BOARD).value
        self._anc = BrainFlowPresets.ANCILLARY_PRESET
        params = BrainFlowInputParams()
        if self.serial_port:
            params.serial_port = self.serial_port
        if self.mac:
            params.mac_address = self.mac

        loop = asyncio.get_event_loop()
        self.board = BoardShim(self.board_id, params)
        await loop.run_in_executor(None, self.board.prepare_session)
        # Abilita il sensore cardiaco PPG (+ 5° canale EEG): comando 'p50'. Non bloccante:
        # se fallisce, l'EEG funziona comunque e il battito resta semplicemente assente.
        try:
            await loop.run_in_executor(None, lambda: self.board.config_board("p50"))
            self.ppg_channels = BoardShim.get_ppg_channels(self.board_id, self._anc)
            self.fs_ppg = BoardShim.get_sampling_rate(self.board_id, self._anc)
            print(f"[muse] PPG abilitato (canali={self.ppg_channels}, fs={self.fs_ppg})")
        except Exception as e:
            self.ppg_channels = None
            print(f"[muse] PPG non disponibile: {e}", file=sys.stderr)
        await loop.run_in_executor(None, self.board.start_stream)
        self.fs = BoardShim.get_sampling_rate(self.board_id)
        # NB Fase 5: validare che questo ordine combaci con TP9/AF7/AF8/TP10.
        self.eeg_channels = BoardShim.get_eeg_channels(self.board_id)
        self.state = "connected"
        print(f"[muse] connesso (fs={self.fs}, canali EEG={self.eeg_channels})")

    def get_window(self, n: int) -> np.ndarray | None:
        if self.board is None:
            return None
        try:
            raw = self.board.get_current_board_data(n)
            if raw.shape[1] < n:
                return None
            data = np.vstack([raw[c] for c in self.eeg_channels[:4]])
            self._fail_count = 0
            self.state = "connected"
            return data
        except Exception as e:           # disconnessione a runtime -> non crashare
            self._fail_count += 1
            if self._fail_count > 5:
                self.state = "connecting"
            print(f"[muse] lettura fallita: {e}", file=sys.stderr)
            return None

    def get_ppg_window(self, n: int) -> np.ndarray | None:
        if self.board is None or not self.ppg_channels:
            return None
        try:
            raw = self.board.get_current_board_data(n, self._anc)
            if raw.shape[1] < int(self.fs_ppg):        # meno di ~1s di PPG
                return None
            return np.vstack([raw[c] for c in self.ppg_channels[:3]])
        except Exception:
            return None

    async def reconnect(self):
        """Hard reconnect BLE: chiude e riapre la sessione (fallback device perso)."""
        self.state = "connecting"
        loop = asyncio.get_event_loop()
        if self.board is not None:
            for fn in (self.board.stop_stream, self.board.release_session):
                try:
                    await loop.run_in_executor(None, fn)
                except Exception:
                    pass
            self.board = None
        self._fail_count = 0
        await self.start()

    def stop(self):
        if self.board is not None:
            try:
                self.board.stop_stream()
                self.board.release_session()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Hub WebSocket (client, broadcast, comandi, relay immagine)
# --------------------------------------------------------------------------- #
class Hub:
    def __init__(self, printer: str | None, output_dir: str, print_options=None):
        self.clients = {}            # websocket -> role ("visual"|"operator"|"?")
        self.frozen = False
        self.printer = printer
        self.print_options = print_options or []
        self.output_dir = output_dir
        self.engine_snapshot = {}
        self.muse_state = "disconnected"
        self.heart_rate = None

    # --- gestione client --------------------------------------------------- #
    async def register(self, ws):
        self.clients[ws] = "?"
        await ws.send(json.dumps(self._features_msg()))   # stato iniziale

    async def unregister(self, ws):
        self.clients.pop(ws, None)

    def _visual_clients(self):
        return [ws for ws, role in self.clients.items() if role == "visual"]

    def _features_msg(self) -> dict:
        msg = {
            "type": "features",
            "muse_state": self.muse_state,
            "frozen": self.frozen,
            "signal_quality": getattr(self, "signal_quality", {c: 0.0 for c in CHANNELS}),
            "heart_rate": self.heart_rate,
            "ts": int(time.time() * 1000),
        }
        msg.update(self.engine_snapshot)
        return msg

    async def broadcast_features(self):
        if not self.clients:
            return
        data = json.dumps(self._features_msg())
        await self._send_many(list(self.clients.keys()), data)

    async def _send_many(self, targets, data: str):
        dead = []
        for ws in targets:
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unregister(ws)

    # --- comandi ----------------------------------------------------------- #
    async def handle_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = msg.get("type")
        cmd = msg.get("cmd")

        if mtype == "hello":
            self.clients[ws] = msg.get("role", "?")
            print(f"[ws] client identificato come '{self.clients[ws]}'")
            return

        if mtype == "artwork_png":
            await self._on_artwork(msg)
            return

        if mtype == "print_unavailable":
            await self._notify_operators({"type": "print_result", "ok": False,
                                          "error": msg.get("reason", "Nessuna immagine da stampare")})
            return

        if cmd == "freeze":
            self.frozen = not self.frozen
            print(f"[cmd] freeze -> {self.frozen}")
            await self._send_many(self._visual_clients(),
                                  json.dumps({"cmd": "freeze", "value": self.frozen}))
        elif cmd == "clear":
            print("[cmd] clear")
            await self._send_many(self._visual_clients(), json.dumps({"cmd": "clear"}))
        elif cmd == "new_session":
            print("[cmd] new_session (reset firma)")
            self.frozen = False
            if self.on_new_session:
                self.on_new_session()
            await self._send_many(self._visual_clients(), json.dumps({"cmd": "new_session"}))
        elif cmd == "anim_mode":
            mode = msg.get("value", "flowfield")
            print(f"[cmd] anim_mode -> {mode}")
            await self._send_many(self._visual_clients(),
                                  json.dumps({"cmd": "anim_mode", "value": mode}))
        elif cmd == "print":
            print("[cmd] print -> richiesta export hi-res al visual")
            vis = self._visual_clients()
            if not vis:
                await self._notify_operators({"type": "print_result", "ok": False,
                                              "error": "Nessun client visual connesso"})
            else:
                await self._send_many(vis, json.dumps({"cmd": "export"}))
        elif cmd == "reconnect":
            print("[cmd] reconnect — riavvio connessione sensore")
            if self.on_reconnect:
                asyncio.create_task(self._safe_reconnect())
        elif cmd == "shutdown":
            print("[cmd] shutdown — arresto del sistema")
            # Avvisa i browser (chiudono le finestre), poi termina il processo:
            # start.sh, vedendo il server uscire, chiude anche il kiosk sulla TV.
            await self._send_many(list(self.clients.keys()),
                                  json.dumps({"cmd": "shutdown"}))
            await asyncio.sleep(0.4)
            if self.on_shutdown:
                self.on_shutdown()

    on_new_session = None
    on_reconnect = None
    on_shutdown = None

    async def _safe_reconnect(self):
        try:
            await self.on_reconnect()
        except Exception as e:
            print(f"[reconnect] fallito: {e}", file=sys.stderr)

    async def _notify_operators(self, msg: dict):
        ops = [ws for ws, role in self.clients.items() if role == "operator"]
        await self._send_many(ops, json.dumps(msg))

    async def _on_artwork(self, msg: dict):
        """Riceve il PNG hi-res dal visual: salva su file (stampa = Fase 4)."""
        b64 = msg.get("data", "")
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        try:
            png = base64.b64decode(b64)
        except Exception as e:
            await self._notify_operators({"type": "print_result", "ok": False,
                                          "error": f"PNG non valido: {e}"})
            return
        os.makedirs(self.output_dir, exist_ok=True)
        fname = os.path.join(self.output_dir,
                             f"neuralis_{datetime.now():%Y%m%d_%H%M%S}.png")
        with open(fname, "wb") as f:
            f.write(png)
        size_kb = len(png) // 1024
        print(f"[artwork] PNG salvato: {fname} ({size_kb} KB)")
        printed, job, perr = await self._print_file(fname)
        if printed:
            print(f"[stampa] inviato a '{self.printer}' (job {job})")
        elif self.printer:
            print(f"[stampa] FALLITA su '{self.printer}': {perr} -> fallback su file",
                  file=sys.stderr)
        else:
            print("[stampa] nessuna stampante configurata: salvato solo su file")
        await self._notify_operators({"type": "print_result", "ok": True,
                                      "saved": fname, "printer": self.printer,
                                      "printed": printed, "job": job, "error": perr})

    async def _print_file(self, path: str):
        """Stampa via CUPS `lp`. Ritorna (printed, job_id, error).

        Il PNG e' gia' stato salvato: se la stampa fallisce il file resta come
        fallback non bloccante (l'evento non si interrompe).
        """
        if not self.printer:
            return (False, None, None)
        args = ["lp", "-d", self.printer, *self.print_options, os.path.abspath(path)]
        # LC_ALL=C per output prevedibile; il job id si estrae comunque via regex
        # (CUPS localizza il messaggio, es. in italiano "L'id richiesto e' ...").
        env = dict(os.environ, LC_ALL="C", LANG="C")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            if proc.returncode == 0:
                txt = out.decode(errors="replace").strip()
                m = re.search(r"(\S+-\d+)", txt)     # es. "Pantum_...-27"
                return (True, m.group(1) if m else None, None)
            msg = err.decode(errors="replace").strip()
            # CUPS riporta un criptico "No such file or directory" quando la
            # destinazione non esiste: rendiamolo comprensibile all'operatore.
            if "No such file or directory" in msg:
                msg = f"stampante '{self.printer}' non trovata o non disponibile"
            return (False, None, msg or f"lp uscita {proc.returncode}")
        except FileNotFoundError:
            return (False, None, "comando lp non trovato (CUPS)")
        except Exception as e:
            return (False, None, str(e))


# --------------------------------------------------------------------------- #
# Loop DSP + main
# --------------------------------------------------------------------------- #
async def dsp_loop(hub: Hub, acq: Acquirer, engine: FeatureEngine):
    win = int(acq.fs * WINDOW_SEC)
    period = 1.0 / BROADCAST_HZ
    bpm_ema = None
    last_bpm = 0.0
    while True:
        t0 = time.perf_counter()
        hub.muse_state = acq.state
        window = acq.get_window(win)
        if window is not None:
            quality = {CHANNELS[i]: round(channel_quality(window[i]), 3) for i in range(4)}
            engine.process(window, quality)
            hub.signal_quality = quality
            hub.engine_snapshot = engine.snapshot()
        # Battito: stima ~1 Hz su una finestra PPG di ~5 s, con smoothing EMA.
        if t0 - last_bpm >= 1.0:
            last_bpm = t0
            if acq.state != "connected":
                bpm_ema = None
                hub.heart_rate = None
            else:
                ppg = acq.get_ppg_window(int(acq.fs_ppg * 5))
                if ppg is not None and ppg.shape[0] >= 2:
                    bpm = estimate_bpm(ppg[1], acq.fs_ppg)        # riga 1 = canale IR
                    if bpm is not None:
                        bpm_ema = bpm if bpm_ema is None else 0.7 * bpm_ema + 0.3 * bpm
                        hub.heart_rate = int(round(bpm_ema))
        await hub.broadcast_features()
        dt = time.perf_counter() - t0
        await asyncio.sleep(max(0.0, period - dt))


async def main():
    ap = argparse.ArgumentParser(description="Neuralis — server EEG -> arte")
    ap.add_argument("--simulate", action="store_true", help="genera EEG finto (no hardware)")
    ap.add_argument("--board", default="MUSE_S", choices=["MUSE_S", "MUSE_2"])
    ap.add_argument("--serial-port", default=None, help="porta del dongle BLED112")
    ap.add_argument("--mac", default=None, help="MAC address del Muse")
    ap.add_argument("--mains", type=int, default=50, choices=[50, 60], help="frequenza di rete")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--printer", default=None, help="nome stampante CUPS")
    ap.add_argument("--print-options", default="",
                    help='opzioni extra per lp, es. "-o media=Postcard -o fit-to-page"')
    ap.add_argument("--output-dir", default="output")
    args = ap.parse_args()

    # Silenzia i traceback innocui di handshake (es. una sonda TCP che apre la
    # porta senza completare l'handshake WebSocket): non sono errori del server.
    logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

    if args.simulate:
        acq = SimulatedAcquirer(mains=args.mains)
        print("[neuralis] modalita' SIMULATE")
    else:
        acq = MuseAcquirer(args.board, args.serial_port, args.mac, args.mains)
        print(f"[neuralis] modalita' MUSE ({args.board})")

    await acq.start()
    engine = FeatureEngine(fs=acq.fs, mains=args.mains)
    hub = Hub(printer=args.printer, output_dir=args.output_dir,
              print_options=args.print_options.split())
    hub.on_new_session = engine.reset_session
    hub.on_reconnect = acq.reconnect

    def _shutdown():
        print("[neuralis] arresto richiesto dall'operatore.")
        try:
            acq.stop()                         # rilascia il sensore (BLE)
        except Exception:
            pass
        os._exit(0)                            # termina -> start.sh chiude il kiosk
    hub.on_shutdown = _shutdown

    if args.printer:
        chk = await asyncio.create_subprocess_exec(
            "lpstat", "-p", args.printer,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await chk.wait()
        if chk.returncode == 0:
            print(f"[stampa] stampante '{args.printer}' trovata.")
        else:
            print(f"[stampa] ATTENZIONE: stampante '{args.printer}' non trovata; "
                  f"i quadri saranno salvati in '{args.output_dir}/' come fallback.")

    async with websockets.serve(
        lambda ws: ws_handler(hub, ws), args.host, args.port, max_size=MAX_WS_BYTES
    ):
        print(f"[neuralis] WebSocket su ws://{args.host}:{args.port}")
        try:
            await dsp_loop(hub, acq, engine)
        finally:
            acq.stop()


async def ws_handler(hub: Hub, websocket):
    await hub.register(websocket)
    try:
        async for raw in websocket:
            await hub.handle_message(websocket, raw)
    except websockets.ConnectionClosed:
        pass
    finally:
        await hub.unregister(websocket)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[neuralis] arresto.")
