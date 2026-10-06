"""Décodeur PSK (compatible fldigi) : BPSK (PSK31 à PSK1000), QPSK (QPSK31 à QPSK500) et PSK-R (PSK125R à
PSK1000R, avec FEC). Mélange, filtrage, synchro symbole de Gardner, détection différentielle et CAF.

- BPSK : Varicode PSK31, une absence d'inversion de phase vaut 1 ;
- QPSK : code convolutif K=5 (0x17, 0x19) décodé par Viterbi à décisions souples, Varicode PSK31 ;
- PSK-R : BPSK + code K=7 (0x6d, 0x4f) + entrelaceur 2x2xN de fldigi + Varicode MFSK ; l'alignement des
  paires de bits est inconnu : deux décodeurs tournent, décalés d'un bit, et le meilleur parle.
"""
import numpy as np

from ..dsp import Decoder, Mixer, FirDecim, lowpass, interp, TAU
from ..tables import VARICODE_DEC


class PSK(Decoder):
    name = "PSK"

    def __init__(self, fs, af=1000.0, baud=31.25, afc=True, squelch=0.35, kind="bpsk", depth=40):
        super().__init__(fs, af)
        self.baud = float(baud)
        self.kind = kind
        self.M = 4 if kind == "qpsk" else 2
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
        self.capture = max(100.0, 2 * self.baud)   # plage d'acquisition autour de la fréquence choisie (Hz)
        self.hist = np.zeros(0, np.complex128)    # bande de base large et fixe, pour l'acquisition
        self.wide_mix = Mixer(af, self.fs)
        self.wide_fir = FirDecim(lowpass(self.fs, self.capture + self.baud, 2 * self.fs / self.baud), self.D)
        self.nfft = 1 << int(np.ceil(np.log2(self.fs2 * 2)))
        self.since_acq = 0
        self.amp = 1e-6
        if kind != "bpsk":
            from .ifk_common import Viterbi, Interleaver, VariShreg, TextOut
            from .ifk_tables import MFSK_VARIDEC
            if kind == "qpsk":
                self.vit = [Viterbi(5, 0x17, 0x19, traceback=30, chunk=4)]
            else:
                self.vit = [Viterbi(7, 0x6d, 0x4f, traceback=45, chunk=4) for _ in range(2)]
                self.inlv = [Interleaver(2, depth, rx=True) for _ in range(2)]
                self.var = [VariShreg(MFSK_VARIDEC) for _ in range(2)]
                self.prev_llr = 0.0
                self.nsym = 0
                self.best = 0
                self.textout = TextOut()

    def set_af(self, af):
        super().set_af(af)
        self.af0 = float(af)
        self.mix.freq = float(af)
        self.wide_mix.freq = float(af)
        self.capture = max(20.0, self.baud * 0.75)   # réglage manuel : acquisition resserrée
        self.last_cand = None

    def status(self):
        q = self.quality
        if self.kind != "bpsk":
            q = max(v.quality for v in self.vit)
        return {"af": round(self.mix.freq, 1), "quality": round(max(0.0, q), 2),
                "snr": round(10 * np.log10(max(self.level, 1e-12) / max(self.noise, 1e-12)), 1)}

    def _acquire(self, z):
        """Acquisition grossière : élevé à la puissance M, un signal M-PSK donne une raie pure à M fois l'écart."""
        M = self.M
        self.hist = np.concatenate([self.hist, z])[-self.nfft:]
        self.since_acq += len(z)
        if len(self.hist) < self.nfft or self.since_acq < self.fs2 * 0.5:
            return
        self.since_acq = 0
        sq = self.hist ** M
        sq = sq * np.hanning(len(sq))
        sp = np.abs(np.fft.fft(sq, self.nfft * 2)) ** 2
        f = np.fft.fftfreq(self.nfft * 2, 1 / self.fs2) / M      # écart par rapport à la fréquence cliquée
        ok = np.abs(f) <= self.capture
        idx = np.flatnonzero(ok)
        k = idx[np.argmax(sp[idx])]
        cand = f[k] if sp[k] > 30 * np.median(sp[idx]) else None
        # Deux mesures successives concordantes avant de corriger : évite de sauter sur du bruit
        prev, self.last_cand = getattr(self, "last_cand", None), cand
        if cand is None or prev is None or abs(cand - prev) > max(1.0, self.baud / 20):
            return
        target = self.af0 + cand
        if abs(target - self.mix.freq) > max(1.5, self.baud / 10):
            self.mix.freq = target
            self.buf = self.buf[:0]
            self.pos = 2 * self.sps
            self.acc = 0j
            self.last_cand = None

    def process(self, x):
        x = np.asarray(x, np.float64)
        z = self.fir.process(self.mix.process(x))
        self.buf = np.concatenate([self.buf, z])
        self._acquire(self.wide_fir.process(self.wide_mix.process(x)))
        text = []
        sps = self.sps
        M = self.M
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
            # Cohérence : moyenne de d^M/|d|^M. Son module (0..1) mesure la qualité sans être
            # perturbé par un écart de fréquence ; son argument donne cet écart.
            self.acc += 0.05 * ((d / ad) ** M - self.acc)
            self.quality = abs(self.acc)
            rot = np.angle(self.acc) / M
            mag = abs(y) ** 2
            self.level += 0.05 * (mag - self.level)
            if self.quality < 0.2:
                self.noise += 0.01 * (mag - self.noise)

            if self.afc and self.quality > 0.3:
                ferr = rot * self.baud / TAU
                nf = self.mix.freq + 0.05 * ferr
                if abs(nf - self.af0) < self.capture:
                    self.mix.freq = nf

            d = d * np.exp(-1j * rot)          # décision sur la phase corrigée
            if self.kind == "bpsk":
                self._bpsk(d, text)
            elif self.kind == "qpsk":
                self._qpsk(d, text)
            else:
                self._pskr(d, text)
        keep = int(self.pos - 2 * sps)
        if keep > 0:
            self.buf = self.buf[keep:]
            self.pos -= keep
        return [{"t": "text", "text": "".join(text)}] if text else []

    # ------------------------------------------------------------------ BPSK
    def _varicode_bit(self, bit, text, gate=None):
        self.bits += bit
        if self.bits.endswith("00"):
            code = self.bits[:-2]
            if code:
                ch = VARICODE_DEC.get(code)
                ok = self.quality > self.squelch if gate is None else gate
                if ch is not None and ok and (ch >= " " or ch in "\r\n"):
                    text.append(ch)
            self.bits = ""
        elif len(self.bits) > 16:
            self.bits = ""

    def _bpsk(self, d, text):
        self._varicode_bit("1" if np.real(d) > 0 else "0", text)

    # ------------------------------------------------------------------ QPSK
    def _qpsk(self, d, text):
        # phase différentielle -> dibit émis s (bit 0 : polynôme 1, bit 1 : polynôme 2) :
        # Δφ = π + s'·π/2 avec s' = (4 - s) & 3 (convention fldigi, sens normal)
        u = d * np.exp(-1j * np.pi) / (abs(d) + 1e-15)          # u = exp(j s' π/2)
        a = max(abs(d), 1e-12)
        self.amp += 0.02 * (a - self.amp)
        k = 4.0 * min(a / self.amp, 3.0)
        # s = (4 - s') & 3 : s' = 0 -> s = 0 ; s' = 1 -> 3 ; s' = 2 -> 2 ; s' = 3 -> 1
        # bit 0 de s : 1 pour s' impair ; bit 1 de s : 1 pour s' = 1 ou 2
        re, im = np.real(u), np.imag(u)
        l0 = k * abs(im) - k * abs(re)                 # s' ∈ {1, 3}  <=> |im| > |re|
        l1 = k * (np.sqrt(0.5) * (im - re))            # s' ∈ {1, 2}  <=> im - re > 0 (quadrants 90° et 180°)
        gate = self.vit[0].quality > 0.9
        for b in self.vit[0].step(l0, l1):
            self._varicode_bit("1" if b else "0", text, gate)

    # ------------------------------------------------------------------ PSK-R
    def _pskr(self, d, text):
        a = max(abs(d), 1e-12)
        self.amp += 0.02 * (a - self.amp)
        llr = 4.0 * float(np.real(d)) / self.amp                  # > 0 : pas d'inversion = bit 1
        llr = float(np.clip(llr, -12, 12))
        k = self.nsym % 2
        self.nsym += 1
        if self.nsym >= 2:
            # branche k : la paire (précédent, courant) ; l'autre branche est décalée d'un bit
            pair = self.inlv[k].symbols([llr, self.prev_llr])      # [premier émis..] -> [poly2, poly1]
            l_p2, l_p1 = pair[0], pair[1]
            if self.inlv[k].filled:
                for bit in self.vit[k].step(l_p1, l_p2):
                    c = self.var[k].bit(bit)
                    if c is None or k != self.best:
                        continue
                    from .ifk_common import printable
                    s = self.textout.put(printable(c), self.vit[k].quality > 0.9)
                    if s:
                        text.append(s)
            o = self.vit[1 - self.best].quality
            if o > self.vit[self.best].quality + 0.1:
                self.best = 1 - self.best
        self.prev_llr = llr
