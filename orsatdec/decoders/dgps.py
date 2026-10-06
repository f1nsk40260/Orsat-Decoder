"""DGPS maritime (ITU-R M.823, RTCM SC-104 version 2) : balises de correction GPS en ondes moyennes
(283,5 à 325 kHz), MSK à 100 ou 200 bauds.

- Démodulation : le carré du signal MSK porte deux raies à 2·fc ± débit/2, qui donnent la porteuse
  exacte (CAF) ; détection différentielle sur un bit (le saut de phase de ±90° donne le bit) ;
  quatre phases d'échantillonnage en parallèle, les trames en double sont écartées.
- Trames RTCM : mots de 30 bits (24 de données + 6 de parité, codage du GPS : D29*/D30* du mot
  précédent, données complémentées si D30* = 1), préambule 0x66, en-tête de deux mots (type,
  station de référence, Z-count modifié, séquence, longueur, santé). La parité rend le décodage
  insensible au sens (USB ou LSB).
- Messages lus : 1 et 9 (corrections de pseudo-distance), 3 (position de la station de
  référence), 7 (almanach des balises), 16 (texte) ; les autres sont nommés.
"""
import math
import time
from collections import deque

import numpy as np
from scipy.signal import lfilter, lfilter_zi

from ..dsp import Decoder, FirDecim, Mixer, lowpass

FS2 = 2400.0
PREAMBLE = 0x66
TYPES = {1: "corrections différentielles", 2: "corrections delta", 3: "position de la station",
         4: "repère géodésique", 5: "santé de la constellation", 6: "trame nulle", 7: "almanach des balises",
         9: "corrections (groupe de satellites)", 10: "corrections P", 14: "semaine GPS", 15: "ionosphère",
         16: "texte", 17: "éphémérides", 18: "RTK phase", 19: "RTK code", 20: "RTK phase (corr.)",
         21: "RTK code (corr.)", 22: "position station (complément)", 23: "antenne", 24: "position ARP",
         27: "almanach étendu", 31: "corrections GLONASS", 32: "position station GLONASS",
         34: "corrections GLONASS (groupe)", 59: "propriétaire"}
HEALTH = {0: "", 1: "UDRE ×0,75", 2: "UDRE ×0,5", 3: "UDRE ×0,3", 4: "UDRE ×0,2", 5: "UDRE ×0,1",
          6: "non surveillée", 7: "HORS SERVICE"}
BITRATES = (25, 50, 100, 110, 150, 200, 250, 300)

# équations de parité GPS (ICD-200) : bits de données utilisés, et bit précédent (29 ou 30) ajouté
_PAR = [((1, 2, 3, 5, 6, 10, 11, 12, 13, 14, 17, 18, 20, 23), 29),
        ((2, 3, 4, 6, 7, 11, 12, 13, 14, 15, 18, 19, 21, 24), 30),
        ((1, 3, 4, 5, 7, 8, 12, 13, 14, 15, 16, 19, 20, 22), 29),
        ((2, 4, 5, 6, 8, 9, 13, 14, 15, 16, 17, 20, 21, 23), 30),
        ((1, 3, 5, 6, 7, 9, 10, 14, 15, 16, 17, 18, 21, 22, 24), 30),
        ((3, 5, 6, 8, 9, 10, 11, 13, 15, 19, 22, 23, 24), 29)]
_MASK = [sum(1 << (24 - i) for i in idx) for idx, _ in _PAR]


def _pop(v):
    return bin(v).count("1") & 1


def parity(data, d29, d30):
    """24 bits de données (entier, d1 en poids fort) + D29*, D30* -> 6 bits de parité (entier)."""
    p = 0
    for m, (_, prev) in zip(_MASK, _PAR):
        p = (p << 1) | (_pop(data & m) ^ (d29 if prev == 29 else d30))
    return p


def check_word(w, d29, d30):
    """Mot reçu de 30 bits (entier, d1 en poids fort) -> 24 bits de données, ou None si parité fausse."""
    data = w >> 6
    if d30:
        data ^= 0xFFFFFF
    return data if parity(data, d29, d30) == (w & 0x3F) else None


def encode_word(data, d29, d30):
    """24 bits de données -> mot de 30 bits émis (pour les mires de test)."""
    p = parity(data, d29, d30)
    return ((data ^ 0xFFFFFF if d30 else data) << 6) | p


def _bits(words, pos, n):
    """Champ de n bits à la position pos dans la suite des mots de données (24 bits chacun)."""
    v = 0
    for k in range(pos, pos + n):
        v = (v << 1) | ((words[k // 24] >> (23 - k % 24)) & 1)
    return v


def _signed(v, n):
    return v - (1 << n) if v >> (n - 1) else v


def ecef_to_geo(x, y, z):
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    h = 0.0
    for _ in range(6):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / max(math.cos(lat), 1e-12) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    return math.degrees(lat), math.degrees(lon), h


def _latlon(lat, lon):
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'} {abs(lon):.4f}° {'E' if lon >= 0 else 'O'}"


def zcount(z):
    s = z * 0.6
    return f"H+{int(s // 60):02d}:{s % 60:04.1f}"


def corrections(words):
    """Messages 1 et 9 -> liste de (PRN, PRC m, RRC m/s, IOD, UDRE)."""
    out = []
    for k in range(len(words) * 24 // 40):
        p = k * 40
        scale = _bits(words, p, 1)
        udre = _bits(words, p + 1, 2)
        prn = _bits(words, p + 3, 5) or 32
        prc = _signed(_bits(words, p + 8, 16), 16)
        rrc = _signed(_bits(words, p + 24, 8), 8)
        iod = _bits(words, p + 32, 8)
        if prc == -32768:                      # satellite à ne pas utiliser
            out.append((prn, None, None, iod, udre))
            continue
        out.append((prn, prc * (0.32 if scale else 0.02), rrc * (0.032 if scale else 0.002), iod, udre))
    return out


def almanac(words):
    """Message 7 -> liste de balises (dict)."""
    out = []
    for k in range(len(words) // 3):
        p = k * 72
        lat = _signed(_bits(words, p, 16), 16) * 0.002747
        lon = _signed(_bits(words, p + 16, 16), 16) * 0.005493
        rng = _bits(words, p + 32, 10)
        freq = _bits(words, p + 42, 12) * 0.1 + 190.0
        health = _bits(words, p + 54, 2)
        sid = _bits(words, p + 56, 10)
        rate = BITRATES[_bits(words, p + 66, 3)]
        out.append({"lat": lat, "lon": lon, "range": rng, "freq": freq, "health": health, "id": sid, "baud": rate})
    return out


def text16(words):
    s = []
    for w in words:
        for sh in (16, 8, 0):
            c = (w >> sh) & 0xFF
            if c == 0:
                return "".join(s)
            s.append(chr(c) if 32 <= c < 127 else "·")
    return "".join(s).rstrip()


def describe(frame):
    """Trame décodée -> texte (sans le préfixe de station)."""
    t, words = frame["type"], frame["words"]
    if t in (1, 9):
        cs = corrections(words)
        return "corrections " + ", ".join(
            f"PRN {prn} {'à ne pas utiliser' if prc is None else f'{prc:+.2f} m'}" for prn, prc, _, _, _ in cs)
    if t == 3 and len(words) >= 4:
        x, y, z = (_signed(_bits(words, i * 32, 32), 32) * 0.01 for i in range(3))
        lat, lon, h = ecef_to_geo(x, y, z)
        return f"station de référence {_latlon(lat, lon)}, h {h:.0f} m"
    if t == 7:
        return "almanach : " + " ; ".join(
            f"balise {b['id']} {_latlon(b['lat'], b['lon'])} {b['freq']:.1f} kHz {b['baud']} bd {b['range']} km"
            for b in almanac(words))
    if t == 16:
        return "texte : " + text16(words)
    return f"type {t} ({TYPES.get(t, 'inconnu')}), {len(words)} mots"


class Framer:
    """Suite de bits -> trames RTCM SC-104 dont tous les mots ont une parité juste."""

    def __init__(self):
        self.reg = 0          # 32 derniers bits (D29*, D30* puis le mot)
        self.n = 0
        self.state = 0        # 0 recherche, 1 en-tête 2, 2 corps
        self.cnt = 0
        self.head = None
        self.words = []
        self.need = 0
        self.good = 0
        self.bad = 0

    def bit(self, b):
        self.reg = ((self.reg << 1) | b) & 0xFFFFFFFF
        self.n += 1
        if self.state == 0:
            if self.n < 32:
                return None
            d = check_word(self.reg & 0x3FFFFFFF, (self.reg >> 31) & 1, (self.reg >> 30) & 1)
            if d is not None and d >> 16 == PREAMBLE:
                self.head, self.state, self.cnt = d, 1, 0
            return None
        self.cnt += 1
        if self.cnt < 30:
            return None
        self.cnt = 0
        d = check_word(self.reg & 0x3FFFFFFF, (self.reg >> 31) & 1, (self.reg >> 30) & 1)
        if d is None:
            self.bad += 1
            self.state = 0
            return None
        self.good += 1
        if self.state == 1:
            self.head2, self.words, self.need = d, [], (d >> 3) & 0x1F
            self.state = 2
        else:
            self.words.append(d)
        if self.state == 2 and len(self.words) >= self.need:
            h1, h2 = self.head, self.head2
            self.state = 0
            return {"type": (h1 >> 10) & 0x3F, "station": h1 & 0x3FF, "z": h2 >> 11, "seq": (h2 >> 8) & 7,
                    "health": h2 & 7, "words": list(self.words)}
        return None


class DGPS(Decoder):
    name = "DGPS"
    kind = "msg"

    def __init__(self, fs, af=1000.0, baud=200.0, every=30.0):
        super().__init__(fs, af)
        self.baud = float(baud)
        self.every = float(every)
        D = max(1, int(round(fs / FS2)))
        self.fs2 = fs / D
        self.mix = Mixer(af, fs)
        self.dec = FirDecim(lowpass(fs, 0.4 * self.fs2, int(8 * D) | 1), D)
        self.lp = lowpass(self.fs2, 0.75 * self.baud, 31)
        self.zi = lfilter_zi(self.lp, 1.0).astype(np.complex128) * 0
        self.T = int(round(self.fs2 / self.baud))
        self.off = None                         # écart fin de porteuse (Hz), trouvé par la CAF
        self.fph = 0.0
        self.afc = deque()
        self.afc_n = 0
        self.afc_next = 3.0 * self.fs2
        self.hist = np.zeros(self.T, np.complex128)
        self.g = 0
        self.lanes = [Framer() for _ in range(4)]
        self.seen = deque(maxlen=64)
        self.count = 0
        self.station = None
        self.health = 0
        self.pending = {}                       # station -> {PRN: (prc, udre)}
        self.last_emit = {}
        self.last_other = {}
        self.locked = False

    def set_af(self, af):
        super().set_af(af)
        self.mix.freq = float(af)
        self.off = None
        self.afc.clear()
        self.afc_n = 0
        self.afc_next = 3.0 * self.fs2

    def status(self):
        st = {"af": round(self.af + (self.off or 0.0), 1), "last": self.count}
        if self.station is not None:
            st["info"] = f"station {self.station}" + (f", {HEALTH[self.health]}" if HEALTH.get(self.health) else "")
        elif self.off is None:
            st["info"] = "recherche de la porteuse"
        return st

    # ------------------------------------------------------------------ porteuse (CAF)
    def _afc(self):
        z = np.concatenate(list(self.afc))
        n = len(z)
        nfft = 1 << int(math.ceil(math.log2(n * 2)))
        S = np.abs(np.fft.fft((z * z) * np.hanning(n), nfft)) ** 2
        df = self.fs2 / nfft
        R = int(round(self.baud / df / 2))
        span = int(min(150.0, self.fs2 / 4) * 2 / df)
        k = np.arange(-span, span + 1)
        a, b = S[(k - R) % nfft], S[(k + R) % nfft]
        sc = np.minimum(a, b)
        i = int(np.argmax(sc))
        ref = np.median(S[(np.arange(-2 * span, 2 * span) % nfft)]) + 1e-30
        if sc[i] / ref < 20.0:
            return
        # interpolation parabolique sur les deux raies
        fine = 0.0
        for c in ((k[i] - R), (k[i] + R)):
            y0, y1, y2 = S[(c - 1) % nfft], S[c % nfft], S[(c + 1) % nfft]
            den = y0 - 2 * y1 + y2
            fine += 0.5 * (y0 - y2) / den if den else 0.0
        f2 = (k[i] + fine / 2) * df
        self.off = f2 / 2

    # ------------------------------------------------------------------ flux
    def process(self, x):
        z = self.dec.process(self.mix.process(np.asarray(x, np.float64)))
        if len(z) == 0:
            return []
        self.afc.append(z)
        self.afc_n += len(z)
        while self.afc_n - len(self.afc[0]) > 8 * self.fs2:
            self.afc_n -= len(self.afc.popleft())
        self.afc_next -= len(z)
        if self.afc_next <= 0:
            self.afc_next = 2.0 * self.fs2
            self._afc()
        if self.off is None:
            self.g += len(z)
            return []
        w = 2 * np.pi * self.off / self.fs2
        ph = self.fph + w * np.arange(len(z))
        self.fph = (self.fph + w * len(z)) % (2 * np.pi)
        y, self.zi = lfilter(self.lp, 1.0, z * np.exp(-1j * ph), zi=self.zi)
        full = np.concatenate([self.hist, y])
        d = np.imag(full[self.T:] * np.conj(full[:-self.T]))
        self.hist = full[-self.T:]
        out = []
        step = self.T / 4
        g0 = self.g
        for lane in range(4):
            o = int(round(lane * step))
            first = (o - g0) % self.T
            for i in range(first, len(d), self.T):
                fr = self.lanes[lane].bit(1 if d[i] > 0 else 0)
                if fr is not None:
                    self._frame(fr, out)
        self.g += len(z)
        return out

    def _frame(self, fr, out):
        key = (fr["type"], fr["station"], fr["z"], fr["seq"], tuple(fr["words"]))
        if key in self.seen:
            return
        self.seen.append(key)
        self.count += 1
        self.station, self.health = fr["station"], fr["health"]
        now = time.time()
        st = fr["station"]
        head = f"Station {st} · {zcount(fr['z'])}"
        if fr["health"]:
            head += f" · {HEALTH[fr['health']]}"
        t = fr["type"]
        if t in (1, 9):
            p = self.pending.setdefault(st, {})
            for prn, prc, _, _, _ in corrections(fr["words"]):
                p[prn] = prc
            if now - self.last_emit.get(st, -1e9) >= self.every:
                self.last_emit[st] = now
                txt = f"type {t}, corrections " + ", ".join(
                    f"PRN {k} {'à ne pas utiliser' if v is None else f'{v:+.2f} m'}" for k, v in sorted(p.items()))
                self.pending[st] = {}
                out.append({"t": "msg", "mode": "DGPS", "utc": time.strftime("%H%M%S", time.gmtime(now)),
                            "text": f"{head} · {txt}"})
            return
        if t == 6:
            return
        if t not in (3, 7, 16) and now - self.last_other.get((st, t), -1e9) < 60:
            return
        self.last_other[(st, t)] = now
        out.append({"t": "msg", "mode": "DGPS", "utc": time.strftime("%H%M%S", time.gmtime(now)),
                    "text": f"{head} · {describe(fr)}"})
