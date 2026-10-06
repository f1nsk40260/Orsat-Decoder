"""Appel sélectif numérique : DSC maritime (ASN, ITU-R M.493) et selcall HF CCIR 493-4 (Codan, Barrett…).

- HF : 100 bauds, 170 Hz (1615 / 1785 Hz) ; VHF (canal 70) : 1200 bauds AFSK 1300 / 2100 Hz en FM.
- Caractères de 10 bits : 7 bits d'information (poids faible en premier), puis le nombre de bits à 0
  de ces 7 bits, sur 3 bits : chaque caractère se vérifie seul.
- Après le motif de points, la mise en phase (DX = 125, RX = 111 à 104) donne le cadrage et le sens ;
  chaque caractère est émis deux fois (positions DX et RX, 5 caractères d'écart) : on garde la copie valide.
- L'appel se termine par une fin de séquence (117, 122 ou 127) et un caractère de contrôle (ECC, OU
  exclusif des caractères du message), vérifié.
"""
import time
from collections import deque

import numpy as np

from ..dsp import Decoder
from .fsk import FSKDemod

FORMATS = {102: "zone géographique", 112: "DÉTRESSE", 114: "groupe", 116: "tous navires",
           120: "individuel", 123: "individuel semi-auto"}
CATEGORY = {100: "routine", 108: "sécurité", 110: "urgence", 112: "détresse"}
TC1 = {100: "F3E/G3E tous modes", 101: "F3E/G3E duplex", 103: "interrogation", 104: "impossible d'obtempérer",
       105: "fin d'appel", 106: "données", 109: "J3E téléphonie", 110: "accusé de détresse",
       112: "relais de détresse", 113: "F1B/J2B FEC", 115: "F1B/J2B ARQ", 118: "essai",
       121: "position du navire", 126: "—"}
TC2 = {100: "sans raison", 101: "encombrement", 102: "occupé", 103: "file d'attente", 104: "station interdite",
       105: "pas d'opérateur", 106: "opérateur indisponible", 107: "équipement hors service",
       108: "canal inutilisable", 109: "mode inutilisable", 110: "navires neutres (conflit)",
       111: "transport sanitaire", 112: "téléphone public", 113: "télécopie / données", 126: "—"}
NATURE = {100: "incendie, explosion", 101: "voie d'eau", 102: "abordage", 103: "échouement",
          104: "gîte, chavirement", 105: "naufrage", 106: "désemparé, à la dérive", 107: "non précisée",
          108: "abandon du navire", 109: "piraterie", 110: "homme à la mer", 112: "balise RLS"}
EOS = {117: "accusé demandé", 122: "accusé de réception", 127: "fin"}
PHASE_RX = set(range(104, 112))


def _chk(info):
    return 7 - sum(info)


def symbol(bits):
    """10 bits (0/1) -> (valeur, valide)."""
    info = bits[:7]
    v = sum(b << i for i, b in enumerate(info))
    c = bits[7] * 4 + bits[8] * 2 + bits[9]
    return v, c == _chk(info)


def digits(chars):
    return "".join(f"{c:02d}" if c is not None and c < 100 else "??" for c in chars)


def mmsi(chars):
    d = digits(chars)
    m = d[:9] if len(chars) == 5 else d
    kind = ""
    if len(chars) == 5:
        if m.startswith("00"):
            kind = " (station côtière)"
        elif m.startswith("0"):
            kind = " (groupe)"
        elif m.startswith("111"):
            kind = " (aéronef SAR)"
        elif m.startswith("970"):
            kind = " (SART)"
        elif m.startswith("972"):
            kind = " (homme à la mer)"
        elif m.startswith("974"):
            kind = " (RLS)"
    return m + kind


def frequency(chars):
    if all(c == 126 for c in chars):
        return "—"
    d = digits(chars)
    if "?" in d:
        return d
    if d[0] in "012":
        return f"{int(d) / 10:.1f} kHz".replace(".", ",")
    if d[0] == "4" and len(chars) == 4:
        return f"{int(d[1:]) / 100:.2f} kHz".replace(".", ",")
    if d[0] == "9":
        return f"VHF canal {int(d[2:])}"
    if d[0] == "3":
        return f"MF/HF voie {d[1:]}"
    return d


def position(chars):
    d = digits(chars)
    if d.startswith("9999") or "?" in d:
        return "position inconnue"
    q = int(d[0])
    ns, ew = ("N", "E") if q == 0 else ("N", "W") if q == 1 else ("S", "E") if q == 2 else ("S", "W")
    return f"{d[1:3]}°{d[3:5]}'{ns} {d[5:8]}°{d[8:10]}'{ew}"


def _name(tab, c):
    return tab.get(c, str(c) if c is not None else "?")


def parse(chars):
    """Caractères du message (spécificateur de format en tête, EOS et ECC en fin) -> texte."""
    if len(chars) >= 2 and chars[0] == chars[1] and chars[0] in FORMATS:
        chars = chars[1:]
    fmt, body, eos, ecc = chars[0], chars[1:-2], chars[-2], chars[-1]
    calc = 0
    for c in chars[:-1]:
        calc ^= (c or 0)
    ok = ecc is not None and None not in chars[:-1] and calc == ecc
    if ecc == eos and not ok:                              # selcall CCIR 493-4 : EOS répété, sans ECC
        ok = None
    head = f"DSC {_name(FORMATS, fmt)}"
    parts = []
    try:
        if fmt == 112:                                      # détresse
            parts = [f"de {mmsi(body[0:5])}", _name(NATURE, body[5]), position(body[6:11])]
            if len(body) >= 13:
                hh = digits(body[11:13])
                parts.append("heure inconnue" if hh.startswith("88") else f"{hh[:2]}:{hh[2:]} UTC")
            if len(body) >= 14:
                parts.append(_name(TC1, body[13]))
        else:
            if fmt in (116,):
                cat, rest = body[0], body[1:]
                addr = None
            else:
                # adresse de 5 caractères (MMSI, zone) ou de 2 / 3 (selcall HF CCIR 493-4)
                n = next((k for k in (5, 3, 2) if len(body) > 2 * k and body[k] in CATEGORY), 5)
                addr, cat, rest = body[:n], body[n], body[n + 1:]
            n2 = 5 if addr is None or len(addr) == 5 else len(addr)
            self_id, rest = rest[:n2], rest[n2:]
            parts.append(f"de {mmsi(self_id)}")
            if addr is not None:
                parts.append(("zone " + position(addr)) if fmt == 102 else f"à {mmsi(addr)}")
            parts.append(_name(CATEGORY, cat))
            if len(rest) >= 2:
                tc1 = rest[0]
                parts.append(_name(TC1, rest[0]))
                if rest[1] != 126:
                    parts.append(_name(TC2, rest[1]))
                rest = rest[2:]
                if cat == 112 and len(rest) >= 6:                # relais / accusé de détresse
                    parts.append(f"navire en détresse {mmsi(rest[0:5])}, {_name(NATURE, rest[5])}")
                    rest = rest[6:]
                    if len(rest) >= 5:
                        parts.append(position(rest[0:5]))
                        rest = rest[5:]
                elif len(rest) >= 5 and tc1 == 121:
                    parts.append(position(rest[0:5]))
                    rest = rest[5:]
                freqs = []
                while len(rest) >= 3:
                    k = 4 if rest[0] is not None and 40 <= rest[0] < 50 and len(rest) >= 4 else 3
                    freqs.append(frequency(rest[:k]))
                    rest = rest[k:]
                if freqs and any(f != "—" for f in freqs):
                    parts.append(" / ".join(freqs))
    except (IndexError, TypeError, ValueError):
        parts.append("champs : " + " ".join(str(c) for c in body))
    parts.append(_name(EOS, eos))
    return f"{head} : " + ", ".join(p for p in parts if p) + ("  [ECC faux]" if ok is False else ""), ok


class DSC(Decoder):
    name = "DSC"
    kind = "msg"

    def __init__(self, fs, af=1700.0, baud=100.0, shift=170.0):
        super().__init__(fs, af)
        self.baud = float(baud)
        # bit 1 (« Y ») = tonalité basse : le discriminateur est inversé
        self.demod = FSKDemod(fs, af, baud, shift, reverse=True, bw_factor=0.6)
        self.spb = self.demod.fs2 / self.baud
        self.phase = 0.0
        self.prev = 0.0
        self.acc = 0.0
        self.bits = deque(maxlen=600)
        self.nbits = 0
        self.align = None
        self.syms = []                     # symboles (valeur ou None) depuis le cadrage
        self.state = "search"
        self.inv = False
        self.count = 0
        self.quality = 0.0

    def set_af(self, af):
        super().set_af(af)
        self.demod.set_af(af)

    def status(self):
        return {"af": round(self.demod.af, 1), "snr": round(self.demod.snr(), 1), "quality": round(self.quality, 2),
                "last": self.count}

    def process(self, x):
        d = self.demod.process(np.asarray(x, np.float64))
        out = []
        spb = self.spb
        for v in d:
            if (self.prev > 0) != (v > 0):
                err = self.phase if self.phase < spb / 2 else self.phase - spb
                self.phase -= 0.15 * err
            self.prev = v
            if 0.2 * spb <= self.phase <= 0.8 * spb:
                self.acc += v
            self.phase += 1
            if self.phase >= spb:
                self.phase -= spb
                self._bit(1 if self.acc > 0 else 0, out)
                self.acc = 0.0
        return out

    # ------------------------------------------------------------------ cadrage et symboles
    def _bit(self, b, out):
        self.bits.append(b)
        self.nbits += 1
        if self.align is None and self.nbits % 5 == 0 and len(self.bits) >= 200:
            self._find_alignment(out)
        elif self.align is not None and (self.nbits - self.align) % 10 == 0:
            s = list(self.bits)[-10:]
            v, ok = symbol(s)
            self.quality += 0.1 * ((1.0 if ok else 0.0) - self.quality)
            self._symbol(v if ok else None, out)

    def _find_alignment(self, out):
        """Cadrage : parmi les 10 décalages, celui où l'on voit des caractères valides et ceux de la mise
        en phase (125 en DX, 104 à 111 en RX, ou leurs inverses), sur les 20 derniers caractères."""
        W = 200
        b = list(self.bits)[-W:]
        phase_set = {125, 2} | PHASE_RX | {127 - c for c in PHASE_RX}
        best = None
        for off in range(10):
            syms = [symbol(b[i:i + 10]) for i in range(off, W - 9, 10)]
            vals = [v for v, ok in syms if ok]
            n = len(vals)
            phasing = sum(v in phase_set for v in vals)
            sc = n + 2 * phasing
            if best is None or sc > best[0]:
                best = (sc, off, n, phasing)
        if best[2] >= 9 and best[3] >= 4:
            self.align = 0
            self.syms = []
            # les caractères déjà reçus (mise en phase) passent dans l'automate
            last = best[1]
            for i in range(best[1], W - 9, 10):
                v, ok = symbol(b[i:i + 10])
                self._symbol(v if ok else None, out)
                last = i
            self.align = (self.nbits - (W - last - 10)) % 10

    def _symbol(self, v, out):
        self.syms.append(v)
        s = self.syms
        if self.state == "search":
            if len(s) > 40:
                del s[:-40]
            # mise en phase : DX = 125 un symbole sur deux, RX = 111..104 entre eux (ou l'inverse en sens inversé)
            for inv in (False, True):
                f = (lambda c: None if c is None else 127 - c) if inv else (lambda c: c)
                vals = [f(c) for c in s[-12:]]
                for par in (0, 1):
                    dx = vals[par::2]
                    rx = vals[1 - par::2]
                    if sum(c == 125 for c in dx) >= 3 and sum(c in PHASE_RX for c in rx) >= 2:
                        self.state, self.inv = "phasing", inv
                        self.pairs = []
                        # par = 0 : le dernier symbole est un RX, le suivant sera un DX
                        self.slot_par = par
                        self.cur_dx = vals[-1]
                        return
            if len(s) > 30 and sum(c is None for c in s[-30:]) > 20:
                self.align = None                             # plus de signal : on recherche le cadrage
            return
        if self.inv and v is not None:
            v = 127 - v
        # pendant la phase et le message : paires (DX, RX)
        if self.slot_par == 0:
            self.cur_dx = v
            self.slot_par = 1
            return
        self.slot_par = 0
        self.pairs.append((self.cur_dx, v))
        if self.state == "phasing":
            if self.cur_dx not in (125, None):
                self.state = "msg"
                self.m0 = len(self.pairs) - 1
            elif len(self.pairs) > 20:
                self._reset()
            return
        self._try_finish(out)

    def _char(self, k):
        """Caractère k du message : copie DX (paire m0+k), sinon copie RX (paire m0+k+2)."""
        p = self.m0 + k
        dx = self.pairs[p][0] if p < len(self.pairs) else None
        rx = self.pairs[p + 2][1] if p + 2 < len(self.pairs) else None
        return dx if dx is not None else rx

    def _try_finish(self, out):
        n = len(self.pairs) - self.m0
        chars = [self._char(k) for k in range(n)]
        for k, c in enumerate(chars):
            if k >= 3 and c in EOS:
                # ECC (k+1) : attendre sa copie RX, ou la fin du signal
                if n >= k + 4 or (n >= k + 2 and chars[k + 1] is not None):
                    txt, ok = parse(chars[:k + 2])
                    self.count += 1
                    out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()), "text": txt})
                    self._reset()
                return
        if n > 60 or (n > 6 and all(c is None for c in chars[-6:])):
            if n > 6:
                txt, _ = parse(chars + [None, None])
                out.append({"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()), "text": txt + " (incomplet)"})
            self._reset()

    def _reset(self):
        self.state = "search"
        self.syms = []
        self.pairs = []
        self.align = None
