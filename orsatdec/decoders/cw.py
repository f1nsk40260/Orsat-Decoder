"""Décodeur CW (Morse).

- filtre étroit autour de la tonalité, enveloppe à 200 Hz, lissée sur un tiers de point ;
- seuils adaptatifs : plancher et sommet estimés par percentiles sur les 4 dernières secondes ;
- vitesse automatique : les durées de signes sont classées en deux groupes (points / traits).
"""
from collections import deque

import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass
from ..tables import MORSE


class CW(Decoder):
    name = "CW"

    def __init__(self, fs, af=700.0, bw=80.0, wpm=20.0, squelch=0.0):
        super().__init__(fs, af)
        self.rate = 200.0
        self.D = max(1, int(round(self.fs / self.rate)))
        self.rate = self.fs / self.D
        self.mix = Mixer(af, self.fs)
        self.fir = FirDecim(lowpass(self.fs, bw / 2, 0.03 * self.fs), self.D)
        self.dot = 1.2 / wpm * self.rate
        self.hist = deque(maxlen=int(4 * self.rate))
        self.marks = deque(maxlen=40)
        self.ma = deque()
        self.ma_sum = 0.0
        self.on = False
        self.run = 0
        self.pend = 0
        self.code = ""
        self.spaced = True
        self.floor = self.top = 1e-6
        self.n = 0
        self.squelch = squelch

    def set_af(self, af):
        super().set_af(af)
        self.mix.freq = float(af)

    def status(self):
        snr = 20 * np.log10(max(self.top, 1e-9) / max(self.floor, 1e-9))
        return {"af": round(self.af, 1), "wpm": round(1.2 * self.rate / self.dot, 1), "snr": round(snr, 1)}

    def process(self, x):
        e = np.abs(self.fir.process(self.mix.process(np.asarray(x, np.float64))))
        out = []
        for v in e:
            # moyenne glissante sur ~1/3 de point
            L = max(1, int(self.dot * 0.5))
            self.ma.append(v)
            self.ma_sum += v
            while len(self.ma) > L:
                self.ma_sum -= self.ma.popleft()
            env = self.ma_sum / len(self.ma)
            self.hist.append(env)
            self.n += 1
            if self.n % 20 == 0 and len(self.hist) > self.rate:
                h = np.fromiter(self.hist, float)
                self.floor = np.percentile(h, 25)
                self.top = np.percentile(h, 92)
            span = self.top - self.floor
            hi = self.floor + 0.55 * span
            lo = self.floor + 0.40 * span
            present = self.top > 1.6 * self.floor and self.top > self.squelch
            self.run += 1
            # anti-rebond : un changement d'état doit durer au moins un quart de point
            want = (env > hi and present) if not self.on else not (env < lo)
            if want != self.on:
                self.pend += 1
            else:
                self.pend = 0
            deb = max(1, int(0.25 * self.dot))
            if self.pend >= deb:
                if not self.on:
                    self._gap(self.run - self.pend, out)
                else:
                    self._mark(self.run - self.pend)
                self.on, self.run, self.pend = not self.on, self.pend, 0
            elif not self.on:
                if self.code and self.run > 2.5 * self.dot:
                    out.append(self._flush())
                elif not self.code and not self.spaced and self.run > 6 * self.dot:
                    out.append(" ")
                    self.spaced = True
        txt = "".join(c for c in out if c)
        return [{"t": "text", "text": txt}] if txt else []

    def _classify_speed(self):
        """Deux groupes de durées : points et traits. Le seuil se place entre les deux."""
        m = np.array(self.marks)
        if len(m) < 6:
            return
        if m.max() < 1.8 * m.min():          # un seul type de signe vu : on ne peut pas trancher
            thr = 2 * self.dot
        else:
            thr = (m.min() + m.max()) / 2
        for _ in range(5):
            short, long_ = m[m < thr], m[m >= thr]
            if len(short) == 0 or len(long_) == 0:
                break
            thr = (short.mean() + long_.mean()) / 2
        short, long_ = m[m < thr], m[m >= thr]
        if len(short) and len(long_) and long_.mean() > 2 * short.mean():
            est = (short.mean() + long_.mean() / 3) / 2
        elif len(short) and not len(long_):
            est = short.mean() if short.mean() > 0.6 * self.dot else self.dot
        else:
            return
        self.dot += 0.3 * (est - self.dot)
        self.dot = min(max(self.dot, 1.2 / 60 * self.rate), 1.2 / 5 * self.rate)

    def _mark(self, n):
        if n < 0.35 * self.dot:
            return
        self.marks.append(n)
        self._classify_speed()
        self.code += "." if n < 2 * self.dot else "-"
        if len(self.code) > 8:
            self.code = ""

    def _gap(self, n, out):
        if self.code and n > 2.5 * self.dot:
            out.append(self._flush())
        if n > 6 * self.dot and not self.spaced:
            out.append(" ")
            self.spaced = True

    def _flush(self):
        c = MORSE.get(self.code, "")
        self.code = ""
        if c:
            self.spaced = False
        return c
