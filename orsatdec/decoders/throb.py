"""THROB et THROBX : chaque caractère est une paire de tonalités (ou une seule)
parmi 9 (THROB) ou 11 (THROBX), émise pendant un symbole d'environ 1, 0,5 ou 0,25 s.

La bande de base est ramenée à 250 Hz ; chaque symbole est corrélé aux tonalités (avec la forme
d'impulsion de l'émetteur), le rythme symbole est cherché sur les derniers symboles, puis suivi.
"""
import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass

NAR = [-32, -24, -16, -8, 0, 8, 16, 24, 32]
WID = [-64, -48, -32, -16, 0, 16, 32, 48, 64]
XNAR = [-39.0625, -31.25, -23.4375, -15.625, -7.8125, 0, 7.8125, 15.625, 23.4375, 31.25, 39.0625]
XWID = [2 * f for f in XNAR]

PAIRS = [(5, 5), (4, 5), (1, 2), (1, 3), (1, 4), (4, 6), (1, 5), (1, 6), (1, 7), (3, 7), (1, 8), (2, 3), (2, 4),
         (2, 8), (2, 5), (5, 6), (2, 6), (2, 9), (3, 4), (3, 5), (1, 9), (3, 6), (8, 9), (3, 8), (3, 3), (2, 2), (1, 1),
         (3, 9), (4, 7), (4, 8), (4, 9), (5, 7), (5, 8), (5, 9), (6, 7), (6, 8), (6, 9), (7, 8), (7, 9), (8, 8), (7, 7),
         (6, 6), (4, 4), (9, 9), (2, 7)]
CHARS = "\0ABCD\0FGHIJKLMNOPQRSTUVWXYZ1234567890,.'/)(E "
XPAIRS = [(6, 11), (1, 6), (2, 6), (2, 5), (2, 7), (2, 8), (5, 6), (2, 9), (2, 10), (4, 8), (4, 6), (2, 11), (3, 4),
          (3, 5), (3, 6), (6, 9), (6, 10), (3, 7), (3, 8), (3, 9), (6, 8), (6, 7), (3, 10), (3, 11), (4, 5), (4, 7),
          (4, 9), (4, 10), (1, 2), (1, 3), (1, 4), (1, 5), (1, 7), (1, 8), (1, 9), (1, 10), (2, 3), (2, 4), (4, 11),
          (5, 7), (5, 8), (5, 9), (5, 10), (5, 11), (7, 8), (7, 9), (7, 10), (7, 11), (8, 9), (8, 10), (8, 11),
          (9, 10), (9, 11), (10, 11), (1, 11)]
XCHARS = "\0 ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890,.'/)(#\"+-;:?!@=\n"

# mode : (durée du symbole à 8 kHz, tonalités, impulsion « semi » ou « pleine », THROBX ?)
THROB_MODES = {"THROB1": (8192, NAR, "semi", False), "THROB2": (4096, NAR, "semi", False),
               "THROB4": (2048, WID, "full", False), "THROBX1": (8192, XNAR, "semi", True),
               "THROBX2": (4096, XNAR, "semi", True), "THROBX4": (2048, XWID, "full", True)}
FR = 250.0                                   # cadence de la bande de base (8000 / 32)


def pulse(n, kind):
    i = np.arange(n)
    if kind == "full":
        return 0.5 * (1 - np.cos(2 * np.pi * i / n))
    p = np.ones(n)
    a = n // 5
    p[:a] = 0.5 * (1 - np.cos(np.pi * i[:a] / (n / 5.0)))
    p[n * 4 // 5:] = 0.5 * (1 + np.cos(np.pi * (i[n * 4 // 5:] - n * 4 // 5) / (n / 5.0)))
    return p


class Throb(Decoder):
    name = "THROB"

    def __init__(self, fs, af=1000.0, mode="THROB1", reverse=False):
        super().__init__(fs, af)
        n8, self.freqs, pk, self.x = THROB_MODES[mode]
        self.mode, self.reverse = mode, reverse
        self.nt = len(self.freqs)
        self.N = n8 // 32                                  # échantillons par symbole à 250 Hz
        self.D = int(round(self.fs / FR))
        self.fr = self.fs / self.D
        self.mix = Mixer(af, self.fs)
        self.fir = FirDecim(lowpass(self.fs, 110.0, int(self.fs / 25)), self.D)
        p = pulse(self.N, pk)
        t = np.arange(self.N) / self.fr
        self.T = np.array([p * np.exp(-2j * np.pi * f * t) for f in self.freqs])
        self.buf = np.zeros(0, complex)
        self.base = 0
        self.next = None
        self.since = 0
        self.shift = False
        self.idle, self.space = 0, 1
        self.last = ""
        self.metric = 0.0
        self.df = 0.0

    def set_af(self, af):
        super().set_af(af)
        self.mix.freq = float(af)
        self.next = None

    def status(self):
        return {"af": round(self.mix.freq, 1), "snr": round(10 * np.log10(max(self.metric, 1e-3)), 1),
                "sync": self.next is not None}

    def _energies(self, start, df=0.0):
        seg = self.buf[start:start + self.N]
        if df:
            seg = seg * np.exp(-2j * np.pi * df * np.arange(self.N) / self.fr)
        return np.abs(self.T @ seg)

    def _acquire(self):
        """Rythme et petit écart de fréquence qui concentrent le mieux l'énergie sur deux tonalités."""
        n = len(self.buf)
        nsym = min(6, n // self.N - 1)
        if nsym < 3:
            return
        best = None
        for df in np.arange(-3.0, 3.01, 0.5):
            for p in range(0, self.N, max(1, self.N // 16)):
                q = 0.0
                for k in range(nsym):
                    s = n - (k + 2) * self.N + p
                    if s < 0:
                        continue
                    e = np.sort(self._energies(s, df))[::-1]
                    q += (e[0] + e[1]) / (e.sum() + 1e-12)
                if best is None or q > best[0]:
                    best = (q, p, df)
        q, p, df = best
        if q / nsym < 0.55:
            return
        self.df = df
        self.next = self.base + n - (nsym + 1) * self.N + p

    def process(self, x):
        z = self.fir.process(self.mix.process(np.asarray(x, np.float64)))
        self.buf = np.concatenate([self.buf, z])
        out = []
        if self.next is None:
            self.since += len(z)
            if self.since >= self.N:
                self.since = 0
                self._acquire()
        while self.next is not None and self.next + self.N + 2 <= self.base + len(self.buf):
            i = self.next - self.base
            # suivi du rythme : on compare l'énergie captée un peu avant et un peu après
            h = max(1, self.N // 32)
            e0 = self._energies(i, self.df)
            em = self._energies(max(0, i - h), self.df)
            ep = self._energies(i + h, self.df) if i + h + self.N <= len(self.buf) else e0
            top = lambda e: np.sort(e)[-2:].sum()
            if top(ep) > top(e0) * 1.02 and top(ep) >= top(em):
                i += h
            elif top(em) > top(e0) * 1.02:
                i -= h
            e = self._energies(i, self.df)
            self._symbol(e, out)
            self.next = self.base + i + self.N
        keep = 8 * self.N
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            if self.next is not None:
                cut = min(cut, self.next - self.base)
            self.buf = self.buf[cut:]
            self.base += cut
        txt = "".join(out)
        return [{"t": "text", "text": txt}] if txt else []

    def _symbol(self, e, out):
        o = np.argsort(e)[::-1]
        t1, t2 = int(o[0]), int(o[1])
        if not self.x and e[t1] > 2 * e[t2]:
            t2 = t1                                         # THROB : symbole à une seule tonalité
        a, b = min(t1, t2), max(t1, t2)
        sig = (e[t1] + e[t2]) / 2
        noise = (e.sum() - e[t1] - (e[t2] if t2 != t1 else 0)) / max(1, self.nt - 2) + 1e-12
        self.metric += 0.2 * (sig / noise - self.metric)
        if self.metric < 3.0:
            return
        if self.reverse:
            a, b = self.nt - 1 - b, self.nt - 1 - a
        pair = (a + 1, b + 1)
        if not self.x:
            if self.shift:
                self.shift = False
                ch = {(1, 9): "?", (2, 8): "@", (3, 7): "-", (5, 5): "\n"}.get(pair)
                if ch:
                    out.append(ch)
                return
            if pair == (4, 6):
                self.shift = True
                return
            if pair in PAIRS:
                ch = CHARS[PAIRS.index(pair)]
                if ch != "\0":
                    out.append(ch)
            return
        if pair in XPAIRS:
            k = XPAIRS.index(pair)
            if k in (self.idle, self.space):
                # THROBX : repos et espace alternent, et changent de rôle après chaque emploi
                if self.last not in ("", " "):
                    out.append(" ")
                    self.last = " "
                else:
                    self.last = ""
                self.idle, self.space = self.space, self.idle
            else:
                out.append(XCHARS[k])
                self.last = XCHARS[k]
