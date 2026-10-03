"""Décodeur BPSK (PSK31, PSK63, PSK125) : mélange, filtrage, synchro symbole de Gardner,
détection différentielle, CAF et décodage Varicode."""
import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass, interp, TAU
from ..tables import VARICODE_DEC


class PSK(Decoder):
    name = "PSK"

    def __init__(self, fs, af=1000.0, baud=31.25, afc=True, squelch=0.35):
        super().__init__(fs, af)
        self.baud = float(baud)
        self.afc = afc
        self.squelch = squelch
        self.D = max(1, int(self.fs // (self.baud * 16)))
        self.fs2 = self.fs / self.D
        self.sps = self.fs2 / self.baud
        taps = lowpass(self.fs, self.baud * 1.1, 4 * self.fs / self.baud)
        self.mix = Mixer(af, self.fs)
        self.fir = FirDecim(taps, self.D)
        self.buf = np.zeros(0, np.complex128)
        self.pos = 2 * self.sps
        self.prev = 0j
        self.bits = ""
        self.quality = 0.0
        self.acc = 0j
        self.level = 0.0
        self.noise = 1e-9
        self.af0 = float(af)          # fréquence choisie par l'utilisateur ; la CAF reste autour
        self.ferr = 0.0
        self.capture = max(25.0, self.baud)      # plage d'acquisition autour de la fréquence choisie (Hz)
        self.hist = np.zeros(0, np.complex128)    # bande de base récente, pour l'acquisition
        self.nfft = 1 << int(np.ceil(np.log2(self.fs2 * 2)))
        self.since_acq = 0

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)
        self.mix.freq = float(af)

    def status(self):
        return {"af": round(self.mix.freq, 1), "quality": round(max(0.0, self.quality), 2),
                "snr": round(10 * np.log10(max(self.level, 1e-12) / max(self.noise, 1e-12)), 1)}

    def _acquire(self, z):
        """Acquisition grossière : élevé au carré, un BPSK donne une raie pure à 2× l'écart de fréquence."""
        self.hist = np.concatenate([self.hist, z])[-self.nfft:]
        self.since_acq += len(z)
        if len(self.hist) < self.nfft or self.since_acq < self.fs2 * 0.5:
            return
        self.since_acq = 0
        sq = self.hist * self.hist
        sq = sq * np.hanning(len(sq))
        sp = np.abs(np.fft.fft(sq, self.nfft * 2)) ** 2
        f = np.fft.fftfreq(self.nfft * 2, 1 / self.fs2) / 2      # écart de fréquence correspondant
        off = self.mix.freq - self.af0
        ok = np.abs(f + off) <= self.capture
        if not np.any(ok):
            return
        idx = np.flatnonzero(ok)
        k = idx[np.argmax(sp[idx])]
        cand = f[k] if sp[k] > 30 * np.median(sp[idx]) else None
        # Deux mesures successives concordantes avant de corriger : évite de sauter sur du bruit
        prev, self.last_cand = getattr(self, "last_cand", None), cand
        if cand is None or prev is None or abs(cand - prev) > max(1.0, self.baud / 20):
            return
        if abs(cand) > max(1.5, self.baud / 10):
            self.mix.freq += cand
            self.buf = self.buf[:0]
            self.pos = 2 * self.sps
            self.hist = self.hist[:0]
            self.acc = 0j
            self.last_cand = None

    def process(self, x):
        z = self.fir.process(self.mix.process(np.asarray(x, np.float64)))
        self.buf = np.concatenate([self.buf, z])
        self._acquire(z)
        text = []
        sps = self.sps
        while self.pos + 2 < len(self.buf):
            p = self.pos
            y = interp(self.buf, p)
            ym = interp(self.buf, p - sps / 2)
            yp = interp(self.buf, p - sps)
            # Synchro symbole (Gardner), normalisée par la puissance
            e = np.real((yp - y) * np.conj(ym))
            pw = abs(y) ** 2 + abs(yp) ** 2 + 1e-12
            self.pos = p + sps + 0.08 * sps * np.clip(e / pw, -1, 1)

            d = y * np.conj(self.prev)
            self.prev = y
            ad = abs(d) + 1e-15
            # Cohérence : moyenne de d²/|d|². Son module (0..1) mesure la qualité sans être
            # perturbé par un écart de fréquence ; son argument donne cet écart.
            self.acc += 0.05 * ((d * d) / (ad * ad) - self.acc)
            self.quality = abs(self.acc)
            rot = np.angle(self.acc) / 2
            mag = abs(y) ** 2
            self.level += 0.05 * (mag - self.level)
            if self.quality < 0.2:
                self.noise += 0.01 * (mag - self.noise)

            if self.afc and self.quality > 0.3:
                ferr = rot * self.baud / TAU
                nf = self.mix.freq + 0.05 * ferr
                if abs(nf - self.af0) < max(15.0, self.baud * 0.5):
                    self.mix.freq = nf

            d = d * np.exp(-1j * rot)          # décision sur la phase corrigée
            bit = "1" if np.real(d) > 0 else "0"
            self.bits += bit
            if self.bits.endswith("00"):
                code = self.bits[:-2]
                if code:
                    ch = VARICODE_DEC.get(code)
                    if ch is not None and self.quality > self.squelch and (ch >= " " or ch in "\r\n"):
                        text.append(ch)
                self.bits = ""
            elif len(self.bits) > 16:
                self.bits = ""
        keep = int(self.pos - 2 * sps)
        if keep > 0:
            self.buf = self.buf[keep:]
            self.pos -= keep
        return [{"t": "text", "text": "".join(text)}] if text else []
