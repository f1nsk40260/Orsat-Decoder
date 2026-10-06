"""ALE 2G (MIL-STD-188-141, FED-STD-1045) : établissement automatique de liaison en HF.

- 8 tonalités de 750 à 2500 Hz (pas 250 Hz), 125 bauds, 3 bits par symbole (code de Gray) ;
- un mot ALE = 24 bits : préambule de 3 bits (TO, TIS, TWAS, DATA, REP, CMD, THRU, FROM) et
  3 caractères ASCII de 7 bits ; deux moitiés de 12 bits protégées chacune par un Golay (24,12)
  (contrôle de B inversé), entrelacées bit à bit (49 bits avec le bit de bourrage) et émises 3 fois
  de suite : vote majoritaire, puis correction de 3 erreurs par moitié ;
- les mots s'assemblent en appels : adresses (TO, TIS, TWAS…, prolongées par DATA / REP),
  commandes et messages AMD.
"""
import itertools
import time

import numpy as np

from ..dsp import Decoder

G = 0xAE3                                   # polynôme du Golay (23,12) ; le 24e bit est la parité
PREAMBLE = {0: "DATA", 1: "THRU", 2: "TO", 3: "TWAS", 4: "FROM", 5: "TIS", 6: "CMD", 7: "REP"}
GRAY = [0b000, 0b001, 0b011, 0b010, 0b110, 0b111, 0b101, 0b100]       # tonalité k -> tribit
BASIC38 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@?")


def _parity12(d):
    r = d << 11
    for i in range(22, 10, -1):
        if r >> i & 1:
            r ^= G << (i - 11)
    r &= 0x7FF
    par = (bin(d).count("1") + bin(r).count("1")) & 1
    return (r << 1) | par


ENC = [_parity12(d) for d in range(4096)]
_SYN = {}
for _w in (1, 2, 3):
    for _pos in itertools.combinations(range(24), _w):
        _e = sum(1 << p for p in _pos)
        _SYN.setdefault((_e & 0xFFF) ^ ENC[_e >> 12], _e)


def golay_encode(d):
    return (d << 12) | ENC[d]


def golay_decode(c):
    """24 bits -> (12 bits de données, nb d'erreurs corrigées) ou (None, 4)."""
    s = (c & 0xFFF) ^ ENC[c >> 12]
    if s == 0:
        return c >> 12, 0
    e = _SYN.get(s)
    if e is None:
        return None, 4
    return (c ^ e) >> 12, bin(e).count("1")


def word_bits(pre, text):
    """Mot ALE -> 147 bits émis (49 bits entrelacés, trois fois)."""
    t = (text + "   ")[:3]
    w = (pre << 21) | (ord(t[0]) & 0x7F) << 14 | (ord(t[1]) & 0x7F) << 7 | (ord(t[2]) & 0x7F)
    a = golay_encode(w >> 12)
    b = golay_encode(w & 0xFFF) ^ 0xFFF
    bits = []
    for i in range(23, -1, -1):
        bits += [(a >> i) & 1, (b >> i) & 1]
    bits.append(0)
    return bits * 3


class ALE(Decoder):
    name = "ALE"
    kind = "msg"

    def __init__(self, fs, af=1625.0):
        super().__init__(fs, af)
        self.sps = self.fs / 125.0
        self.N = int(round(self.sps))
        self.delta = float(af) - 1625.0            # décalage des tonalités (clic)
        self.delta0 = self.delta
        self.buf = np.zeros(0)
        self.base = 0                              # indice absolu de buf[0]
        self.next = None                           # début (absolu) du prochain symbole
        self.bits = []
        self.synced = 0                            # symboles restant avant le prochain mot attendu
        self.lost = 0
        self.call = []                             # (préambule, texte) de l'appel en cours
        self.last_word_t = 0.0
        self.t = 0.0
        self.count = 0
        self.quality = 0.0
        self._make_dft()

    def _make_dft(self):
        f = 750.0 + 250.0 * np.arange(8) + self.delta
        n = np.arange(self.N)
        self.W = np.exp(-2j * np.pi * np.outer(f, n) / self.fs)

    def set_af(self, af):
        super().set_af(af)
        self.delta0 = self.delta = float(af) - 1625.0
        self._make_dft()

    def status(self):
        return {"af": round(1625.0 + self.delta, 1), "sync": self.synced > 0, "last": self.count,
                "quality": round(self.quality, 2)}

    # ------------------------------------------------------------------ fréquence et rythme
    def _acquire(self):
        """Décalage de fréquence (peigne de 8 raies) et phase symbole, sur la dernière seconde."""
        n = int(self.fs)
        x = self.buf[-n:]
        nf = 1 << 15
        sp = np.abs(np.fft.rfft(x * np.hanning(len(x)), nf)) ** 2
        f = np.fft.rfftfreq(nf, 1 / self.fs)
        cand = np.arange(self.delta0 - 120, self.delta0 + 120.5, 0.5)
        comb = np.zeros(len(cand))
        for k in range(8):
            comb += np.interp(750 + 250 * k + cand, f, sp)
        noise = np.median(sp[(f > 500) & (f < 2800)]) * 8 + 1e-20
        i = int(np.argmax(comb))
        if comb[i] < 6 * noise:
            return False
        # affinage : décalage et phase qui concentrent le mieux l'énergie de chaque symbole sur une tonalité
        n0 = np.arange(self.N)
        best = None
        for dlt in np.arange(cand[i] - 30, cand[i] + 30.5, 1.0):
            W = np.exp(-2j * np.pi * np.outer(750.0 + 250.0 * np.arange(8) + dlt, n0) / self.fs)
            for p in range(0, self.N, 6):
                m = (len(x) - p) // self.N
                E = np.abs(x[p:p + m * self.N].reshape(m, self.N) @ W.T) ** 2
                q = np.mean(E.max(1) / (E.sum(1) + 1e-20))
                if best is None or q > best[0]:
                    best = (q, p, dlt)
        if best[0] < 0.45:
            return False
        self.delta = float(best[2])
        self._make_dft()
        # phase fine autour de la meilleure
        p0 = best[1]
        for p in range(max(0, p0 - 5), min(self.N, p0 + 6)):
            m = (len(x) - p) // self.N
            E = np.abs(x[p:p + m * self.N].reshape(m, self.N) @ self.W.T) ** 2
            q = np.mean(E.max(1) / (E.sum(1) + 1e-20))
            if q > best[0]:
                best = (q, p, best[2])
        start = self.base + len(self.buf) - n + best[1]
        self.next = float(start)
        while self.next + self.N < self.base + len(self.buf) - self.N:
            self.next += self.sps
        self.next -= self.sps * 50                 # repartir un mot en arrière (le début du mot en cours)
        self.next = max(self.next, float(self.base))
        self.bits = []
        return True

    def process(self, x):
        x = np.asarray(x, np.float64)
        self.buf = np.concatenate([self.buf, x])
        self.t += len(x) / self.fs
        out = []
        if self.next is None:
            if len(self.buf) >= self.fs and int(self.t * 2) != int((self.t - len(x) / self.fs) * 2):
                self._acquire()
        if self.next is not None:
            while self.next + self.N <= self.base + len(self.buf):
                i = int(round(self.next)) - self.base
                seg = self.buf[i:i + self.N]
                E = np.abs(self.W @ seg) ** 2
                k = int(np.argmax(E))
                self.quality += 0.02 * (E[k] / (E.sum() + 1e-20) - self.quality)
                v = GRAY[k]
                self.bits += [(v >> 2) & 1, (v >> 1) & 1, v & 1]
                if len(self.bits) > 147:
                    del self.bits[:-147]
                self.next += self.sps
                self._symbol(out)
        # historique : deux secondes
        keep = int(2 * self.fs)
        if len(self.buf) > keep:
            cut = len(self.buf) - keep
            if self.next is not None:
                cut = min(cut, int(self.next) - self.base)
            if cut > 0:
                self.buf = self.buf[cut:]
                self.base += cut
        if self.call and self.t - self.last_word_t > 1.2:
            self._flush(out)
        return out

    # ------------------------------------------------------------------ mots
    def _word(self):
        b = np.array(self.bits, np.int8)
        s = b[0:49] + b[49:98] + b[98:147]
        bad = int(np.sum((s == 1) | (s == 2)))
        maj = (s[:48] >= 2).astype(int)
        a = int("".join(map(str, maj[0::2])), 2)
        bb = int("".join(map(str, maj[1::2])), 2) ^ 0xFFF
        da, ea = golay_decode(a)
        db, eb = golay_decode(bb)
        if da is None or db is None:
            return None, bad, 9
        w = (da << 12) | db
        return w, bad, max(ea, eb)

    def _symbol(self, out):
        if len(self.bits) < 147:
            return
        if self.synced > 0:
            self.synced -= 1
            if self.synced > 0:
                return
            w, bad, err = self._word()
            if w is not None and err <= 3 and bad <= 24 and self._plausible(w, strict=w >> 21 in (1, 2, 3, 4, 5)):
                self._accept(w)
                self.synced = 49
                self.lost = 0
            else:
                self.lost += 1
                self.synced = 49 if self.lost < 2 else 0
            return
        w, bad, err = self._word()
        if w is not None and bad <= 12 and err <= 1 and self._plausible(w, strict=True):
            self._accept(w)
            self.synced = 49
            self.lost = 0
        elif self.t - self.last_word_t > 3.0 and len(self.buf) >= self.fs:
            # rien depuis longtemps : refaire l'acquisition (fréquence et rythme) toutes les secondes
            if int(self.t) != getattr(self, "_acq_t", -1):
                self._acq_t = int(self.t)
                self._acquire()

    @staticmethod
    def _plausible(w, strict=False):
        pre = w >> 21
        ch = [(w >> s) & 0x7F for s in (14, 7, 0)]
        if any(c < 0x20 and c not in (10, 13) for c in ch) or any(c > 0x7E for c in ch):
            return False
        if strict and pre in (1, 2, 3, 4, 5):                # adresses : alphabet de base (38)
            return all(chr(c) in BASIC38 for c in ch)
        return True

    def _accept(self, w):
        pre = PREAMBLE[w >> 21]
        txt = "".join(chr((w >> s) & 0x7F) for s in (14, 7, 0))
        self.last_word_t = self.t
        if self.call and self.call[-1] == (pre, txt) and pre != "DATA":
            return                                           # appel de balayage répété
        self.call.append((pre, txt))

    def _flush(self, out):
        parts = []
        cur = None
        for pre, txt in self.call:
            if pre in ("DATA", "REP"):
                if cur is not None:
                    cur[1] += txt
                continue                                     # suite d'un mot manqué : sans contexte
            cur = [pre, txt]
            parts.append(cur)
        self.call = []
        items = []
        for pre, txt in parts:
            if pre == "CMD":
                items.append(f"CMD {txt.rstrip()}")
            elif pre in ("DATA", "REP"):
                items.append(txt.rstrip())
            else:
                items.append(f"{pre} {txt.rstrip('@ ')}")
        # appels répétés pendant le balayage : on ne garde qu'une fois chaque élément consécutif
        dedup = [it for i, it in enumerate(items) if i == 0 or it != items[i - 1]]
        txt = " · ".join(d for d in dedup if d)
        if txt:
            self.count += 1
            out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()), "text": "ALE " + txt})
