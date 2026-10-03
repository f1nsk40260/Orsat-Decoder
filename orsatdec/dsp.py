"""Briques DSP communes : oscillateur de mélange, filtre RIF décimateur à état, interpolation."""
import numpy as np
from scipy.signal import firwin

TAU = 2 * np.pi


class Mixer:
    """Translate un signal réel ou complexe de -f Hz (oscillateur à phase continue)."""

    def __init__(self, freq, fs):
        self.freq, self.fs, self.ph = float(freq), float(fs), 0.0

    def process(self, x):
        n = len(x)
        w = TAU * self.freq / self.fs
        ph = self.ph + w * np.arange(n)
        self.ph = (self.ph + w * n) % TAU
        return x * np.exp(-1j * ph)


class FirDecim:
    """Filtre RIF à état + décimation par un entier D, continu d'un bloc à l'autre."""

    def __init__(self, taps, D, dtype=np.complex128):
        self.taps = np.asarray(taps, dtype=np.float64)
        self.D = int(D)
        self.buf = np.zeros(len(self.taps) - 1, dtype=dtype)
        self.phase = 0

    def process(self, x):
        if len(x) == 0:
            return x[:0]
        x = np.concatenate([self.buf, x])
        y = np.convolve(x, self.taps, mode="valid")
        if len(self.taps) > 1:
            self.buf = x[-(len(self.taps) - 1):]
        out = y[self.phase::self.D]
        self.phase = (self.phase - len(y)) % self.D
        return out


class ToneFinder:
    """Garde ~1 s d'audio brut et cherche, toutes les `period` s, où se trouve le signal attendu
    autour d'une fréquence (porteuse seule, ou paire de tonalités FSK). Sert de CAF grossière."""

    def __init__(self, fs, seconds=1.0, period=1.0):
        self.fs = float(fs)
        self.n = 1 << int(np.ceil(np.log2(self.fs * seconds)))
        self.buf = np.zeros(0)
        self.period = int(self.fs * period)
        self.count = 0

    def feed(self, x):
        self.buf = np.concatenate([self.buf, x])[-self.n:]
        self.count += len(x)
        if self.count >= self.period and len(self.buf) == self.n:
            self.count = 0
            return True
        return False

    def spectrum(self):
        sp = np.abs(np.fft.rfft(self.buf * np.hanning(self.n))) ** 2
        return np.fft.rfftfreq(self.n, 1 / self.fs), sp

    def find(self, center, span, shift=0.0, min_ratio=8.0):
        """Fréquence centrale du signal dans [center-span, center+span], ou None si rien de net.
        shift > 0 : on cherche une paire de tonalités espacées de shift Hz."""
        f, sp = self.spectrum()
        df = f[1] - f[0]
        sel = (f >= center - span - shift / 2 - 20) & (f <= center + span + shift / 2 + 20)
        if not np.any(sel):
            return None
        noise = np.median(sp[sel]) + 1e-20
        cands = np.arange(center - span, center + span + df, df)
        def power(fc):
            if shift > 0:
                return np.interp(fc - shift / 2, f, sp) + np.interp(fc + shift / 2, f, sp)
            return np.interp(fc, f, sp)
        # lissage léger : la puissance d'un signal modulé s'étale sur quelques bins
        k = max(1, int(round(8 / df)))
        pw = np.array([power(c) for c in cands])
        pw = np.convolve(pw, np.ones(k) / k, mode="same")
        i = int(np.argmax(pw))
        ref = noise * (2 if shift > 0 else 1)
        if pw[i] < min_ratio * ref:
            return None
        return float(cands[i])


def lowpass(fs, cutoff, ntaps):
    ntaps = int(ntaps) | 1
    return firwin(ntaps, cutoff, fs=fs)


def interp(buf, pos):
    """Interpolation linéaire de buf à la position fractionnaire pos."""
    i = int(np.floor(pos))
    f = pos - i
    if i < 0 or i + 1 >= len(buf):
        return buf[min(max(i, 0), len(buf) - 1)]
    return buf[i] * (1 - f) + buf[i + 1] * f


class Decoder:
    """Interface commune des décodeurs.

    process(x) reçoit des échantillons audio réels (float, ±1) à fs Hz et renvoie une liste
    d'événements : {"t": "text", "text": "..."} ou {"t": "msg", ...} pour les messages structurés.
    """
    name = "?"
    kind = "text"          # "text" : flux de caractères ; "msg" : messages structurés

    def __init__(self, fs, af=1000.0):
        self.fs = float(fs)
        self.af = float(af)

    def set_af(self, af):
        self.af = float(af)

    def status(self):
        return {"af": round(self.af, 1)}

    def process(self, x):
        return []
