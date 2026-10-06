"""ACARS (ARINC 618) : messages des avions en VHF (129 à 137 MHz), AM, MSK à 2400 bits/s sur une
sous-porteuse audio (1200 / 2400 Hz).

Démodulateur MSK cohérent et boucle de phase repris d'acarsdec (Thierry Leconte, LGPL-2) :
mélange à 1800 Hz, filtre adapté en demi-cosinus, horloge de bit à 2400 Hz, la boucle corrige
l'écart de fréquence. Trame : SYN SYN SOH, mode, immatriculation (7), ACK, étiquette (2), bloc,
STX, [n° de message (4), vol (6) dans le sens avion -> sol], texte, ETX / ETB, CRC-16. Caractères
de 7 bits + parité impaire, bit de poids faible en premier. Jusqu'à trois erreurs de parité sont
corrigées à l'aide du CRC (comme acarsdec).
"""
import itertools
import math
import time

import numpy as np

from ..dsp import Decoder

SYN, SOH, STX, ETX, ETB, DLE = 0x16, 0x01, 0x02, 0x83, 0x97, 0x7F
MAXPERR = 3
LABELS = {"_d": "sans texte", "_\x7f": "sans texte", "H1": "message équipage / ACARS", "Q0": "essai de liaison",
          "SQ": "squitter (station sol)", "5Z": "compagnie", "10": "départ", "11": "arrivée prévue",
          "12": "hors bloc", "13": "en vol", "14": "posé", "15": "au bloc", "16": "position", "20": "plan de vol",
          "80": "position (compagnie)", "B6": "ADS-C", "BA": "ADS-C", "AA": "CPDLC", "A6": "ADS-C",
          "QA": "hors bloc (OOOI)", "QB": "décollage (OOOI)", "QC": "atterrissage (OOOI)", "QD": "au bloc (OOOI)",
          "QE": "ETA", "QF": "décollage", "C1": "message ATC", "RA": "message imprimante", "2Z": "ETA",
          "4N": "compagnie", "44": "position", "47": "position", "H2": "météo", "RB": "message cabine"}


def _crc_table():
    t = []
    for b in range(256):
        c = b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
        t.append(c)
    return t


CRC_T = _crc_table()


def crc16(data, c=0):
    for b in data:
        c = (c >> 8) ^ CRC_T[(c ^ b) & 0xFF]
    return c


_SYN = {}


def syndrome(d, bit):
    """CRC d'une erreur sur `bit` d'un octet suivi de d octets (CRC compris) : le CRC est linéaire."""
    k = (d, bit)
    s = _SYN.get(k)
    if s is None:
        s = crc16(bytes([1 << bit]) + bytes(d))
        _SYN[k] = s
    return s


def odd(b):
    return bin(b).count("1") & 1


def repair(txt, crc):
    """txt : octets de SOH exclu jusqu'à ETX inclus ; crc : 2 octets. -> octets corrigés ou None."""
    txt = bytearray(txt)
    n = len(txt)
    r = crc16(bytes(txt) + bytes(crc))
    if r == 0:
        return txt
    crcerr = {syndrome(1 - i // 8, i % 8) for i in range(16)}          # erreur dans le CRC lui-même
    bad = [i for i, b in enumerate(txt) if not odd(b)]
    if len(bad) > MAXPERR:
        return None
    if bad:
        for bits in itertools.product(range(8), repeat=len(bad)):
            s = r
            for i, b in zip(bad, bits):
                s ^= syndrome(n - i - 1 + 2, b)
            if s == 0 or s in crcerr:
                for i, b in zip(bad, bits):
                    txt[i] ^= 1 << b
                return txt
        return None
    if r in crcerr:
        return txt
    for k in range(n):                       # deux bits faux dans un même octet (parité juste)
        d = n - k - 1 + 2
        for i in range(8):
            for j in range(i + 1, 8):
                if r ^ syndrome(d, i) ^ syndrome(d, j) == 0:
                    txt[k] ^= (1 << i) | (1 << j)
                    return txt
    return None


def parse(txt):
    """Bloc corrigé (sans parité, de mode à ETX) -> dict."""
    t = bytes(b & 0x7F for b in txt)
    if len(t) < 13:
        return None
    m = {"mode": chr(t[0]), "reg": t[1:8].decode("latin-1").replace(".", "").strip(),
         "ack": "NAK" if t[8] == 0x15 else chr(t[8]), "label": t[9:11].decode("latin-1"), "bid": chr(t[11]),
         "end": "ETB" if txt[-1] == ETB else "ETX"}
    body = t[13:-1]
    m["no"] = m["flight"] = ""
    if t[12] == STX and m["bid"].isdigit() and len(body) >= 10:
        m["no"], m["flight"] = body[:4].decode("latin-1"), body[4:10].decode("latin-1").strip()
        body = body[10:]
    elif t[12] != STX:
        body = b""
    m["text"] = "".join(chr(c) if 32 <= c < 127 or c in (10, 13) else "·" for c in body).replace("\r\n", "\n")
    m["text"] = m["text"].replace("\r", "\n").strip("\n")
    return m


def describe(m):
    lab = m["label"].replace("\x7f", "d")
    name = LABELS.get(m["label"]) or LABELS.get(lab, "")
    head = [f"ACARS {m['reg'] or '(sol)'}"]
    if m["flight"]:
        head.append(f"vol {m['flight']}")
    head.append(f"étiquette {lab}" + (f" ({name})" if name else ""))
    head.append(f"mode {m['mode']}")
    head.append(f"bloc {m['bid']}" + (" (avion → sol)" if m["bid"].isdigit() else " (sol → avion)" if m["bid"].isalpha() else ""))
    if m["no"]:
        head.append(f"n° {m['no']}")
    if m["ack"] not in ("\x15", "NAK") and m["ack"].strip():
        head.append(f"acquitte {m['ack']}")
    if m["end"] == "ETB":
        head.append("suite à venir")
    s = " · ".join(head)
    if m["text"]:
        s += "\n    " + m["text"].replace("\n", "\n    ")
    return s


class ACARS(Decoder):
    name = "ACARS"
    kind = "msg"

    def __init__(self, fs, af=1800.0, phase=0, _sub=False):
        super().__init__(fs, 1800.0)
        self.fs = float(fs)
        self.flen = int(self.fs / 1200) + 1
        over = 12
        n = self.flen * over + 1
        i = np.arange(n)
        self.h = np.maximum(np.cos(2 * np.pi * 600.0 / self.fs / over * (i - (n - 1) / 2)), 0.0)
        self.over = over
        self.inb = np.zeros(self.flen, np.complex128)
        self.idx = 0
        self.phi = 0.0
        self.clk = 0.0
        self.df = 0.0
        self.S = 0
        self.outbits = 0
        self.nbits = 8
        self.state = "wsyn"
        self.blk = None
        self.count = 0
        self.last = None
        self.lvl = 0.0
        # deux démodulateurs décalés d'un demi-bit et d'une voie (I/Q) : l'accrochage ne dépend plus du hasard
        self.S = phase
        self.clk = 0.75 * math.pi * phase
        self.subs = [] if _sub else [ACARS(fs, af, phase=1, _sub=True)]
        self.recent = []

    def status(self):
        return {"af": 1800.0, "last": self.count, "info": f"écart {self.df * self.fs / 2 / math.pi:+.0f} Hz"
                if self.state != "wsyn" else ""}

    def process(self, x):
        out = []
        x = np.asarray(x, np.float64)
        fs, flen, h, over = self.fs, self.flen, self.h, self.over
        inb = self.inb
        idx, p, clk = self.idx, self.phi, self.clk
        w0 = 1800.0 / fs * 2 * math.pi
        for v in x:
            s = w0 + self.df
            p += s
            if p >= 2 * math.pi:
                p -= 2 * math.pi
            inb[idx] = v * complex(math.cos(p), -math.sin(p))
            idx = (idx + 1) % flen
            clk += s
            if clk >= 3 * math.pi / 2 - s / 2:
                clk -= 3 * math.pi / 2
                o = min(int(over * (clk / s + 0.5)), over)
                taps = h[o: o + over * flen: over]
                z = np.dot(taps, np.roll(inb, -idx))
                lvl = abs(z)
                z /= lvl + 1e-8
                if self.S & 1:
                    vo = z.imag
                    dphi = -z.real if vo >= 0 else z.real
                else:
                    vo = z.real
                    dphi = z.imag if vo >= 0 else -z.imag
                self._putbit(-vo if self.S & 2 else vo, out)
                self.S += 1
                self.df = 0.52 * self.df + 0.48 * 38e-4 * dphi
        self.idx, self.phi, self.clk = idx, p, clk
        for sub in self.subs:
            out += sub.process(x)
        if self.subs:
            uniq = []
            for ev in out:
                if ev["text"] not in self.recent:
                    self.recent = (self.recent + [ev["text"]])[-20:]
                    uniq.append(ev)
                    self.count += 1
            out = uniq
        return out

    def _putbit(self, v, out):
        self.outbits = (self.outbits >> 1) | (0x80 if v > 0 else 0)
        self.nbits -= 1
        if self.nbits <= 0:
            self._byte(self.outbits, out)

    def _reset(self):
        self.state = "wsyn"
        self.df = 0.0
        self.nbits = 1

    def _byte(self, r, out):
        st = self.state
        self.nbits = 8
        if st == "wsyn":
            if r == SYN:
                self.state = "syn2"
            elif r == (~SYN & 0xFF):
                self.S ^= 2
                self.state = "syn2"
            else:
                self.nbits = 1
        elif st == "syn2":
            if r == SYN:
                self.state = "soh"
            elif r == (~SYN & 0xFF):
                self.S ^= 2
            else:
                self._reset()
        elif st == "soh":
            if r == SOH:
                self.blk, self.perr = bytearray(), 0
                self.state = "txt"
            else:
                self._reset()
        elif st == "txt":
            self.blk.append(r)
            if not odd(r):
                self.perr += 1
                if self.perr > MAXPERR + 1:
                    self._reset()
                    return
            if r in (ETX, ETB):
                self.state, self.crc = "crc1", bytearray()
            elif len(self.blk) > 240:
                self._reset()
        elif st == "crc1":
            self.crc.append(r)
            self.state = "crc2"
        elif st == "crc2":
            self.crc.append(r)
            self._finish(out)
            self.state = "end"
        else:
            self._reset()
            self.nbits = 8

    def _finish(self, out):
        blk = self.blk
        if len(blk) < 13:
            return
        blk[12] = (blk[12] & (ETX | STX)) | (ETX & STX)          # STX ou ETX après le n° de bloc
        fixed = repair(bytes(blk), bytes(self.crc))
        if fixed is None or any(not odd(b) for b in fixed):
            return
        m = parse(fixed)
        if m is None:
            return
        key = (m["reg"], m["label"], m["bid"], m["text"])
        if key == self.last:
            return
        self.last = key
        out.append({"t": "msg", "mode": "ACARS", "utc": time.strftime("%H%M%S", time.gmtime()), "text": describe(m)})
