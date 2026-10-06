"""Packet radio AX.25 : 1200 bauds AFSK (Bell 202, APRS en VHF, audio FM) et 300 bauds FSK (HF, 200 Hz).

1. Démodulation FSK (filtres mark/space et ATC, comme le RTTY) ; la polarité n'a pas d'importance, le
   codage NRZI ne regarde que les changements ;
2. synchro bit par boucle sur les transitions ; plusieurs démodulateurs en parallèle (largeurs de filtre
   différentes), comme Dire Wolf : une trame ratée par l'un est souvent reçue par l'autre ;
3. HDLC : fanions 0x7E, suppression des bits de bourrage, contrôle CRC-16 (FCS) ; une trame dont le
   CRC est faux est corrigée si un seul bit est en cause ;
4. AX.25 : adresses (indicatif-SSID), relais, champ d'information ; APRS : position décodée.
"""
import re
import time

import numpy as np

from ..dsp import Decoder
from .fsk import FSKDemod


def crc16_x25(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def _bytes(bits):
    a = np.asarray(bits, np.uint8).reshape(-1, 8)
    return bytes((a * (1 << np.arange(8))).sum(axis=1).astype(np.uint8))


def check_frame(bits, fix=True):
    """Trame (bits sans bourrage, fanions exclus) -> octets sans FCS, ou None. Corrige un bit faux."""
    if len(bits) % 8 or not (18 * 8 <= len(bits) <= 400 * 8):
        return None
    data = _bytes(bits)
    if crc16_x25(data[:-2]) == data[-2] | (data[-1] << 8):
        return data[:-2]
    if fix and len(bits) <= 120 * 8:
        b = np.array(bits, np.uint8)
        for i in range(len(b)):
            b[i] ^= 1
            d = _bytes(b)
            if crc16_x25(d[:-2]) == d[-2] | (d[-1] << 8):
                return d[:-2]
            b[i] ^= 1
    return None


def _call(a):
    call = "".join(chr(c >> 1) for c in a[:6]).strip()
    ssid = (a[6] >> 1) & 0x0F
    return call + (f"-{ssid}" if ssid else "")


def parse_ax25(f):
    """Octets d'une trame -> dict (source, destination, relais, type, info) ou None si incohérent."""
    addrs = []
    i = 0
    while i + 7 <= len(f):
        a = f[i:i + 7]
        if any(not (0x40 <= (c >> 1) <= 0x5A or (c >> 1) in range(0x30, 0x3A) or (c >> 1) == 0x20) for c in a[:6]):
            return None
        addrs.append((_call(a), bool(a[6] & 0x80)))
        i += 7
        if a[6] & 1:
            break
        if len(addrs) > 10:
            return None
    if len(addrs) < 2 or i >= len(f):
        return None
    ctrl = f[i]
    i += 1
    info = b""
    kind = "I" if ctrl & 1 == 0 else ("S" if ctrl & 3 == 1 else "U")
    if kind == "I" or (kind == "U" and ctrl & 0xEF == 0x03):          # I ou UI : PID puis information
        if i < len(f):
            i += 1
        info = f[i:]
        if kind == "U":
            kind = "UI"
    digis = [c + ("*" if h else "") for c, h in addrs[2:]]
    return {"dst": addrs[0][0], "src": addrs[1][0], "digis": digis, "kind": kind, "info": info}


def _printable(b):
    s = []
    for c in b:
        if c in (10, 13):
            s.append(" ")
        elif 32 <= c < 127:
            s.append(chr(c))
        elif c >= 0xA0:
            s.append(chr(c))
        else:
            s.append(f"<{c:02x}>")
    return "".join(s).strip()


APRS_POS = re.compile(r"(\d{2})(\d{2}\.\d{2})([NS]).(\d{3})(\d{2}\.\d{2})([EW])")


def aprs_position(info):
    """Position APRS non compressée (DDMM.mmN/DDDMM.mmE) -> (lat, lon) ou None."""
    m = APRS_POS.search(info)
    if not m:
        return None
    lat = int(m.group(1)) + float(m.group(2)) / 60
    lon = int(m.group(4)) + float(m.group(5)) / 60
    if m.group(3) == "S":
        lat = -lat
    if m.group(6) == "W":
        lon = -lon
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return round(lat, 5), round(lon, 5)


class _Hdlc:
    """Bits NRZI démodulés -> trames HDLC (bits utiles)."""

    def __init__(self):
        self.prev = 0
        self.raw = 0
        self.ones = 0
        self.bits = []
        self.inframe = False

    def bit(self, level):
        b = 1 if level == self.prev else 0           # NRZI : pas de changement = 1
        self.prev = level
        self.raw = ((self.raw << 1) | b) & 0xFF
        if self.raw == 0x7E:                          # fanion
            frame = self.bits[:-7] if self.inframe else None
            self.bits, self.ones, self.inframe = [], 0, True
            return frame if frame and len(frame) >= 18 * 8 else None
        if b:
            self.ones += 1
            if self.ones >= 7:                        # abandon
                self.inframe, self.bits = False, []
                return None
        else:
            if self.ones == 5:                        # bit de bourrage
                self.ones = 0
                return None
            self.ones = 0
        if self.inframe:
            self.bits.append(b)
            if len(self.bits) > 400 * 8:
                self.inframe, self.bits = False, []
        return None


class _Slicer:
    """Un démodulateur complet : discriminateur FSK, synchro bit, HDLC."""

    def __init__(self, fs, af, baud, shift, bw):
        self.demod = FSKDemod(fs, af, baud, shift, False, bw_factor=bw)
        self.spb = self.demod.fs2 / baud
        self.phase = 0.0
        self.prev = 0.0
        self.acc = 0.0
        self.hdlc = _Hdlc()

    def process(self, x, frames):
        d = self.demod.process(x)
        spb = self.spb
        for v in d:
            if (self.prev > 0) != (v > 0):
                err = self.phase if self.phase < spb / 2 else self.phase - spb
                self.phase -= 0.25 * err
            self.prev = v
            if 0.25 * spb <= self.phase <= 0.75 * spb:
                self.acc += v
            self.phase += 1
            if self.phase >= spb:
                self.phase -= spb
                f = self.hdlc.bit(1 if self.acc > 0 else 0)
                self.acc = 0.0
                if f is not None:
                    frames.append(f)


class AX25(Decoder):
    name = "Packet"
    kind = "msg"

    def __init__(self, fs, af=1700.0, baud=1200.0, shift=1000.0):
        super().__init__(fs, af)
        self.baud = float(baud)
        self.shift = float(shift)
        self.slicers = [_Slicer(fs, af, baud, shift, bw) for bw in (0.5, 0.7, 1.0)]
        self.recent = []                              # trames récentes (doublons des démodulateurs)
        self.count = 0

    def set_af(self, af):
        super().set_af(af)
        for s in self.slicers:
            s.demod.set_af(af)

    def status(self):
        d = self.slicers[0].demod
        return {"af": round(d.af, 1), "snr": round(d.snr(), 1), "last": self.count}

    def process(self, x):
        x = np.asarray(x, np.float64)
        raw = []
        for s in self.slicers:
            s.process(x, raw)
        out = []
        now = time.time()
        self.recent = [(t, f) for t, f in self.recent if now - t < 3]
        for bits in raw:
            f = check_frame(bits)
            if f is None or any(f == g for _, g in self.recent):
                continue
            self.recent.append((now, f))
            p = parse_ax25(f)
            if p is None:
                continue
            self.count += 1
            info = _printable(p["info"])
            path = ",".join([p["dst"]] + p["digis"])
            ev = {"t": "msg", "utc": time.strftime("%H%M%S", time.gmtime()), "freq": None,
                  "text": f"{p['src']}>{path}" + (f" [{p['kind']}]" if p["kind"] not in ("UI", "I") else "")
                          + (f": {info}" if info else "")}
            pos = aprs_position(info) if p["kind"] == "UI" else None
            if pos:
                ev["pos"] = pos
                ev["text"] += f"  ({pos[0]:.4f}, {pos[1]:.4f})"
            out.append(ev)
        return out
