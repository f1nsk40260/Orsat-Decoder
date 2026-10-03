"""FT8 / FT4 : l'audio est découpé en créneaux UTC (15 s ou 7,5 s) puis décodé par ft8_lib
(compilé avec Orsat-Decoder, dossier native/). Plusieurs signaux par créneau."""
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from ..dsp import Decoder

NATIVE = Path(os.environ.get("ORSAT_NATIVE", Path(__file__).resolve().parents[2] / "native" / "bin"))
LINE = re.compile(r"^(\d{6})\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+\.\d+)\s+(\d+)\s+~\s+(.*\S)\s*$")


class FT8(Decoder):
    name = "FT8"
    kind = "msg"

    def __init__(self, fs, af=1500.0, ft4=False, latency=0.6):
        super().__init__(fs, af)
        self.ft4 = ft4
        self.name = "FT4" if ft4 else "FT8"
        self.period = 7.5 if ft4 else 15.0
        self.latency = latency          # retard estimé de l'audio reçu du serveur (s)
        self.buf = []
        self.slot = None
        self.lock = threading.Lock()
        self.results = []
        self.last_count = 0
        self.decoder = NATIVE / "decode_ft8"

    def status(self):
        return {"af": self.af, "slot": self.period, "last": self.last_count,
                "ready": self.decoder.exists()}

    def _slot_of(self, t):
        return int((t - self.latency) // self.period)

    def process(self, x):
        now = time.time()
        slot = self._slot_of(now)
        if self.slot is None:
            self.slot = slot
        if slot != self.slot:
            audio = np.concatenate(self.buf) if self.buf else np.zeros(0)
            start = self.slot * self.period
            self.buf = []
            self.slot = slot
            if len(audio) > self.fs * self.period * 0.6:
                threading.Thread(target=self._decode, args=(audio, start), daemon=True).start()
        self.buf.append(np.asarray(x, np.float64))
        with self.lock:
            res, self.results = self.results, []
        return res

    def _decode(self, audio, start):
        if not self.decoder.exists():
            with self.lock:
                self.results.append({"t": "msg", "text": "décodeur FT8 absent (native/bin/decode_ft8)", "error": True})
            return
        if abs(self.fs - 12000) > 1:
            audio = resample_poly(audio, 12000, int(round(self.fs)))
        audio = audio / (np.max(np.abs(audio)) + 1e-9) * 0.9
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            w = wave.open(path, "wb")
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(12000)
            w.writeframes((audio * 32767).astype("<i2").tobytes())
            w.close()
            args = [str(self.decoder)] + (["-ft4"] if self.ft4 else []) + [path]
            out = subprocess.run(args, capture_output=True, text=True, timeout=30).stdout
        except Exception as e:
            out = ""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        utc = time.strftime("%H%M%S", time.gmtime(start))
        msgs = []
        for line in out.splitlines():
            m = LINE.match(line.strip())
            if m:
                msgs.append({"t": "msg", "mode": self.name, "utc": utc, "snr": float(m.group(2)),
                             "dt": float(m.group(3)), "freq": int(m.group(4)), "text": m.group(5)})
        self.last_count = len(msgs)
        with self.lock:
            self.results.extend(msgs)
