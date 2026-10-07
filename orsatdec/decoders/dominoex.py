"""Décodeur DominoEX (DominoEX 4 à 88 et Micro) : IFK+ à 18 tonalités, Varicode à quartets.

Option « FEC » : code convolutif K=7 et entrelaceur 4x4 sur
le Varicode MFSK. Le canal secondaire (texte d'attente) est reconnu mais pas affiché.

Sans FEC, chaque quartet est la valeur la plus vraisemblable compte tenu des énergies des deux symboles
consécutifs ; le silencieux s'appuie sur le rapport signal/bruit mesuré sur le peigne de tonalités.
"""
import numpy as np

from ..dsp import Decoder
from .ifk_common import (DOMINO_MODES, IFK_TONES, ToneDemod, Viterbi, Interleaver, VariShreg, TextOut, IFKDiff,
                         printable)
from .ifk_tables import DOMINO_VARIDEC, MFSK_VARIDEC


def mupsk_pri2sec(c):
    """Codes primaires réservés -> caractère secondaire (| 0x100)."""
    if 127 <= c < 153:
        return c + (ord("A") - 127) + 0x100
    if 14 <= c < 24:
        return c + (ord("0") - 14) + 0x100
    if 1 <= c < 4:
        return c + (ord(" ") - 1) + 0x100
    if c == 4:
        return ord("_") + 0x100
    if 5 <= c < 8:
        return c + (ord("$") - 5) + 0x100
    if 9 <= c < 13:
        return c + (ord("'") - 9) + 0x100
    if 24 <= c < 29:
        return c + (ord("+") - 24) + 0x100
    if 29 <= c < 32:
        return c + (ord(":") - 29) + 0x100
    if 153 <= c < 157:
        return c + (ord("=") - 153) + 0x100
    if 157 <= c < 160:
        return c + (ord("[") - 157) + 0x100
    return c


class DominoEX(Decoder):
    name = "DominoEX"

    def __init__(self, fs, af=1500.0, mode="DominoEX 11", fec=False, reverse=False, squelch=None):
        super().__init__(fs, af)
        sr, symlen, ds = DOMINO_MODES[mode]
        self.mode, self.fec, self.reverse = mode, bool(fec), bool(reverse)
        self.squelch = float(squelch if squelch is not None else (0.925 if self.fec else 0.65))
        self.dem = ToneDemod(fs, af, sr / symlen, IFK_TONES, ds)
        self.ifk = IFKDiff()
        self.out = TextOut()
        self.q = 0.0
        # sans FEC
        self.nibbles = []
        # avec FEC
        self.vit = Viterbi(7, 0x6d, 0x4f, traceback=45, chunk=4)
        self.inlv = Interleaver(4, 4, rx=True)
        self.inlv.warm = 8 + (self.vit.tb + self.vit.chunk + self.vit.W) // 2
        self.var = VariShreg(MFSK_VARIDEC)

    def set_af(self, af):
        super().set_af(af)
        self.dem.set_af(af)

    def quality(self):
        if self.fec:
            return self.vit.quality if self.inlv.filled else 0.0
        return self.q

    def status(self):
        q = self.quality()
        return {"af": round(self.dem.centre(), 1), "snr": round(self.dem.snr_db(), 1),
                "quality": round(max(0.0, q), 2), "sync": int(q > self.squelch), "mode": self.mode}

    def _symbol(self, T):
        if self.reverse:
            T = T[::-1]
        Lc = self.ifk.metric(self.dem.tone_llr(T))
        if Lc is None:
            return []
        return self._fec(Lc) if self.fec else self._plain(Lc)

    def _plain(self, Lc):
        # qualité : probabilité a posteriori du quartet retenu, lissée (≈ 1/16 sur du bruit)
        p = np.exp(Lc - Lc.max())
        p /= p.sum()
        c = int(np.argmax(Lc))
        self.q += 0.08 * (p[c] - self.q)
        out = []
        if not (c & 8):                                  # début d'un nouveau caractère : décoder le précédent
            if 1 <= len(self.nibbles) <= 3:
                sym = 0
                for v in self.nibbles:
                    sym = (sym << 4) | v
                ch = DOMINO_VARIDEC.get(sym, -1)
                if ch >= 0 and not ch & 0x100:
                    s = self.out.put(printable(ch), self.q > self.squelch)
                    if s:
                        out.append(s)
            self.nibbles = []
        self.nibbles = (self.nibbles + [c])[-4:]
        return out

    def _fec(self, Lc):
        llr = self.inlv.symbols(np.clip(self.ifk.bit_llr(Lc), -30, 30))
        out = []
        for i in (0, 2):
            is_open = self.vit.quality > self.squelch and self.inlv.filled
            for bit in self.vit.step(llr[i], llr[i + 1]):
                c = self.var.bit(bit)
                if c is None or c < 0:
                    continue
                c = mupsk_pri2sec(c)
                if c & 0x100:
                    continue
                s = self.out.put(printable(c), is_open)
                if s:
                    out.append(s)
        return out

    def process(self, x):
        self.dem.feed(np.asarray(x, np.float64))
        out = []
        for T in self.dem.symbols():
            self.dem.lock = self.quality() > self.squelch - (0.04 if self.fec else 0.2)
            out += self._symbol(T)
        return [{"t": "text", "text": "".join(out)}] if out else []
