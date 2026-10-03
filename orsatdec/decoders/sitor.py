"""Décodeur SITOR-B / Navtex (CCIR 476, mode FEC).

1. Démodulation FSK (100 bauds, shift 170 Hz) et synchro bit par boucle sur les transitions ;
2. recherche du cadrage : parmi les 14 décalages possibles (7 bits × 2 positions), celui où les
   mots de 7 bits respectent le plus souvent la règle « 4 marks, 3 spaces » ;
3. chaque caractère est émis deux fois, à 5 caractères d'écart : on garde la copie valide.
"""
from collections import deque

import numpy as np

from ..dsp import Decoder
from ..tables import CCIR_LTRS, CCIR_FIGS, CCIR_LTRS_SHIFT, CCIR_FIGS_SHIFT, CCIR_ALPHA, CCIR_REP, CCIR_CHAR32
from .fsk import FSKDemod

VALID = [bin(c).count("1") == 4 for c in range(128)]


class SitorB(Decoder):
    name = "Navtex"

    def __init__(self, fs, af=1000.0, shift=170.0, reverse=False, baud=100.0):
        super().__init__(fs, af)
        self.baud = baud
        self.demod = FSKDemod(fs, af, baud, shift, reverse, bw_factor=0.6)
        self.spb = self.demod.fs2 / baud
        self.phase = 0.0                     # position dans le bit courant (0..spb)
        self.acc = 0.0
        self.n = 0
        self.prev = 0.0
        self.bits = deque(maxlen=7 * 2 * 40)  # derniers bits (valeurs souples)
        self.nbits = 0
        self.align = None                    # décalage de cadrage choisi (0..13)
        self.since_check = 0
        self.figs = False
        self.quality = 0.0
        self.first_dx = None
        self.pending = deque()               # mots non encore décodés, en attente de leur copie
        self.recent = deque(maxlen=8)
        self.held = deque(maxlen=6)        # validité des derniers caractères (silencieux sur du bruit)

    def set_af(self, af):
        super().set_af(af)
        self.demod.set_af(af)

    def status(self):
        return {"af": round(self.demod.af, 1), "snr": round(self.demod.snr(), 1), "quality": round(self.quality, 2),
                "sync": self.align is not None}

    # -- synchro bit : intégration sur le bit, réajustement sur chaque transition
    def process(self, x):
        d = self.demod.process(np.asarray(x, np.float64))
        out = []
        spb = self.spb
        for v in d:
            if (self.prev > 0) != (v > 0):
                # une transition devrait tomber en bord de bit (phase 0) : on corrige doucement
                err = self.phase if self.phase < spb / 2 else self.phase - spb
                self.phase -= 0.15 * err
            self.prev = v
            if 0.2 * spb <= self.phase <= 0.8 * spb:
                self.acc += v
                self.n += 1
            self.phase += 1
            if self.phase >= spb:
                self.phase -= spb
                self._bit(self.acc / max(1, self.n), out)
                self.acc, self.n = 0.0, 0
        txt = "".join(out)
        return [{"t": "text", "text": txt}] if txt else []

    def _bit(self, soft, out):
        self.bits.append(soft)
        self.nbits += 1
        self.since_check += 1
        if self.since_check >= 70:
            self.since_check = 0
            self._find_alignment()
        if self.align is not None and (self.nbits - self.align) % 14 == 0 and len(self.bits) >= 14:
            b = list(self.bits)[-14:]
            self._pair((self._word(b[:7]), b[:7]), (self._word(b[7:]), b[7:]), out)

    @staticmethod
    def _word(soft):
        return sum((1 << i) for i, s in enumerate(soft) if s > 0)

    def _find_alignment(self):
        b = np.array(self.bits)
        if len(b) < 14 * 20:
            return
        hard = (b > 0).astype(int)
        scores = []
        for off in range(7):
            seg = hard[off:]
            n = (len(seg) // 7) * 7
            words = seg[:n].reshape(-1, 7)
            vals = words @ (1 << np.arange(7))
            valid = np.mean([VALID[v] for v in vals])
            # le phasage (alpha / rep) décalé d'un bit donne aussi des mots « valides » :
            # on favorise le cadrage où ces deux codes apparaissent réellement
            phase = np.mean([(v == CCIR_ALPHA or v == CCIR_REP) for v in vals])
            scores.append(valid + 0.5 * phase)
        order = np.argsort(scores)
        best, second = int(order[-1]), scores[order[-2]]
        self.quality = min(1.0, max(0.0, float(scores[best]) - 0.5 * 0.0))
        self.quality = float(np.mean([VALID[v] for v in (hard[best:best + ((len(hard) - best) // 7) * 7].reshape(-1, 7) @ (1 << np.arange(7)))]))
        # cadrage retenu s'il se détache nettement des autres ; on le garde tant qu'il reste plausible
        if scores[best] > 0.45 and scores[best] - second > 0.10:
            start = self.nbits - len(b)
            new = (start + best) % 7
            if self.align is None or self.align % 7 != new:
                self.align = new
                self.pending.clear()
                self.first_dx = None
        elif scores[best] < 0.3:
            self.align = None

    def _pair(self, w1, w2, out):
        """Les mots arrivent par paires ; on identifie laquelle des deux positions porte la
        première émission (DX), puis on décode chaque caractère dès que sa copie est connue."""
        self.pending.append(w1)
        self.pending.append(w2)
        # Chaque caractère c_i se trouve aux positions 2i (DX) et 2i+5 (RX) du flux.
        if self.first_dx is None:
            seq = [w[0] for w in self.pending]
            if len(seq) >= 12:
                best = None
                for par in (0, 1):
                    agree = sum(1 for i in range(par, len(seq) - 5, 2) if seq[i] == seq[i + 5] and VALID[seq[i]])
                    best = (agree, par) if best is None or agree > best[0] else best
                if best[0] >= 2:
                    self.first_dx = best[1]
                    for _ in range(self.first_dx):
                        self.pending.popleft()
                else:
                    while len(self.pending) > 12:
                        self.pending.popleft()
            return
        while len(self.pending) >= 6:
            dx, rx = self.pending[0], self.pending[5]
            self.pending.popleft()
            self.pending.popleft()
            code = self._combine(dx, rx)
            self.recent.append(VALID[dx[0]] or VALID[rx[0]])
            ch = self._char(code)
            is_open = len(self.recent) >= 4 and sum(self.recent) >= 0.7 * len(self.recent)
            if is_open:
                # silencieux à mémoire : on restitue ce qui est arrivé pendant son ouverture
                out.extend(c for c in self.held if c and c != "_")
                self.held.clear()
                if ch:
                    out.append(ch)
            else:
                self.held.append(ch)

    @staticmethod
    def _combine(dx, rx):
        """Copie valide, sinon somme des deux copies souples, sinon inversion du bit le moins sûr."""
        if VALID[dx[0]]:
            return dx[0]
        if VALID[rx[0]]:
            return rx[0]
        soft = np.asarray(dx[1]) + np.asarray(rx[1])
        code = sum((1 << i) for i, v in enumerate(soft) if v > 0)
        if VALID[code]:
            return code
        ones = bin(code).count("1")
        cand = [i for i in range(7) if ((code >> i) & 1) == (1 if ones > 4 else 0)]
        if abs(ones - 4) == 1 and cand:
            i = min(cand, key=lambda k: abs(soft[k]))
            return code ^ (1 << i)
        return None

    def _char(self, code):
        if code is None:
            return "_"
        if code in (CCIR_ALPHA, CCIR_REP, CCIR_CHAR32):
            return ""
        if code == CCIR_LTRS_SHIFT:
            self.figs = False
            return ""
        if code == CCIR_FIGS_SHIFT:
            self.figs = True
            return ""
        ch = (CCIR_FIGS if self.figs else CCIR_LTRS)[code]
        if ch in "_\x07":
            return ""
        return ch
