"""Briques communes aux modes multi-tonalités de fldigi : MFSK, DominoEX et THOR.

- tables des sous-modes (copiées de mfsk.cxx, dominoex.cxx, thor.cxx) ;
- codage convolutif K=7 (ou K=15 pour les THOR rapides), décodeur de Viterbi à décisions souples ;
- entrelaceur « diagonal » de fldigi (interleave.cxx), écrit sous forme de lignes à retard équivalentes ;
- démodulateur de tonalités : TFD glissante sur une grille fine de fréquences, avec acquisition
  automatique de la synchro symbole et de la fréquence (± 2 écarts, 30 Hz minimum) et suivi de dérive ;
- détection IFK+ souple (DominoEX, THOR), décodage Varicode (registre « datashreg » de fldigi)
  et silencieux à mémoire.
"""
from collections import deque

import numpy as np
from scipy.special import i0e

from ..dsp import Mixer, FirDecim, lowpass

# ------------------------------------------------------------------ sous-modes
# MFSK : fréquence d'échantillonnage d'origine, symlen, bits/symbole, profondeur d'entrelacement,
# nombre de tonalités, longueur de préambule (bits)
MFSK_MODES = {
    "MFSK4":    (8000, 2048, 5, 5, 32, 107),
    "MFSK8":    (8000, 1024, 5, 5, 32, 107),
    "MFSK11":   (11025, 1024, 4, 10, 16, 107),
    "MFSK16":   (8000, 512, 4, 10, 16, 107),
    "MFSK22":   (11025, 512, 4, 10, 16, 107),
    "MFSK31":   (8000, 256, 3, 10, 8, 107),
    "MFSK32":   (8000, 256, 4, 10, 16, 107),
    "MFSK64":   (8000, 128, 4, 10, 16, 180),
    "MFSK128":  (8000, 64, 4, 20, 16, 214),
    "MFSK64L":  (8000, 128, 4, 400, 16, 2500),
    "MFSK128L": (8000, 64, 4, 800, 16, 5000),
}

# DominoEX : fréquence d'origine, symlen, multiplicateur d'écart (« doublespaced »)
DOMINO_MODES = {
    "DominoEX Micro": (8000, 4000, 1),
    "DominoEX 4":  (8000, 2048, 2),
    "DominoEX 5":  (11025, 2048, 2),
    "DominoEX 8":  (8000, 1024, 2),
    "DominoEX 11": (11025, 1024, 1),
    "DominoEX 16": (8000, 512, 1),
    "DominoEX 22": (11025, 512, 1),
    "DominoEX 44": (11025, 256, 2),
    "DominoEX 88": (11025, 128, 1),
}

# THOR : fréquence d'origine, symlen, écart, profondeur d'entrelacement, longueur de vidage, K=15 ?
THOR_MODES = {
    "THOR Micro": (8000, 4000, 1, 4, 4, False),
    "THOR 4":   (8000, 2048, 2, 10, 4, False),
    "THOR 5":   (11025, 2048, 2, 10, 4, False),
    "THOR 8":   (8000, 1024, 2, 10, 4, False),
    "THOR 11":  (11025, 1024, 1, 10, 4, False),
    "THOR 16":  (8000, 512, 1, 10, 4, False),
    "THOR 22":  (11025, 512, 1, 10, 4, False),
    "THOR 25x4": (8000, 320, 4, 50, 40, True),
    "THOR 50x1": (8000, 160, 1, 50, 40, True),
    "THOR 50x2": (8000, 160, 2, 50, 40, True),
    "THOR 100": (8000, 80, 1, 50, 40, True),
}

IFK_TONES = 18
K7 = (7, 0x6d, 0x4f)                 # coefficients « NASA »
K15 = (15, 0o44735, 0o63057)         # THOR 25x4, 50x1, 50x2, 100


def gray_tx(v):
    """« grayencode » de fldigi (misc.cxx) : donnée -> numéro de tonalité (OU exclusif cumulé)."""
    b = v
    for i in range(1, 8):
        b ^= v >> i
    return b


def gray_rx(t):
    """« graydecode » de fldigi : numéro de tonalité -> donnée."""
    return t ^ (t >> 1)


def _parity(x):
    return bin(int(x)).count("1") & 1


# ------------------------------------------------------------------ codage convolutif
class ConvEncoder:
    """Codeur de fldigi (viterbi.cxx) : registre « shreg = shreg<<1 | bit », 2 bits de sortie par bit,
    bit 0 = polynôme 1 (émis le premier), bit 1 = polynôme 2."""

    def __init__(self, k=7, p1=0x6d, p2=0x4f):
        self.mask = (1 << k) - 1
        self.out = [_parity(i & p1) | (_parity(i & p2) << 1) for i in range(1 << k)]
        self.shreg = 0

    def encode(self, bit):
        self.shreg = ((self.shreg << 1) | (1 if bit else 0)) & 0xFFFFFFFF
        return self.out[self.shreg & self.mask]


class Viterbi:
    """Décodeur de Viterbi à décisions souples (rapports de vraisemblance, > 0 = bit 1).

    Même convention d'états que fldigi : le bit le plus récent est en poids faible.
    Qualité : gain de la meilleure métrique sur W pas, rapporté au gain maximal possible (somme des |LLR|).
    Un code de rendement 1/2 « explique » encore ~86 % du bruit pur ; un signal décodable donne > 92 %.
    La valeur est retardée pour correspondre aux bits qui sortent du décodeur."""

    def __init__(self, k=7, p1=0x6d, p2=0x4f, traceback=45, chunk=4):
        ns = 1 << (k - 1)
        h = ns // 2
        n = np.arange(ns)
        out = np.array([_parity(i & p1) | (_parity(i & p2) << 1) for i in range(1 << k)])
        # état n = 2i+b : prédécesseurs i (bit sortant 0) et i+h (bit sortant 1)
        self.o0 = out[n].reshape(h, 2).astype(np.intp)
        self.o1 = out[n + ns].reshape(h, 2).astype(np.intp)
        self.ns, self.h = ns, h
        self.tb, self.chunk = int(traceback), int(chunk)
        self.c0 = np.empty((h, 2), np.float32)
        self.c1 = np.empty((h, 2), np.float32)
        self.bm = np.empty(4, np.float32)
        self.reset()

    def reset(self):
        self.m = np.zeros(self.ns, np.float32)
        self.hist = []
        self.quality = 0.0
        self.W = 96                                   # fenêtre de mesure de qualité (pas)
        self.cum = np.zeros(self.W)                   # progression cumulée de la meilleure métrique
        self.cuma = np.zeros(self.W)                  # somme cumulée des |LLR|
        self.S = self.Sa = self.qs = 0.0
        self.t = 0
        self.qh = deque(maxlen=max(1, self.tb + self.chunk // 2 - self.W // 2))

    def step(self, l0, l1):
        """l0, l1 : LLR des deux bits codés (polynôme 1 puis 2). Renvoie la liste des bits décidés."""
        l0, l1 = 0.5 * float(l0), 0.5 * float(l1)
        a = abs(l0) + abs(l1)
        bm, c0, c1, h = self.bm, self.c0, self.c1, self.h
        bm[0] = -l0 - l1
        bm[1] = l0 - l1
        bm[2] = -bm[1]
        bm[3] = -bm[0]
        np.take(bm, self.o0, out=c0)
        c0 += self.m[:h, None]
        np.take(bm, self.o1, out=c1)
        c1 += self.m[h:, None]
        d = c1 > c0
        np.maximum(c0, c1, out=c0)
        top = float(c0.max())
        self.m = c0.ravel() - top
        self.S += top
        self.Sa += a
        i = self.t % self.W
        dS, dA = self.S - self.cum[i], self.Sa - self.cuma[i]
        self.cum[i], self.cuma[i] = self.S, self.Sa
        self.t += 1
        if self.t >= self.W and dA > 1e-9:
            self.qs += 0.1 * (dS / dA - self.qs)
            self.qh.append(self.qs)
            self.quality = self.qh[0]
        self.hist.append(d.ravel())
        if len(self.hist) < self.tb + self.chunk:
            return []
        s = int(np.argmax(self.m))
        bits = []
        for t in range(len(self.hist) - 1, -1, -1):
            if t < self.chunk:
                bits.append(s & 1)
            s = (s >> 1) + (h if self.hist[t][s] else 0)
        del self.hist[:self.chunk]
        return bits[::-1]


def soft_bits(L, mask):
    """LLR de chaque bit à partir des log-vraisemblances L des valeurs possibles ; mask (nbits, nval) booléen."""
    e = np.exp(L - L.max())
    s1 = (mask * e).sum(axis=1)
    s0 = (~mask * e).sum(axis=1)
    return np.clip(np.log(s1 + 1e-30) - np.log(s0 + 1e-30), -30, 30)


# ------------------------------------------------------------------ entrelaceur
class Interleaver:
    """Entrelaceur de fldigi (interleave.cxx). Pour un symbole de `size` bits et `depth` étages, il équivaut
    exactement à des lignes à retard : à l'émission le bit i est retardé de i*depth symboles, à la réception
    de (size-1-i)*depth symboles. Contenu initial : 0 à l'émission, poinçonnage (LLR nul) à la réception."""

    def __init__(self, size, depth, rx=False):
        self.size = size
        self.delay = [((size - 1 - i) if rx else i) * depth for i in range(size)]
        self.n = max(self.delay) + 1
        self.buf = np.zeros((self.n, size))
        self.ptr = 0
        self.count = 0
        self.warm = 8          # symboles de marge (le décodeur y ajoute le retard de sa mesure de qualité)

    def symbols(self, syms):
        """syms : `size` valeurs (bits ou LLR) dans l'ordre d'émission. Renvoie les valeurs entrelacées."""
        self.buf[self.ptr] = syms
        out = np.array([self.buf[(self.ptr - self.delay[i]) % self.n, i] for i in range(self.size)])
        self.ptr = (self.ptr + 1) % self.n
        self.count += 1
        return out

    @property
    def filled(self):
        """Vrai quand le contenu initial (poinçonné) est sorti : avant, un bit sur deux peut manquer et
        n'importe quelle suite paraît être un mot de code (qualité Viterbi trompeuse)."""
        return self.count >= self.n + self.warm

    def bits(self, v):
        """Version entière (émission) : v = `size` bits, premier bit en poids fort."""
        syms = [(v >> (self.size - 1 - i)) & 1 for i in range(self.size)]
        out = self.symbols(syms)
        r = 0
        for b in out:
            r = (r << 1) | int(b)
        return r


# ------------------------------------------------------------------ Varicode
class VariShreg:
    """Registre de décodage Varicode IZ8BLY de fldigi : un caractère se termine sur la suite « 001 »."""

    def __init__(self, table):
        self.table = table
        self.reg = 1

    def bit(self, b):
        self.reg = ((self.reg << 1) | (1 if b else 0)) & 0xFFFFFFFF
        if (self.reg & 7) == 1:
            c = self.table.get(self.reg >> 1, -1)
            self.reg = 1
            return c
        if self.reg > 0xFFFFF:          # trop long : bruit
            self.reg = 1
        return None


def printable(c):
    """Caractère primaire affichable (None sinon). CR devient saut de ligne."""
    if c is None or c < 0 or c & 0x100:
        return None
    if c == 13:
        return "\n"
    if c == 10:
        return "\r"                     # marqueur : LF ignoré s'il suit un CR (voir TextOut)
    if c >= 32 and c != 127:
        return chr(c) if c < 128 else bytes([c]).decode("latin-1")
    return None


class TextOut:
    """Sortie texte avec silencieux à mémoire : tant que le silencieux est fermé on garde les derniers
    caractères, rendus s'il se rouvre peu après (signal faible qui papillote)."""

    def __init__(self, hold=4):
        self.held = []
        self.hold = hold
        self.last = ""
        self.closed = 0                 # caractères reçus depuis la dernière ouverture

    def put(self, ch, open_):
        if ch is None:
            return ""
        if ch == "\r":                  # LF de fldigi : saut de ligne seulement s'il n'y a pas eu de CR
            if self.last == "\n":
                return ""
            ch = "\n"
        if open_:
            # après un long silence, la mémoire ne contient que du bruit ou une transition : on la jette
            s = ("".join(self.held) if self.closed <= self.hold else "") + ch
            self.held.clear()
            self.closed = 0
            self.last = ch
            return s
        self.closed += 1
        self.held = (self.held + [ch])[-self.hold:]
        return ""

    def drop(self):
        self.held.clear()


# ------------------------------------------------------------------ démodulateur de tonalités
def logi0(x):
    return x + np.log(i0e(x))


def lse(v, axis=-1):
    m = np.max(v, axis=axis, keepdims=True)
    return np.squeeze(m, axis) + np.log(np.sum(np.exp(v - m), axis=axis))


class ToneDemod:
    """Démodulateur commun : renvoie, symbole par symbole, l'énergie de chacune des `ntones` tonalités
    (normalisée par le bruit d'une case), avec synchro symbole et fréquence trouvées dans le signal.

    Principe : bande de base complexe décimée, puis TFD à fenêtre rectangulaire d'un symbole calculée sur
    une grille de fréquences au quart de la vitesse de modulation et pour J positions temporelles réparties
    sur une période symbole. Pour chaque hypothèse (position j, décalage q du peigne de tonalités) on cumule
    le maximum d'énergie sur le peigne ; le maximum de ce tableau donne la synchro et la fréquence.
    La grille suit la dérive (recentrage) dans une plage supplémentaire égale à la plage d'acquisition."""

    P = 4                 # finesse de la grille de fréquences (cases par baud)

    def __init__(self, fs, af, baud, ntones, spacing, nacc=None, low_tone=False):
        self.low_tone = low_tone
        self.fs, self.baud, self.M, self.ds = float(fs), float(baud), int(ntones), int(spacing)
        sp = self.ds * self.baud
        self.sp = sp
        self.margin = max(2 * sp, 30.0)                # plage d'acquisition autour du clic
        self.room = self.margin                        # dérive admise au-delà
        half = self.M * sp / 2 + self.baud
        cutoff = half + self.margin + self.room
        D = max(1, int(self.fs // (2.6 * cutoff)))
        # décimation en deux étages si elle est forte (filtres plus courts)
        D1 = max(1, D // 4) if D >= 8 else 1
        D2 = max(1, D // D1)
        self.fs1 = self.fs / D1
        self.fs2 = self.fs1 / D2
        self.mix = Mixer(af, self.fs)
        self.fir1 = None
        if D1 > 1:
            tw = self.fs1 - 2 * cutoff
            self.fir1 = FirDecim(lowpass(self.fs, self.fs1 / 2, 4 * self.fs / tw), D1)
        if D2 > 1:
            tw = self.fs2 - 2 * cutoff
            self.fir2 = FirDecim(lowpass(self.fs1, self.fs2 / 2, 4 * self.fs1 / tw), D2)
        else:                                          # pas de décimation : simple passe-bas
            fc = min(1.25 * cutoff, 0.45 * self.fs1)
            self.fir2 = FirDecim(lowpass(self.fs1, fc, 8 * self.fs1 / cutoff), 1)
        self.L = self.fs2 / self.baud                  # échantillons par symbole (fractionnaire)
        self.Lw = int(round(self.L))
        self.tstep = max(1, int(round(self.L / 16)))
        self.J = max(4, int(round(self.L / self.tstep)))
        self.Jc = self.J // 2
        self.N = int(round(self.P * self.L))           # longueur de TFD (fenêtre complétée de zéros)
        self.step = self.fs2 / self.N                  # pas de la grille ≈ baud / P
        self.Qh = int(np.ceil(self.margin / self.step))
        self.Q = 2 * self.Qh + 1
        tsp = self.ds * self.P                         # écart entre tonalités, en cases
        self.G = self.Qh + tsp * (self.M - 1) // 2 + 1
        base = self.G - self.Qh - tsp * (self.M - 1) // 2
        self.idx = np.arange(self.Q)[:, None] + tsp * np.arange(self.M)[None, :] + base
        # à égalité (préambule sur une seule tonalité), on préfère le peigne le plus proche du clic
        self.bias = 1e-6 * np.abs(np.arange(self.Q) - self.Qh)[None, :]
        self.nacc = nacc or int(np.clip(round(2.5 * self.baud), 10, 40))
        self.af0 = float(af)
        self.c = 0.0                                   # centre de la grille par rapport au mélangeur (Hz)
        self._cols()
        self.reset()

    def _cols(self):
        """Cases de la TFD formant la grille de fréquences centrée sur c (FFT plutôt que produit
        matriciel : pas de BLAS multi-thread, qui s'effondre quand la machine est chargée)."""
        cb = int(round(self.c / self.step))
        self.cols = (np.arange(2 * self.G + 1) - self.G + cb) % self.N

    def reset(self):
        self.buf = np.zeros(0, np.complex128)
        self.base = 0                                  # indice absolu du premier échantillon de buf
        self.pos = float(self.Jc * self.tstep)         # début estimé du prochain symbole (indice absolu)
        self.A = np.zeros((self.J, self.Q))
        self.nsym = 0
        self.qbest = self.Qh
        self.snr_lin = 0.0
        self.sig = 1.0                                 # énergie moyenne de la tonalité émise (bruit = 1)
        self.off_count = 0
        self.fcorr = 0.0                               # correction fine de fréquence (CAF), Hz
        self.frate = 0.0                               # dérive estimée, Hz par symbole
        self.lock = False                              # positionné par le décodeur (silencieux ouvert)

    def set_af(self, af):
        self.af0 = float(af)
        self.mix.freq = float(af)
        self.c = 0.0
        self._cols()
        self.A[:] = 0
        self.nsym = 0
        self.qbest = self.Qh
        self.fcorr = self.frate = 0.0

    def centre(self):
        return self.mix.freq + self.c + self.fcorr + (self.qbest - self.Qh) * self.step

    def snr_db(self):
        """S/B estimé dans 2500 Hz."""
        return 10 * np.log10(max(self.snr_lin, 1e-3) * self.baud / 2500.0)

    def feed(self, x):
        z = self.mix.process(np.asarray(x, np.float64))
        if self.fir1 is not None:
            z = self.fir1.process(z)
        z = self.fir2.process(z)
        self.buf = np.concatenate([self.buf, z])

    def symbols(self):
        """Générateur : pour chaque symbole complet disponible, renvoie le vecteur des énergies des tonalités."""
        J, ts, Lw = self.J, self.tstep, self.Lw
        offs = (np.arange(J) - self.Jc) * ts
        win = offs[:, None] + np.arange(Lw)[None, :]
        while True:
            p0 = int(round(self.pos)) - self.base
            if p0 + offs[0] < 0:                       # rattrapage après un grand saut en arrière
                self.pos = self.base + self.Jc * ts
                continue
            if p0 + offs[-1] + Lw > len(self.buf):
                break
            X = self.buf[p0 + win]
            if self.fcorr:
                X = X * np.exp(-2j * np.pi * self.fcorr / self.fs2 * np.arange(Lw))[None, :]
            E = np.abs(np.fft.fft(X, self.N, axis=1)[:, self.cols]) ** 2      # (J, F)
            # bruit d'une case ; plancher à -17 dB sous la case la plus forte (signal propre ou silence)
            noise = max(np.median(E) / 0.693, E.max() / 50.0) + 1e-20
            E = E / noise
            met = E[:, self.idx].max(axis=2)           # (J, Q)
            if self.low_tone:
                # préambule MFSK de fldigi : la tonalité la plus basse seule. À égalité entre peignes, on
                # préfère celui dont la tonalité forte est en première place (léger bonus)
                met = met + 0.02 * E[:, self.idx[:, 0]]
            self.nsym += 1
            a = max(1.0 / self.nacc, 1.0 / self.nsym)
            self.A += a * (met - self.A)
            j, q = np.unravel_index(int(np.argmax(self.A - self.bias)), self.A.shape)
            self.qbest = q
            T = E[j, self.idx[q]]
            mx = T.max()
            self._afc(E[j], self.idx[q, int(np.argmax(T))], mx)
            self.sig += 0.05 * (max(mx - 1.0, 0.3) - self.sig)
            self.snr_lin += 0.05 * (max(mx - 1.0, 0.0) - self.snr_lin)
            # synchro symbole : sommet (interpolé) du cumul, ramené au centre de la fenêtre de recherche
            e = j - self.Jc
            col = self.A[:, q]
            ym, y0, yp = col[(j - 1) % J], col[j], col[(j + 1) % J]
            den = ym - 2 * y0 + yp
            frac = 0.5 * (ym - yp) / den if den < 0 else 0.0
            e += float(np.clip(frac, -0.5, 0.5))
            if abs(e) >= 1.5:
                k = int(round(e))
                self.pos += k * ts
                self.A = np.roll(self.A, -k, axis=0)
            else:
                self.pos += 0.1 * e * ts
            self.pos += self.L
            # fréquence : recentrage de la grille si le peigne reste écarté (dérive)
            dq = q - self.Qh
            self.off_count = self.off_count + 1 if abs(dq) > self.Qh // 2 else 0
            if self.off_count > self.nacc and self.nsym > 3 * self.nacc and self.lock:
                newc = float(np.clip(self.c + dq * self.step, -self.room, self.room))
                k = int(round((newc - self.c) / self.step))
                if k:
                    self.c += k * self.step
                    self._cols()
                    A = np.full_like(self.A, self.A.min())
                    if k > 0:
                        A[:, :-k] = self.A[:, k:]
                    else:
                        A[:, -k:] = self.A[:, :k]
                    self.A = A
                    self.qbest = q - k
                    self.off_count = 0
            # purge du tampon
            keep = int(round(self.pos)) - self.base + offs[0] - 2
            if keep > 4096:
                self.buf = self.buf[keep:]
                self.base += keep
            yield T

    def _afc(self, row, col, mx):
        """CAF fine (2e ordre) : position interpolée de la tonalité décidée sur la grille. La correction est
        appliquée avant la TFD, si bien que le cumul A reste valable pendant que la grille suit la dérive."""
        if mx < 4.0 or col < 1 or col >= len(row) - 1:
            return
        am, a0, ap = np.sqrt(row[col - 1]), np.sqrt(row[col]), np.sqrt(row[col + 1])
        den = am - 2 * a0 + ap
        if den >= 0:
            return
        d = float(np.clip(0.5 * (am - ap) / den, -0.5, 0.5)) * self.step     # Hz
        # hors verrouillage, seuls les symboles nettement au-dessus du bruit comptent (pas de dérive sur du bruit)
        w = min(1.0, (mx - 4.0) / 12.0) if self.lock else 0.5 * min(1.0, max(0.0, (mx - 8.0) / 12.0))
        if w <= 0:
            return
        self.frate = float(np.clip(self.frate + 0.004 * w * d, -0.1 * self.step, 0.1 * self.step))
        self.fcorr += 0.08 * w * d + self.frate
        lim = self.margin + self.room - abs(self.c)
        self.fcorr = float(np.clip(self.fcorr, -lim, lim))

    def tone_llr(self, T):
        """Log-vraisemblance (à une constante près) de chaque tonalité : canal de Rice, bruit unitaire."""
        return logi0(np.minimum(2.0 * np.sqrt(self.sig * np.maximum(T, 0.0)), 1e4))


class IFKDiff:
    """Détection IFK+ non cohérente : la donnée c (0..15) est l'écart (tonalité courante - précédente - 2)
    modulo 18. Vraisemblance de c = somme sur la tonalité précédente p de L(p) + L((p+c+2) mod 18) ;
    on utilise donc l'énergie des deux symboles, sans dépendre d'une décision ferme sur le précédent."""

    def __init__(self):
        p = np.arange(IFK_TONES)
        c = np.arange(16)
        self.idx = (p[None, :] + c[:, None] + 2) % IFK_TONES      # (16, 18)
        self.prev = None
        # bits de c, dans l'ordre d'émission (poids fort d'abord), comme decodesymbol() de fldigi
        self.bits = np.array([[(v >> (3 - k)) & 1 for v in range(16)] for k in range(4)], bool)

    def metric(self, lt):
        """lt : log-vraisemblance des 18 tonalités du symbole courant. Renvoie L(c) (16 valeurs) ou None."""
        prev, self.prev = self.prev, lt
        if prev is None:
            return None
        return lse(prev[None, :] + lt[self.idx], axis=1)

    def bit_llr(self, Lc):
        return soft_bits(Lc, self.bits)
