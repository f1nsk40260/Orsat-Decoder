"""JS8 (JS8Call, KN4CRD) : messagerie en 8-FSK dérivée de FT8, créneaux de 15 s (normal), 10 s (rapide),
6 s (turbo) ou 30 s (lent). 79 symboles : trois matrices de Costas 7x7 encadrent 58 symboles de
données (29 de parité puis 29 de message), code LDPC (174, 87), 75 bits utiles + CRC-12.

Les 72 bits de message forment des trames JS8Call : balise (@HB) ou CQ, indicatif composé, message
dirigé (indicatif, destinataire, commande), données en texte libre compressées (dictionnaire JSC de
262 144 mots) ou en Huffman. Les tables (matrices de Costas, code LDPC, alphabets, commandes,
dictionnaire JSC) viennent des sources de JS8Call (GPL-3) ; le décodeur est écrit pour Orsat-Decoder.
"""
import logging
import lzma
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from ..dsp import Decoder

log = logging.getLogger("orsat.js8")

# matrice de parité du code (174, 87) : contrôles de chaque bit (mot de code = 87 bits de parité puis 87 bits)
MN = (
    (0, 24, 68), (1, 4, 72), (2, 31, 67), (3, 50, 60), (5, 62, 69), (6, 32, 78), (7, 49, 85), (8, 36, 42),
    (9, 40, 64), (10, 13, 63), (11, 74, 76), (12, 22, 80), (14, 15, 81), (16, 55, 65), (17, 52, 59), (18, 30, 51),
    (19, 66, 83), (20, 28, 71), (21, 23, 43), (25, 34, 75), (26, 35, 37), (27, 39, 41), (29, 53, 54), (33, 48, 86),
    (38, 56, 57), (44, 73, 82), (45, 61, 79), (46, 47, 84), (58, 70, 77), (0, 49, 52), (1, 46, 83), (2, 24, 78),
    (3, 5, 13), (4, 6, 79), (7, 33, 54), (8, 35, 68), (9, 42, 82), (10, 22, 73), (11, 16, 43), (12, 56, 75),
    (14, 26, 55), (15, 27, 28), (17, 18, 58), (19, 39, 62), (20, 34, 51), (21, 53, 63), (23, 61, 77), (25, 31, 76),
    (29, 71, 84), (30, 64, 86), (32, 38, 50), (36, 47, 74), (37, 69, 70), (40, 41, 67), (44, 66, 85), (45, 80, 81),
    (48, 65, 72), (57, 59, 65), (60, 64, 84), (0, 13, 20), (1, 12, 58), (2, 66, 81), (3, 31, 72), (4, 35, 53),
    (5, 42, 45), (6, 27, 74), (7, 32, 70), (8, 48, 75), (9, 57, 63), (10, 47, 67), (11, 18, 44), (14, 49, 60),
    (15, 21, 25), (16, 71, 79), (17, 39, 54), (19, 34, 50), (22, 24, 33), (23, 62, 86), (26, 38, 73), (28, 77, 82),
    (29, 69, 76), (30, 68, 83), (21, 36, 85), (37, 40, 80), (41, 43, 56), (46, 52, 61), (51, 55, 78), (59, 74, 80),
    (0, 38, 76), (1, 15, 40), (2, 30, 53), (3, 35, 77), (4, 44, 64), (5, 56, 84), (6, 13, 48), (7, 20, 45),
    (8, 14, 71), (9, 19, 61), (10, 16, 70), (11, 33, 46), (12, 67, 85), (17, 22, 42), (18, 63, 72), (23, 47, 78),
    (24, 69, 82), (25, 79, 86), (26, 31, 39), (27, 55, 68), (28, 62, 65), (29, 41, 49), (32, 36, 81), (34, 59, 73),
    (37, 54, 83), (43, 51, 60), (50, 52, 71), (57, 58, 66), (46, 55, 75), (0, 18, 36), (1, 60, 74), (2, 7, 65),
    (3, 59, 83), (4, 33, 38), (5, 25, 52), (6, 31, 56), (8, 51, 66), (9, 11, 14), (10, 50, 68), (12, 13, 64),
    (15, 30, 42), (16, 19, 35), (17, 79, 85), (20, 47, 58), (21, 39, 45), (22, 32, 61), (23, 29, 73), (24, 41, 63),
    (26, 48, 84), (27, 37, 72), (28, 43, 80), (34, 67, 69), (40, 62, 75), (44, 48, 70), (49, 57, 86), (47, 53, 82),
    (12, 54, 78), (76, 77, 81), (0, 1, 23), (2, 5, 74), (3, 55, 86), (4, 43, 52), (6, 49, 82), (7, 9, 27),
    (8, 54, 61), (10, 28, 66), (11, 32, 39), (13, 15, 19), (14, 34, 72), (16, 30, 38), (17, 35, 56), (18, 45, 75),
    (20, 41, 83), (21, 33, 58), (22, 25, 60), (24, 59, 64), (26, 63, 79), (29, 36, 65), (31, 44, 71), (37, 50, 85),
    (40, 76, 78), (42, 55, 67), (46, 73, 81), (39, 51, 77), (53, 60, 70), (45, 57, 68),
)
# matrice génératrice : bits de parité = GEN · message (une ligne hexadécimale par bit de parité)
GEN = (
    "23bba830e23b6b6f50982e", "1f8e55da218c5df3309052", "ca7b3217cd92bd59a5ae20",
    "56f78313537d0f4382964e", "6be396b5e2e819e373340c", "293548a138858328af4210",
    "cb6c6afcdc28bb3f7c6e86", "3f2a86f5c5bd225c961150", "849dd2d63673481860f62c",
    "56cdaec6e7ae14b43feeee", "04ef5cfa3766ba778f45a4", "c525ae4bd4f627320a3974",
    "41fd9520b2e4abeb2f989c", "7fb36c24085a34d8c1dbc4", "40fc3e44bb7d2bb2756e44",
    "d38ab0a1d2e52a8ec3bc76", "3d0f929ef3949bd84d4734", "45d3814f504064f80549ae",
    "f14dbf263825d0bd04b05e", "db714f8f64e8ac7af1a76e", "8d0274de71e7c1a8055eb0",
    "51f81573dd4049b082de14", "d8f937f31822e57c562370", "b6537f417e61d1a7085336",
    "ecbd7c73b9cd34c3720c8a", "3d188ea477f6fa41317a4e", "1ac4672b549cd6dba79bcc",
    "a377253773ea678367c3f6", "0dbd816fba1543f721dc72", "ca4186dd44c3121565cf5c",
    "29c29dba9c545e267762fe", "1616d78018d0b4745ca0f2", "fe37802941d66dde02b99c",
    "a9fa8e50bcb032c85e3304", "83f640f1a48a8ebc0443ea", "3776af54ccfbae916afde6",
    "a8fc906976c35669e79ce0", "f08a91fb2e1f78290619a8", "cc9da55fe046d0cb3a770c",
    "d36d662a69ae24b74dcbd8", "40907b01280f03c0323946", "d037db825175d851f3af00",
    "1bf1490607c54032660ede", "0af7723161ec223080be86", "eca9afa0f6b01d92305edc",
    "7a8dec79a51e8ac5388022", "9059dfa2bb20ef7ef73ad4", "6abb212d9739dfc02580f2",
    "f6ad4824b87c80ebfce466", "d747bfc5fd65ef70fbd9bc", "612f63acc025b6ab476f7c",
    "05209a0abb530b9e7e34b0", "45b7ab6242b77474d9f11a", "6c280d2a0523d9c4bc5946",
    "f1627701a2d692fd9449e6", "8d9071b7e7a6a2eed6965e", "bf4f56e073271f6ab4bf80",
    "c0fc3ec4fb7d2bb2756644", "57da6d13cb96a7689b2790", "a9fa2eefa6f8796a355772",
    "164cc861bdd803c547f2ac", "cc6de59755420925f90ed2", "a0c0033a52ab6299802fd2",
    "b274db8abd3c6f396ea356", "97d4169cb33e7435718d90", "81cfc6f18c35b1e1f17114",
    "481a2a0df8a23583f82d6c", "081c29a10d468ccdbcecb6", "2c4142bf42b01e71076acc",
    "a6573f3dc8b16c9d19f746", "c87af9a5d5206abca532a8", "012dee2198eba82b19a1da",
    "b1ca4ea2e3d173bad4379c", "b33ec97be83ce413f9acc8", "5b0f7742bca86b8012609a",
    "37d8e0af9258b9e8c5f9b2", "35ad3fb0faeb5f1b0c30dc", "6114e08483043fd3f38a8a",
    "cd921fdf59e882683763f6", "95e45ecd0135aca9d6e6ae", "2e547dd7a05f6597aac516",
    "14cd0f642fc0c5fe3a65ca", "3a0a1dfd7eee29c2e827e0", "c8b5dffc335095dcdcaf2a",
    "3dd01a59d86310743ec752", "8abdb889efbe39a510a118", "3f231f212055371cf3e2a2",
)

COSTAS = {"original": ((4, 2, 5, 6, 1, 3, 0),) * 3,
          "modified": ((0, 6, 2, 3, 5, 4, 1), (1, 5, 0, 2, 3, 6, 4), (2, 5, 0, 6, 4, 1, 3))}
# sous-mode : (nom, échantillons par symbole à 12 kHz, durée du créneau, retard d'émission, Costas)
SUBMODES = {"A": ("normal", 1920, 15, 0.5, "original"), "B": ("rapide", 1200, 10, 0.2, "modified"),
            "C": ("turbo", 600, 6, 0.1, "modified"), "E": ("lent", 3840, 30, 0.5, "modified")}
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-+"
ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ /@"
NBASECALL = 37 * 36 * 10 * 27 * 27 * 27
NBASEGRID = 180 * 180
NUSERGRID = NBASEGRID + 10
NMAXGRID = (1 << 15) - 1
GROUPS = ["<....>", "@ALLCALL", "@JS8NET", "@DX/NA", "@DX/SA", "@DX/EU", "@DX/AS", "@DX/AF", "@DX/OC", "@DX/AN",
          "@REGION/1", "@REGION/2", "@REGION/3"] + [f"@GROUP/{i}" for i in range(10)] + [
          "@COMMAND", "@CONTROL", "@NET", "@NTS"] + [f"@RESERVE/{i}" for i in range(5)] + [
          "@APRSIS", "@RAGCHEW", "@JS8", "@EMCOMM", "@ARES", "@MARS", "@AMRRON", "@RACES", "@RAYNET", "@RADAR",
          "@SKYWARN", "@CQ", "@HB", "@QSO", "@QSOPARTY", "@CONTEST", "@FIELDDAY", "@SOTA", "@IOTA", "@POTA",
          "@QRP", "@QRO"]
BASECALLS = {NBASECALL + 1 + i: g for i, g in enumerate(GROUPS)}
CMDS = {0: " SNR?", 1: " DIT DIT", 2: " NACK", 3: " HEARING?", 4: " GRID?", 5: ">", 6: " STATUS?", 7: " STATUS",
        8: " HEARING", 9: " MSG", 10: " MSG TO:", 11: " QUERY", 12: " QUERY MSGS", 13: " QUERY CALL", 14: " ACK",
        15: " GRID", 16: " INFO?", 17: " INFO", 18: " FB", 19: " HW CPY?", 20: " SK", 21: " RR", 22: " QSL?",
        23: " QSL", 24: " CMD", 25: " SNR", 26: " NO", 27: " YES", 28: " 73", 29: " HEARTBEAT SNR", 30: " AGN?",
        31: " "}
SNR_CMDS = {25, 29}
CQS = {0: "CQ CQ CQ", 1: "CQ DX", 2: "CQ QRP", 3: "CQ CONTEST", 4: "CQ FIELD", 5: "CQ FD", 6: "CQ CQ", 7: "CQ"}
HUFF = {" ": "01", "E": "100", "T": "1101", "A": "0011", "O": "11111", "I": "11100", "N": "10111", "S": "10100",
        "H": "00011", "R": "00000", "D": "111011", "L": "110011", "C": "110001", "U": "101101", "M": "101011",
        "W": "001011", "F": "001001", "G": "000101", "Y": "000011", "P": "1111011", "B": "1111001", ".": "1110100",
        "V": "1100101", "K": "1100100", "-": "1100001", "+": "1100000", "?": "1011001", "!": "1011000",
        '"': "1010101", "X": "1010100", "0": "0010101", "J": "0010100", "1": "0010001", "Q": "0010000",
        "2": "0001001", "Z": "0001000", "3": "0000101", "5": "0000100", "4": "11110101", "9": "11110100",
        "8": "11110001", "6": "11110000", "7": "11101011", "/": "11101010"}
_HUFF_DEC = {v: k for k, v in HUFF.items()}
FIRST, LAST, DATA = 1, 2, 4

# ---------------------------------------------------------------------------------------- codes
_G = np.array([[(int(h[c // 4], 16) >> (3 - c % 4)) & 1 for c in range(87)] for h in GEN], np.uint8)
_EV = np.array([b for b, chks in enumerate(MN) for _ in chks])            # arête -> bit
_EC = np.array([c for chks in MN for c in chks])                            # arête -> contrôle
_ORDER = np.argsort(_EC, kind="stable")
_CSTART = np.searchsorted(_EC[_ORDER], np.arange(87))


def crc12(bits75):
    """CRC-12 « augmenté » (polynôme 0xC06) des 75 bits suivis de 13 zéros, ou exclusif 42."""
    reg = 0
    for b in list(bits75) + [0] * 13:
        reg = (reg << 1) | int(b)
        if reg & 0x1000:
            reg ^= 0x1C06
    return (reg & 0xFFF) ^ 42


def encode_bits(msg87):
    m = np.asarray(msg87, np.uint8)
    return np.concatenate([(_G.astype(int) @ m) % 2, m]).astype(np.uint8)


def bp_decode(llr, iters=30):
    """Propagation de croyance (somme-produit) sur le code LDPC (174, 87) ; llr > 0 : bit 1."""
    llr = np.asarray(llr, float)
    tov = np.zeros(len(_EV))
    for _ in range(iters + 1):
        zn = llr + np.bincount(_EV, tov, 174)
        cw = (zn > 0).astype(np.uint8)
        if not np.any(np.bincount(_EC, cw[_EV], 87).astype(int) & 1):
            return cw
        toc = zn[_EV] - tov
        t = np.tanh(np.clip(-toc / 2, -9, 9))
        t = np.where(np.abs(t) < 1e-9, 1e-9, t)
        lp = np.log(np.abs(t))
        neg = (t < 0).astype(int)
        sl = np.bincount(_EC, lp, 87)[_EC] - lp
        sn = (np.bincount(_EC, neg, 87)[_EC].astype(int) - neg) & 1
        prod = np.exp(sl) * np.where(sn, -1.0, 1.0)
        tov = 2 * np.arctanh(np.clip(-prod, -0.999999, 0.999999))
    return None


def tones_for(msg12, i3, costas):
    """12 caractères + type de trame -> 79 tonalités (pour les mires)."""
    bits = []
    for ch in msg12:
        v = ALPHABET.index(ch)
        bits += [(v >> (5 - k)) & 1 for k in range(6)]
    bits += [(i3 >> 2) & 1, (i3 >> 1) & 1, i3 & 1]
    c = crc12(bits)
    bits += [(c >> (11 - k)) & 1 for k in range(12)]
    cw = encode_bits(bits)
    par, msg = cw[:87], cw[87:]
    t = []
    for arr, blk in zip(costas, (par, msg, None)):
        t += list(arr)
        if blk is not None:
            t += [int(blk[3 * k] * 4 + blk[3 * k + 1] * 2 + blk[3 * k + 2]) for k in range(29)]
    return t


# ---------------------------------------------------------------------------------------- messages
_JSC = None


def jsc_map():
    global _JSC
    if _JSC is None:
        raw = lzma.decompress((Path(__file__).resolve().parents[1] / "data" / "jsc_map.txt.xz").read_bytes())
        _JSC = [w.replace("\\n", "\n").replace("\\\\", "\\") for w in raw.decode("latin-1").split("\n")]
    return _JSC


def jsc_decompress(bits):
    s, c = 7, 9
    base = [0, s]
    for k in range(2, 8):
        base.append(base[-1] + s * c ** (k - 1))
    words = jsc_map()
    nybbles, seps = [], []
    i = 0
    while i + 4 <= len(bits):
        v = int("".join(map(str, bits[i:i + 4])), 2)
        nybbles.append(v)
        i += 4
        if v < s:
            if i < len(bits) and bits[i]:
                seps.append(len(nybbles) - 1)
            i += 1
    out, start = [], 0
    while start < len(nybbles):
        k = j = 0
        while start + k < len(nybbles) and nybbles[start + k] >= s:
            j = j * c + (nybbles[start + k] - s)
            k += 1
        if j >= len(words) or start + k >= len(nybbles):
            break
        j = j * s + nybbles[start + k] + base[k]
        if j >= len(words):
            break
        out.append(words[j])
        if seps and seps[0] == start + k:
            out.append(" ")
            seps.pop(0)
        start += k + 1
    return "".join(out)


def jsc_compress(text):
    """Compression JSC (mots entiers du dictionnaire seulement : suffisant pour les mires)."""
    words = jsc_map()
    index = {}
    for i, w in enumerate(words):
        index.setdefault(w, i)
    s, c = 7, 9
    bits = []
    parts = text.split(" ")
    for n, w in enumerate(parts):
        i = index[w]
        sep = n < len(parts) - 1
        code = [((i % s) << 1) | sep]
        x = i // s
        nyb = []
        while x > 0:
            x -= 1
            nyb.insert(0, (x % c) + s)
            x //= c
        for v in nyb:
            bits += [(v >> (3 - k)) & 1 for k in range(4)]
        bits += [(code[0] >> (4 - k)) & 1 for k in range(5)]
    return bits


def huff_decode(bits):
    out, cur = [], ""
    for b in bits:
        cur += str(b)
        if cur in _HUFF_DEC:
            out.append(_HUFF_DEC[cur])
            cur = ""
    return "".join(out)


def _unpad(bits):
    n = len(bits) - 1 - bits[::-1].index(0) if 0 in bits else len(bits)
    return bits[:n]


def unpack_callsign(v, portable=False):
    if v in BASECALLS:
        return BASECALLS[v]
    w = [""] * 6
    for pos, mod, off in ((5, 27, 10), (4, 27, 10), (3, 27, 10), (2, 10, 0), (1, 36, 0)):
        w[pos] = ALNUM[v % mod + off]
        v //= mod
    w[0] = ALNUM[v] if v < len(ALNUM) else "?"
    call = "".join(w)
    if call.startswith("3D0"):
        call = "3DA0" + call[3:]
    if call.startswith("Q") and "A" <= call[1:2] <= "Z":
        call = "3X" + call[1:]
    call = call.strip()
    return call + "/P" if portable else call


def unpack_grid(v):
    if v > NBASEGRID:
        return ""
    dlat = v % 180 - 90
    dlong = v // 180 * 2 - 180 + 2
    nlong = int(60.0 * (180.0 - dlong) / 5)
    n1, n2 = nlong // 240, (nlong - 240 * (nlong // 240)) // 24
    nlat = int(60.0 * (dlat + 90) / 2.5)
    m1, m2 = nlat // 240, (nlat - 240 * (nlat // 240)) // 24
    return chr(ord("A") + n1) + chr(ord("A") + m1) + chr(ord("0") + n2) + chr(ord("0") + m2)


def unpack_alnum50(p):
    w = [""] * 11
    for pos, mod in ((10, 38), (9, 38), (8, 38), (7, 2), (6, 38), (5, 38), (4, 38), (3, 2), (2, 38), (1, 38), (0, 39)):
        t = p % mod
        w[pos] = ("/" if t else " ") if mod == 2 else ALNUM[t]
        p //= mod
    return "".join(w).replace(" ", "")


def fmt_snr(snr):
    if snr < -60 or snr > 60:
        return ""
    return f"{'+' if snr >= 0 else '-'}{abs(snr):02d}"


def frame_text(bits72, i3):
    """72 bits + type -> (texte, nature) comme JS8Call."""
    b = [int(x) for x in bits72]
    if i3 & DATA:
        return jsc_decompress(_unpad(b)), "data"
    if b[0] == 1:                                      # données (ancien format) : Huffman ou JSC
        rest = b[1:]
        comp = rest[0]
        n = len(rest) - 1 - rest[::-1].index(0) if 0 in rest else len(rest)
        body = rest[1:n]
        return (jsc_decompress(body) if comp else huff_decode(body)), "data"
    flag = b[0] * 4 + b[1] * 2 + b[2]
    v64 = int("".join(map(str, b[:64])), 2)
    extra8 = int("".join(map(str, b[64:72])), 2)
    if flag == 3:                                      # message dirigé
        frm = unpack_callsign(int("".join(map(str, b[3:31])), 2), bool(extra8 >> 7 & 1))
        to = unpack_callsign(int("".join(map(str, b[31:59])), 2), bool(extra8 >> 6 & 1))
        cmdn = int("".join(map(str, b[59:64])), 2) % 32
        cmd = CMDS.get(cmdn, "")
        ex = extra8 % 64
        num = ""
        if ex:
            num = " " + (fmt_snr(ex - 31) if cmdn in SNR_CMDS else str(ex - 31))
        return f"{frm}: {to}{cmd}{num} ", "directed"
    call = unpack_alnum50((v64 >> 11) & ((1 << 50) - 1))
    p5, p3 = extra8 >> 3, extra8 & 7
    num = ((v64 & 0x7FF) << 5) | p5
    if flag == 0:                                      # balise (heartbeat) ou CQ
        grid = unpack_grid(num & 0x7FFF)
        if num & 0x8000:
            return f"{call}: @ALLCALL {CQS.get(p3, 'CQ')} {grid} ", "cq"
        return f"{call}: @HB HEARTBEAT {grid} ", "hb"
    if flag in (1, 2):                                 # indicatif composé
        extra = ""
        if num <= NBASEGRID:
            extra = " " + unpack_grid(num)
        elif NUSERGRID <= num < NMAXGRID:
            v = num - NUSERGRID
            if v & 0x80:
                cmdn = 29 if v & 0x40 else 25
                extra = CMDS[cmdn] + " " + fmt_snr((v & 0x3F) - 31)
            else:
                extra = CMDS.get(v & 0x7F, "")
        return (f"{call}: " if flag == 1 else f"{call}{extra} "), "compound"
    return "", "?"


# ---------------------------------------------------------------------------------------- démodulation
def decode_window(x, fs, sub, t_nominal=0.5, dt_range=(-1.0, 2.5), fmin=200.0, fmax=3000.0, max_cand=40,
                  sync_min=2.0):
    """Fenêtre audio (début = début du créneau) -> liste de dict (freq, dt, snr, i3, bits72)."""
    _, nsps0, _, _, cst = SUBMODES[sub]
    costas = COSTAS[cst]
    x = np.asarray(x, float)
    if fs != 12000:
        x = resample_poly(x, 12000, int(fs))
    nsps = nsps0
    baud = 12000.0 / nsps
    hop = nsps // 4
    nfft = 2 * nsps
    nfr = (len(x) - nsps) // hop
    if nfr < 79 * 4:
        return []
    win = np.hanning(nsps)
    idx = np.arange(nsps)[None, :] + hop * np.arange(nfr)[:, None]
    S = np.abs(np.fft.rfft(x[idx] * win, nfft)) ** 2                     # trames de 1/4 de symbole
    df = 12000.0 / nfft
    b0, b1 = int(fmin / df), int(fmax / df) - 16
    t_lo = max(0, int((t_nominal + dt_range[0]) * 12000 / hop))
    t_hi = min(nfr - 79 * 4, int((t_nominal + dt_range[1]) * 12000 / hop))
    if t_hi <= t_lo:
        return []
    ts = np.arange(t_lo, t_hi)
    bins = np.arange(b0, b1)
    sig = np.zeros((len(ts), len(bins)))
    tot = np.zeros_like(sig)
    for c, arr in enumerate(costas):
        for j, tone in enumerate(arr):
            rows = S[ts + 4 * (36 * c + j)]
            sig += rows[:, bins + 2 * tone]
            tot += sum(rows[:, bins + 2 * k] for k in range(8))
    sync = sig / ((tot - sig) / 7 + 1e-12)
    cands = []
    flat = np.argsort(sync, axis=None)[::-1]
    for f in flat[:max_cand * 20]:
        ti, bi = divmod(int(f), len(bins))
        if sync[ti, bi] < sync_min:
            break
        t0, fb = ts[ti], bins[bi]
        if any(abs(t0 - a) < 4 and abs(fb - b) < 4 for a, b, _ in cands):
            continue
        cands.append((t0, fb, sync[ti, bi]))
        if len(cands) >= max_cand:
            break
    X = np.fft.rfft(x)
    N = len(x)
    out, seen = [], set()
    for t0, fb, sc in cands:
        r = _refine(X, N, t0 * hop, fb * df, nsps, costas)
        if r is None:
            continue
        f0, start, llrs, snr = r
        for llr in llrs:
            cw = bp_decode(llr)
            if cw is None or not cw.any():
                continue
            msg = cw[87:]
            if crc12(list(msg[:75])) != int("".join(map(str, msg[75:87])), 2):
                continue
            key = tuple(msg[:75])
            if key in seen:
                break
            seen.add(key)
            i3 = int(msg[72]) * 4 + int(msg[73]) * 2 + int(msg[74])
            out.append({"freq": f0, "dt": start / 12000.0 - t_nominal, "snr": snr, "i3": i3,
                        "bits72": msg[:72].copy(), "sync": float(sc)})
            break
    return out


def _refine(X, N, s0, f0, nsps, costas):
    """Bande de base autour du signal (32 échantillons par symbole), recalage fin, puis LLR."""
    baud = 12000.0 / nsps
    nds = 32
    fsd = nds * baud
    df = 12000.0 / N
    lo = int(round((f0 - 1.5 * baud) / df))
    nb = int(round(fsd / df))
    if lo < 0 or lo + nb > len(X):
        return None
    seg = X[lo:lo + nb].copy()
    n = len(seg)
    w = np.ones(n)
    taper = max(1, n // 20)
    w[:taper] = np.hanning(2 * taper)[:taper]
    w[-taper:] = np.hanning(2 * taper)[taper:]
    z = np.fft.ifft(seg * w) * (n / N)
    flo = lo * df                                        # z(t) : fréquence flo -> 0
    dec = N / n                                          # échantillons d'origine par échantillon de z
    best = None
    tones = np.arange(8)
    for dfr in np.linspace(-0.25, 0.25, 5) * baud:
        f_rel = f0 + dfr - flo                           # tonalité 0 dans z
        base_t = s0 / dec
        for dt in np.arange(-nds // 2, nds // 2 + 1, 2):
            st = int(round(base_t + dt))
            if st < 0 or st + 79 * nds > len(z):
                continue
            seg_z = z[st:st + 79 * nds].reshape(79, nds)
            tt = np.arange(nds) / fsd
            E = np.exp(-2j * np.pi * (f_rel + tones[:, None] * baud) * tt[None, :])
            A = np.abs(seg_z @ E.T)                       # (79, 8)
            sc = sum(A[36 * c + j, arr[j]] for c, arr in enumerate(costas) for j in range(7))
            if best is None or sc > best[0]:
                best = (sc, dfr, st, A)
    if best is None:
        return None
    _, dfr, st, A = best
    data = np.concatenate([A[7:36], A[43:72]])
    ps = data
    llr0 = np.empty(174)
    llr1 = np.empty(174)
    lg = np.log(ps + 1e-32)
    for k, (one, zero) in enumerate((([4, 5, 6, 7], [0, 1, 2, 3]), ([2, 3, 6, 7], [0, 1, 4, 5]),
                                     ([1, 3, 5, 7], [0, 2, 4, 6]))):
        llr0[k::3] = ps[:, one].max(1) - ps[:, zero].max(1)
        llr1[k::3] = lg[:, one].max(1) - lg[:, zero].max(1)
    out = []
    for l in (llr0, llr1):
        sd = np.sqrt(np.mean(l ** 2) - np.mean(l) ** 2) or 1.0
        out.append(l / sd * 2.83)
    l3 = out[0].copy()
    l3[:24] = 0
    out.append(l3)
    sigp = np.mean(np.max(A, 1) ** 2)
    noise = np.median(A ** 2)
    snr = 10 * np.log10(max(sigp / max(noise, 1e-12) - 1, 1e-3)) - 10 * np.log10(2500 / baud)
    return f0 + dfr, st * dec, out, round(float(snr))


# ---------------------------------------------------------------------------------------- décodeur
class JS8(Decoder):
    """Créneaux UTC (15 s en mode normal) : tout le passe-bande est décodé à chaque fin de créneau."""
    name = "JS8"
    kind = "msg"

    def __init__(self, fs, submode="A", latency=0.6):
        super().__init__(fs, 1500.0)
        self.sub = submode
        self.label, self.nsps, self.period, self.delay, _ = SUBMODES[submode]
        self.latency = latency
        self.buf = []
        self.slot = None
        self.lock = threading.Lock()
        self.results = []
        self.last_count = 0
        self.partial = {}                 # fréquence arrondie -> texte en cours (trames d'un même message)

    def status(self):
        return {"af": 1500.0, "slot": self.period, "last": self.last_count, "info": f"JS8 {self.label}"}

    def process(self, x):
        now = time.time()
        slot = int((now - self.latency) // self.period)
        if self.slot is None:
            self.slot = slot
        if slot != self.slot:
            audio = np.concatenate(self.buf) if self.buf else np.zeros(0)
            start = self.slot * self.period
            self.buf, self.slot = [], slot
            if len(audio) > self.fs * self.period * 0.8:
                threading.Thread(target=self._decode, args=(audio, start), daemon=True).start()
        self.buf.append(np.asarray(x, np.float64))
        with self.lock:
            res, self.results = self.results, []
        return res

    def _decode(self, audio, start):
        try:
            found = decode_window(audio, self.fs, self.sub, t_nominal=self.delay)
        except Exception as e:                          # pragma: no cover
            log.warning("JS8 : %s", e)
            found = []
        utc = time.strftime("%H%M%S", time.gmtime(start))
        msgs = [m for d in sorted(found, key=lambda d: d["freq"]) for m in self.messages(d, utc)]
        self.last_count = len(msgs)
        with self.lock:
            self.results.extend(msgs)

    def messages(self, d, utc):
        txt, kind = frame_text(d["bits72"], d["i3"])
        if not txt:
            return []
        key = int(round(d["freq"] / 10.0))
        i3 = d["i3"]
        if i3 & FIRST and not (i3 & LAST):
            self.partial[key] = txt
        elif key in self.partial and not (i3 & FIRST):
            self.partial[key] += txt
        end = " ␄" if i3 & LAST else ""
        return [{"t": "msg", "mode": f"JS8{self.sub}", "utc": utc, "snr": d["snr"], "dt": round(d["dt"], 1),
                 "freq": int(round(d["freq"])), "text": txt.rstrip() + end}]


def js8_audio(frames, sub="A", fs=12000, amp=0.5, delay=None):
    """Mire de test : [(fréquence, 12 caractères, type)] -> audio d'un créneau (FSK à phase continue)."""
    _, nsps, period, d0, cst = SUBMODES[sub]
    delay = d0 if delay is None else delay
    n = int(period * fs) + nsps
    x = np.zeros(n)
    baud = 12000.0 / nsps
    for f0, msg, i3 in frames:
        t = tones_for(msg, i3, COSTAS[cst])
        fr = np.repeat(f0 + np.array(t) * baud, nsps)
        ph = 2 * np.pi * np.cumsum(fr) / fs
        s0 = int(delay * fs)
        x[s0:s0 + len(ph)] += amp / len(frames) * np.cos(ph)
    return x


def data_frame(text):
    """Texte (mots du dictionnaire) -> 12 caractères d'une trame de données rapide (type DATA)."""
    bits = jsc_compress(text)
    if len(bits) >= 72:
        raise ValueError("texte trop long pour une trame")
    bits = bits + [0] + [1] * (71 - len(bits))
    return "".join(ALPHABET[int("".join(map(str, bits[6 * i:6 * i + 6])), 2)] for i in range(12))
