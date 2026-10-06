"""PACTOR I (SCS, 1991) en écoute : FSK 200 Hz, 100 ou 200 bauds (la vitesse change en cours de
liaison), cycle ARQ de 1,25 s dont 0,96 s de paquet.

- Paquet : en-tête 0x55, données (8 octets à 100 bauds, 20 à 200 bauds), octet d'état, CRC-16 CCITT
  (X.25) sur données + état ; octets émis bit de poids faible en premier. L'en-tête est lu 0x55 ou
  0xAA selon le paquet : les deux sens sont essayés, le CRC tranche.
- Octet d'état : bits 0-1 compteur de paquet (une répétition ARQ garde le même compteur), bits 2-3
  format (0 ASCII 8 bits, 1 Huffman, 2 Huffman casse inversée), bit 6 demande de changement de sens,
  bit 7 QRT (fin de liaison).
- Memory-ARQ : un paquet faux est gardé et additionné (valeurs souples) à ses répétitions, jusqu'à
  ce que le CRC passe.
- Paquet de synchronisation (appel) : en-tête puis indicatif de la station appelée, complété par 0x0F.
Les signaux de contrôle (CS1 à CS4) de la station qui reçoit ne portent pas de texte et sont ignorés.
"""
import time
from collections import deque

import numpy as np

from ..dsp import Decoder
from .fsk import FSKDemod

# Code de Huffman de PACTOR (table de l'annexe du protocole) : bits dans l'ordre d'émission.
HUFFMAN = {
    " ": "10", "e": "011", "n": "0101", "i": "1101", "r": "1110", "t": "00000", "s": "00100", "d": "00111",
    "a": "01000", "u": "11111", "l": "000010", "h": "000100", "g": "000111", "m": "001011", "\r": "001100",
    "\n": "001101", "o": "010010", "c": "010011", "b": "0000110", "f": "0000111", "w": "0001100", "D": "0001101",
    "k": "0010101", "z": "1100010", ".": "1100100", ",": "1100101", "S": "1111011", "A": "00101001",
    "E": "11000000", "p": "11000010", "v": "11000011", "0": "11000111", "F": "11001100", "B": "11001111",
    "C": "11110001", "I": "11110010", "T": "11110100", "O": "000101000", "P": "000101100", "1": "001010000",
    "R": "110000010", "(": "110011011", ")": "110011100", "L": "110011101", "N": "111100000", "Z": "111100110",
    "M": "111101010", "9": "0001010010", "W": "0001010100", "5": "0001010101", "y": "0001010110",
    "2": "0001011010", "3": "0001011011", "4": "0001011100", "6": "0001011101", "7": "0001011110",
    "8": "0001011111", "H": "0010100010", "J": "1100000110", "U": "1100000111", "V": "1100011000",
    "\x1c": "1100011001", "x": "1100011010", "K": "1100110100", "?": "1100110101", "=": "1111000010",
    "q": "1111010110", "Q": "1111010111", "j": "00010100110", "G": "00010100111", "-": "00010101111",
    ":": "00101000111", "!": "11110011101", "/": "11110011110", "*": "001010001100", '"': "110001101100",
    "%": "110001101101", "'": "110001101110", "_": "111100001100", "&": "111100111001", "+": "111100111110",
    ">": "111100111111", "@": "0001010111000", "$": "0001010111001", "<": "0001010111010",
    "X": "0001010111011", "#": "0010100011011", "Y": "00101000110101", ";": "11110000110100",
    "\\": "11110000110101", "[": "001010001101000", "]": "001010001101001", "\x7f": "110001101111000",
    "~": "110001101111001", "}": "110001101111010", "|": "110001101111011", "{": "110001101111100",
    "`": "110001101111101", "^": "110001101111110", "\x1f": "110001101111111", "\x1d": "111100001101100",
    "\x1b": "111100001101101", "\x19": "111100001101110", "\x18": "111100001101111", "\x17": "111100001110000",
    "\x16": "111100001110001", "\x15": "111100001110010", "\x14": "111100001110011", "\x13": "111100001110100",
    "\x12": "111100001110101", "\x11": "111100001110110", "\x10": "111100001110111", "\x1e": "111100001111000",
    "\x0f": "111100001111001", "\x0e": "111100001111010", "\x0c": "111100001111011", "\x0b": "111100001111100",
    "\t": "111100001111101", "\x08": "111100001111110", "\x07": "111100001111111", "\x06": "111100111000000",
    "\x05": "111100111000001", "\x04": "111100111000010", "\x03": "111100111000011", "\x02": "111100111000100",
    "\x01": "111100111000101", "\x00": "111100111000110", "\x1a": "111100111000111",
}
_DECODE = {v: k for k, v in HUFFMAN.items()}
IDLE = {0x00, 0x1E}
CALL_CHARS = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/")


def crc16(data):
    """CRC-16 CCITT réfléchi (X.25) : init 0xFFFF, sortie complémentée."""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0x8408 if c & 1 else c >> 1
    return c ^ 0xFFFF


def to_bytes(bits):
    bits = np.asarray(bits, np.uint8).reshape(-1, 8)
    return (bits << np.arange(8, dtype=np.uint8)).sum(axis=1).astype(np.uint8).tobytes()


def huffman_decode(data, swapped=False):
    bits = "".join(f"{b:08b}"[::-1] for b in data)
    end = bits.rfind("1") + 1          # bourrage final à 0 : ne pas le lire comme des « t »
    out, cur = [], ""
    for i, b in enumerate(bits):
        if not cur and i >= end:
            break
        cur += b
        c = _DECODE.get(cur)
        if c is not None:
            out.append(c.swapcase() if swapped else c)
            cur = ""
    return "".join(out)


def huffman_encode(text, swapped=False):
    return "".join(HUFFMAN[c.swapcase() if swapped else c] for c in text)


def packet_ok(bits, nbytes):
    """bits souples ou durs (en-tête compris) -> (données, état) si le CRC passe, sinon None."""
    b = to_bytes(bits)
    if crc16(b[1:nbytes - 2]) != (b[nbytes - 2] | (b[nbytes - 1] << 8)):
        return None
    return b[1:nbytes - 3], b[nbytes - 3]


def _hdr_dist(byte):
    return min(bin(byte ^ 0x55).count("1"), bin(byte ^ 0xAA).count("1"))


class Lane:
    """Une vitesse (100 ou 200 bauds) : horloge bit, recherche des paquets, Memory-ARQ."""

    def __init__(self, baud, fs2):
        self.baud = baud
        self.spb = fs2 / baud
        self.nbytes = 12 if baud < 150 else 24
        self.nbits = self.nbytes * 8
        self.phase = 0.0
        self.prev = 0.0
        self.acc = 0.0
        self.soft = deque(maxlen=self.nbits + 8)
        self.hold = 0                    # bits à ignorer après un paquet reconnu
        self.mem = deque(maxlen=4)       # paquets faux récents (valeurs souples, sens corrigé, instant)
        self.t = 0                       # compteur de bits
        self.good = 0

    def feed(self, d, out):
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
                self.soft.append(self.acc / (0.6 * spb))
                self.acc = 0.0
                self.t += 1
                if self.baud < 150 and len(self.soft) >= 72:
                    self._call(out)
                if self.hold:
                    self.hold -= 1
                elif len(self.soft) >= self.nbits:
                    self._check(out)

    def _check(self, out):
        s = np.array(self.soft)[-self.nbits:]
        hb = (s[:8] > 0).astype(np.uint8)
        hdr = int((hb << np.arange(8, dtype=np.uint8)).sum())
        if _hdr_dist(hdr) > 1:
            return
        r = packet_ok(s > 0, self.nbytes) or packet_ok(s < 0, self.nbytes)
        if bin(hdr ^ 0xAA).count("1") < bin(hdr ^ 0x55).count("1"):
            s = -s                           # Memory-ARQ : copies ramenées à l'en-tête 0x55
        if r is None and self.t % 2 == 0:
            r = self._memory(s)
        if r is not None:
            self.good += 1
            self.hold = self.nbits - 8
            self.mem.clear()
            out.append((self.baud, r[0], r[1]))
            return
        if _hdr_dist(hdr) == 0 and np.mean(np.abs(s)) > 0.3:
            self.mem.append((s, self.t))

    def _call(self, out):
        """Paquet de synchronisation : en-tête puis indicatif appelé (100 bauds), complété par 0x0F."""
        s = np.array(self.soft)[-72:]
        b = to_bytes(s > 0)
        if b[0] == 0xAA:
            b = bytes(x ^ 0xFF for x in b)
        if b[0] != 0x55:
            return
        body = b[1:9]
        call = body.split(b"\x0f")[0]
        if not (3 <= len(call) <= 8 and all(c in CALL_CHARS for c in call)):
            return
        if any(c != 0x0F for c in body[len(call):]) or not any(48 <= c <= 57 for c in call):
            return
        out.append(("call", call.decode(), None))

    def _memory(self, s):
        """Memory-ARQ : somme avec les paquets faux des cycles précédents (1,25 s, à ±2 bits)."""
        cyc = int(round(1.25 * self.baud))
        acc = s.copy()
        for m, t in reversed(self.mem):
            dt = self.t - t
            k = round(dt / cyc)
            if k < 1 or k > 6 or abs(dt - k * cyc) > 2:
                continue
            acc = acc + m
            r = packet_ok(acc > 0, self.nbytes) or packet_ok(acc < 0, self.nbytes)
            if r is not None:
                return r
        return None


class PactorI(Decoder):
    name = "PACTOR-I"

    def __init__(self, fs, af=1500.0, shift=200.0):
        super().__init__(fs, af)
        self.demod = FSKDemod(fs, af, 200.0, shift, bw_factor=0.6)
        fs2 = self.demod.fs2
        self.lanes = [Lane(200.0, fs2), Lane(100.0, fs2)]
        self.last = None
        self.last_time = 0.0
        self.speed = None
        self.fmt = 0
        self.packets = 0
        self.calls = {}
        self.announced = None
        self.cr = False

    def set_af(self, af):
        super().set_af(af)
        self.demod.set_af(af)

    def status(self):
        st = {"af": round(self.demod.af, 1), "snr": round(self.demod.snr(), 1), "last": self.packets}
        if self.speed:
            st["info"] = f"{self.speed:.0f} bauds, " + ("ASCII", "Huffman", "Huffman (casse inversée)", "?")[self.fmt & 3]
        return st

    def process(self, x):
        d = self.demod.process(np.asarray(x, np.float64))
        found = []
        for ln in self.lanes:
            ln.feed(d, found)
        out = []
        for baud, data, st in found:
            if baud == "call":
                self._call(data, out)
            else:
                self._packet(baud, data, st, out)
        return out

    def _call(self, call, out):
        """Un appel est répété à chaque cycle : on l'affiche à sa deuxième réception."""
        now = time.time()
        prev = self.calls.get(call)
        self.calls[call] = now
        if prev is not None and now - prev < 15 and self.announced != call:
            self.announced = call
            out.append({"t": "text", "text": f"\n[appel PACTOR vers {call}]\n"})

    def _packet(self, baud, data, st, out):
        now = time.time()
        cnt = st & 3
        key = (cnt, data)
        if key == self.last and now - self.last_time < 30:
            self.last_time = now
            return                                   # répétition ARQ (ou FEC) : déjà affiché
        self.last, self.last_time = key, now
        self.packets += 1
        self.speed = baud
        self.fmt = (st >> 2) & 3
        if self.fmt == 0:
            txt = "".join(chr(b) for b in data if b not in IDLE)
        elif self.fmt in (1, 2):
            txt = huffman_decode(data, swapped=self.fmt == 2)
            txt = txt.replace("\x1e", "").replace("\x00", "")
        else:
            txt = ""
        clean = []
        for c in txt:                                # CR LF, même coupé entre deux paquets
            if c == "\n" and self.cr:
                self.cr = False
                continue
            self.cr = c == "\r"
            if c in "\r\n":
                clean.append("\n")
            elif 32 <= ord(c) < 127 or ord(c) >= 160:
                clean.append(c)
        txt = "".join(clean)
        if st & 0x40:
            txt += " [changement de sens]"
        if st & 0x80:
            txt += "\n[QRT : fin de liaison]\n"
        if txt:
            out.append({"t": "text", "text": txt})
