"""Mires des sous-modes que sait décoder Orsat-Decoder, pour compléter les empreintes de la base Artemis.

Chaque page sigidwiki n'a qu'un enregistrement (PSK31 pour « PSK », Olivia 16/500 pour « Olivia »…) ;
on ajoute ici l'empreinte des autres variantes courantes, mesurée sur nos propres générateurs.
SYNTH : pageid Artemis -> [(étiquette, mode Orsat, paramètres, fabrique (texte, fs, af) -> audio)].
"""
from .encoders import psk_encode, rtty_encode, cw_encode, sitorb_encode, navtex_message
from .mfsk import mfsk_encode, dominoex_encode, thor_encode
from .olivia import olivia_encode, contestia_encode
from .mt63 import mt63_encode
from .images import hell_encode

TEXT = "CQ CQ DE F1NSK F1NSK THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789 " * 3


def _psk(b):
    return lambda t, fs, af: psk_encode(t, fs=fs, af=af, baud=b)


def _rtty(b, s):
    return lambda t, fs, af: rtty_encode(t, fs=fs, af=af, baud=b, shift=s)


def _cw(w):
    return lambda t, fs, af: cw_encode(t, fs=fs, af=af, wpm=w)


def _mfsk(m):
    return lambda t, fs, af: mfsk_encode(t, fs=fs, af=af, mode=m)


def _domino(m):
    return lambda t, fs, af: dominoex_encode(t, fs=fs, af=af, mode=m)


def _thor(m):
    return lambda t, fs, af: thor_encode(t, fs=fs, af=af, mode=m)


def _olivia(n, b):
    return lambda t, fs, af: olivia_encode(t, fs=fs, af=af, tones=n, bw=b)


def _contestia(n, b):
    return lambda t, fs, af: contestia_encode(t, fs=fs, af=af, tones=n, bw=b)


def _mt63(b):
    return lambda t, fs, af: mt63_encode(t, fs=fs, af=af, bw=b)


SYNTH = {
    140: [("PSK31", "psk31", {}, _psk(31.25)), ("PSK63", "psk63", {}, _psk(62.5)), ("PSK125", "psk125", {}, _psk(125.0))],
    197: [(f"RTTY {b:g} bd / {s:g} Hz", "rtty", {"baud": b, "shift": s}, _rtty(b, s))
          for b, s in ((45.45, 170.0), (50.0, 450.0), (50.0, 425.0), (75.0, 170.0), (50.0, 850.0), (100.0, 850.0))],
    567: [(f"CW {w} mpm", "cw", {}, _cw(w)) for w in (12, 20, 30)],
    620: [("Navtex / SITOR-B", "navtex", {}, lambda t, fs, af: sitorb_encode(navtex_message(t[:120]), fs=fs, af=af))],
    200: [(m, "mfsk-" + m[4:], {}, _mfsk(m)) for m in ("MFSK8", "MFSK16", "MFSK32", "MFSK64")],
    832: [(m, "dominoex-" + m.split()[1], {}, _domino(m)) for m in ("DominoEX 8", "DominoEX 11", "DominoEX 16", "DominoEX 22")],
    1421: [(m, "thor-" + m.split()[1], {}, _thor(m)) for m in ("THOR 8", "THOR 11", "THOR 16", "THOR 22")],
    504: [(f"Olivia {n}/{b}", "olivia", {"tones": n, "bw": b}, _olivia(n, b))
          for n, b in ((8, 250), (8, 500), (16, 500), (32, 1000), (16, 1000), (64, 2000))],
    1236: [(f"Contestia {n}/{b}", "contestia", {"tones": n, "bw": b}, _contestia(n, b))
           for n, b in ((4, 250), (8, 250), (8, 500), (16, 500), (32, 1000))],
    1370: [(f"MT63-{b}", f"mt63_{b}", {}, _mt63(b)) for b in (500, 1000, 2000)],
    1309: [("Feld Hell", "hell", {"mode": "feld"}, lambda t, fs, af: hell_encode(t[:60], mode="feld", fs=fs, af=af))],
}
