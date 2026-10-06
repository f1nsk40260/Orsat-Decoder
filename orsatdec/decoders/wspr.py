"""WSPR (K1JT) : balises de propagation, créneaux de 2 minutes (minutes paires UTC), 4-FSK à 1,46 baud dans
200 Hz autour de 1500 Hz, code convolutif K=32 décodé par Fano. Le décodage est fait par wsprd (K1JT, K9AN,
VA2GKA, GPL-3), compilé avec Orsat-Decoder (dossier native/wspr). L'audio du créneau est ramené en bande de
base à 375 Hz (1500 Hz -> 0 Hz) puis confié au décodeur natif."""
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import firwin, lfilter

from ..dsp import Decoder

log = logging.getLogger("orsat.wspr")
NATIVE = Path(os.environ.get("ORSAT_NATIVE", Path(__file__).resolve().parents[2] / "native" / "bin"))
SLOT = 120.0
SR = 375.0


def to_iq(audio, fs):
    """Audio réel (fs) -> bande de base complexe à 375 Hz, 1500 Hz ramené à 0."""
    t = np.arange(len(audio)) / fs
    z = audio * np.exp(-2j * np.pi * 1500.0 * t)
    D = int(round(fs / SR))
    h = firwin(int(8 * D) | 1, 170.0, fs=fs)
    z = lfilter(h, 1.0, z)[::D]
    if abs(fs / D - SR) > 0.5:
        from scipy.signal import resample
        z = resample(z, int(len(z) * SR * D / fs))
    return z


def decode_iq(z, binary=None, conj=False):
    """Bande de base (375 Hz) -> liste de dict (snr, dt, freq audio, dérive, indicatif, locator, puissance)."""
    binary = Path(binary or NATIVE / "wspr_decode_iq").resolve()
    z = np.asarray(z, np.complex64)[: int(SR * SLOT)]
    if conj:
        z = np.conj(z)
    peak = np.max(np.abs(z)) + 1e-12
    z = z / peak * 0.5
    iq = np.empty(2 * len(z), np.float32)
    iq[0::2], iq[1::2] = z.real, z.imag
    with tempfile.TemporaryDirectory() as d:          # wsprd écrit un fichier dans le dossier courant
        p = Path(d) / "slot.iq"
        iq.tofile(p)
        r = subprocess.run([str(binary), str(p)], capture_output=True, text=True, timeout=90, cwd=d)
    out = []
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 7:
            try:
                out.append({"snr": float(f[0]), "dt": float(f[1]) + 1.0, "freq": float(f[2]), "drift": float(f[3]),
                            "call": f[4], "loc": f[5], "pwr": f[6]})
            except ValueError:
                pass
    return out


class WSPR(Decoder):
    name = "WSPR"
    kind = "msg"

    def __init__(self, fs, af=1500.0, latency=0.6):
        super().__init__(fs, af)
        self.latency = latency
        self.buf = []
        self.slot = None
        self.lock = threading.Lock()
        self.results = []
        self.last_count = 0
        self.binary = NATIVE / "wspr_decode_iq"

    def status(self):
        return {"af": 1500.0, "slot": SLOT, "last": self.last_count, "ready": self.binary.exists()}

    def _slot_of(self, t):
        return int((t - self.latency) // SLOT)

    def process(self, x):
        now = time.time()
        slot = self._slot_of(now)
        if self.slot is None:
            self.slot = slot
        if slot != self.slot:
            audio = np.concatenate(self.buf) if self.buf else np.zeros(0)
            start = self.slot * SLOT
            self.buf = []
            self.slot = slot
            if len(audio) > self.fs * 100:
                threading.Thread(target=self._decode, args=(audio, start), daemon=True).start()
        self.buf.append(np.asarray(x, np.float64))
        with self.lock:
            res, self.results = self.results, []
        return res

    def _decode(self, audio, start):
        if not self.binary.exists():
            with self.lock:
                self.results.append({"t": "msg", "text": "décodeur WSPR absent (native/bin/wspr_decode_iq)", "error": True})
            return
        try:
            found = decode_iq(to_iq(audio, self.fs), self.binary)
        except Exception as e:
            log.warning("WSPR : %s", e)
            found = []
        utc = time.strftime("%H%M%S", time.gmtime(start))
        msgs = [{"t": "msg", "mode": "WSPR", "utc": utc, "snr": m["snr"], "dt": m["dt"], "freq": int(round(m["freq"])),
                 "text": f"{m['call']} {m['loc']} {m['pwr']} dBm" + (f"  (dérive {m['drift']:+.0f} Hz)" if m["drift"] else "")}
                for m in found]
        self.last_count = len(msgs)
        with self.lock:
            self.results.extend(msgs)
