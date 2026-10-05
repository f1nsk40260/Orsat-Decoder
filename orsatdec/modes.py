"""Catalogue des modes d'Orsat-Decoder : décodeur, paramètres, démodulation et position audio.

« af » est l'endroit où le signal tombe dans l'audio démodulé : un clic sur le waterfall à la
fréquence F accorde le serveur sur F - af en BLU haute, de sorte que le signal arrive à af Hz.
"""
from .decoders.psk import PSK
from .decoders.fsk import RTTY
from .decoders.cw import CW
from .decoders.sitor import SitorB
from .decoders.ft8 import FT8
from .decoders.mfsk import MFSK
from .decoders.dominoex import DominoEX
from .decoders.thor import THOR
from .decoders.ifk_common import MFSK_MODES, DOMINO_MODES, THOR_MODES
from .decoders.olivia import Olivia, Contestia
from .decoders.mt63 import MT63
from .decoders.wefax import WeFax
from .decoders.sstv import SSTV
from .decoders.hell import Hell

DIR = {"key": "reverse", "label": "Sens", "opts": [[False, "Normal"], [True, "Inversé"]], "def": False}

MODES = [
    {"id": "psk31", "label": "PSK31", "family": "PSK", "desc": "Le mode clavier le plus utilisé.", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=31.25), "bw": 60},
    {"id": "psk63", "label": "PSK63", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=62.5), "bw": 100},
    {"id": "psk125", "label": "PSK125", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=125.0), "bw": 180},
    {"id": "rtty", "label": "RTTY", "family": "RTTY et FSK", "desc": "Amateur (45 bd, 170 Hz) et météo DWD (50 bd, 425 Hz).",
     "af": 1000, "bw_from": "shift",
     "params": [{"key": "baud", "label": "Vitesse", "opts": [[45.45, "45 bd"], [50.0, "50 bd"], [75.0, "75 bd"], [100.0, "100 bd"]], "def": 45.45},
                {"key": "shift", "label": "Shift", "opts": [[170.0, "170 Hz"], [425.0, "425 Hz"], [450.0, "450 Hz"], [850.0, "850 Hz"]], "def": 170.0},
                DIR],
     "make": lambda fs, af, p: RTTY(fs, af, baud=p.get("baud", 45.45), shift=p.get("shift", 170.0), reverse=p.get("reverse", False))},
    {"id": "navtex", "label": "Navtex / SITOR-B", "family": "Maritime", "desc": "Avis aux navigateurs, 518 et 490 kHz.",
     "af": 1000, "bw": 340, "params": [DIR],
     "make": lambda fs, af, p: SitorB(fs, af, reverse=p.get("reverse", False))},
    {"id": "cw", "label": "CW", "family": "CW", "desc": "Morse, vitesse automatique.", "af": 700,
     "params": [{"key": "bw", "label": "Filtre", "opts": [[50.0, "Étroit"], [80.0, "Normal"], [150.0, "Large"]], "def": 80.0}],
     "make": lambda fs, af, p: CW(fs, af, bw=p.get("bw", 80.0)), "bw": 80},
    {"id": "ft8", "label": "FT8", "family": "Signaux faibles", "desc": "Créneaux de 15 s, toute la bande audio.",
     "af": 0, "whole": True, "kind": "msg", "make": lambda fs, af, p: FT8(fs, 1500)},
    {"id": "ft4", "label": "FT4", "family": "Signaux faibles", "desc": "Créneaux de 7,5 s.",
     "af": 0, "whole": True, "kind": "msg", "make": lambda fs, af, p: FT8(fs, 1500, ft4=True)},
]



def _bw_mfsk(m):
    sr, sl, _, _, n, _ = MFSK_MODES[m]
    return round(n * sr / sl)


def _bw_ifk(sr, sl, ds):
    return round(18 * ds * sr / sl)


MODES += [
    {"id": "mfsk-" + m[4:].lower(), "label": m, "family": "MFSK",
     "desc": "MFSK de fldigi : FEC et entrelacement, très robuste." if m == "MFSK16" else "",
     "af": 1500, "bw": _bw_mfsk(m), "params": [DIR],
     "make": (lambda m: lambda fs, af, p: MFSK(fs, af, mode=m, reverse=p.get("reverse", False)))(m)}
    for m in ("MFSK4", "MFSK8", "MFSK11", "MFSK16", "MFSK22", "MFSK31", "MFSK32", "MFSK64", "MFSK128")
] + [
    {"id": "dominoex-" + m.split()[1].lower(), "label": m, "family": "MFSK",
     "desc": "IFK+ 18 tonalités, insensible à la dérive." if m == "DominoEX 11" else "",
     "af": 1500, "bw": _bw_ifk(*DOMINO_MODES[m]),
     "params": [{"key": "fec", "label": "FEC", "opts": [[False, "Sans"], [True, "Avec (MultiPSK)"]], "def": False}, DIR],
     "make": (lambda m: lambda fs, af, p: DominoEX(fs, af, mode=m, fec=p.get("fec", False),
                                                   reverse=p.get("reverse", False)))(m)}
    for m in DOMINO_MODES
] + [
    {"id": "thor-" + m.split()[1].lower(), "label": m, "family": "MFSK",
     "desc": "IFK+ avec FEC et entrelacement." if m == "THOR 16" else "",
     "af": 1500, "bw": _bw_ifk(*THOR_MODES[m][:3]), "params": [DIR],
     "make": (lambda m: lambda fs, af, p: THOR(fs, af, mode=m, reverse=p.get("reverse", False)))(m)}
    for m in THOR_MODES
]

OLIVIA_TONES = {"key": "tones", "label": "Tonalités", "opts": [[4, "4"], [8, "8"], [16, "16"], [32, "32"], [64, "64"]], "def": 32}
OLIVIA_BW = {"key": "bw", "label": "Largeur", "opts": [[125, "125 Hz"], [250, "250 Hz"], [500, "500 Hz"], [1000, "1000 Hz"], [2000, "2000 Hz"]], "def": 1000}
IL = {"key": "interleave", "label": "Entrelacement", "opts": [["long", "Long"], ["court", "Court"]], "def": "long"}

MODES += [
    {"id": "olivia", "label": "Olivia", "family": "Olivia",
     "desc": "MFSK + FEC Walsh, très robuste. Courants : 8/250, 8/500, 16/500, 32/1000.",
     "af": 1500, "bw_from": "bw", "bw": 1000, "params": [OLIVIA_TONES, OLIVIA_BW, DIR],
     "make": lambda fs, af, p: Olivia(fs, af, tones=p.get("tones", 32), bw=p.get("bw", 1000), reverse=p.get("reverse", False))},
    {"id": "contestia", "label": "Contestia", "family": "Olivia",
     "desc": "Variante rapide d'Olivia (caractères 6 bits, majuscules).",
     "af": 1500, "bw_from": "bw", "bw": 500,
     "params": [dict(OLIVIA_TONES, opts=OLIVIA_TONES["opts"][:4], **{"def": 8}),
                dict(OLIVIA_BW, opts=OLIVIA_BW["opts"][:4], **{"def": 500}), DIR],
     "make": lambda fs, af, p: Contestia(fs, af, tones=p.get("tones", 8), bw=p.get("bw", 500), reverse=p.get("reverse", False))},
    {"id": "mt63_500", "label": "MT63-500", "family": "MT63", "desc": "64 porteuses, 5 car./s, le plus robuste.",
     "af": 750, "bw": 500, "params": [IL], "make": lambda fs, af, p: MT63(fs, af, bw=500, interleave=p.get("interleave", "long"))},
    {"id": "mt63_1000", "label": "MT63-1000", "family": "MT63", "desc": "Le plus courant (urgences, ARES/SHARES).",
     "af": 1000, "bw": 1000, "params": [IL], "make": lambda fs, af, p: MT63(fs, af, bw=1000, interleave=p.get("interleave", "long"))},
    {"id": "mt63_2000", "label": "MT63-2000", "family": "MT63", "desc": "Rapide, 2 kHz de large.",
     "af": 1500, "bw": 2000, "params": [IL], "make": lambda fs, af, p: MT63(fs, af, bw=2000, interleave=p.get("interleave", "long"))},
    {"id": "wefax", "label": "Fax météo", "family": "Images", "kind": "img", "desc": "WEFAX HF, démarrage et phasage automatiques.",
     "af": 1900, "bw": 1000,
     "params": [{"key": "ioc", "label": "IOC", "opts": [[576, "576"], [288, "288"]], "def": 576},
                {"key": "lpm", "label": "Lignes/min", "opts": [[60, "60"], [90, "90"], [120, "120"], [240, "240"]], "def": 120}],
     "make": lambda fs, af, p: WeFax(fs, af, ioc=p.get("ioc", 576), lpm=p.get("lpm", 120))},
    {"id": "sstv", "label": "SSTV", "family": "Images", "kind": "img", "desc": "Martin, Scottie, Robot, PD, Wraase ; code VIS automatique.",
     "af": 1900, "bw": 1300, "make": lambda fs, af, p: SSTV(fs, af)},
    {"id": "hell", "label": "Hellschreiber", "family": "Images", "kind": "img", "desc": "Feld Hell et variantes, lecture à l'œil.",
     "af": 1000, "bw": 300,
     "params": [{"key": "mode", "label": "Variante", "opts": [["feld", "Feld Hell"], ["slow", "Slow Hell"], ["x5", "Hell X5"], ["x9", "Hell X9"],
                 ["fsk245", "FSK Hell 245"], ["fsk105", "FSK Hell 105"], ["hell80", "Hell 80"]], "def": "feld"}, DIR],
     "make": lambda fs, af, p: Hell(fs, af, mode=p.get("mode", "feld"), reverse=p.get("reverse", False))},
]

BY_ID = {m["id"]: m for m in MODES}

# Fréquences connues : un clic crée directement le canal (fréquence du signal, ou cadran pour FT8/FT4)
PRESETS = [
    {"label": "Navtex international", "mode": "navtex", "freq": 518000},
    {"label": "Navtex national", "mode": "navtex", "freq": 490000},
    {"label": "Navtex 4209,5 kHz", "mode": "navtex", "freq": 4209500},
    {"label": "Météo DWD 4583 kHz", "mode": "rtty", "freq": 4583000, "params": {"baud": 50.0, "shift": 450.0, "reverse": True}},
    {"label": "Météo DWD 7646 kHz", "mode": "rtty", "freq": 7646000, "params": {"baud": 50.0, "shift": 450.0, "reverse": True}},
    {"label": "Météo DWD 10100,8 kHz", "mode": "rtty", "freq": 10100800, "params": {"baud": 50.0, "shift": 450.0, "reverse": True}},
    {"label": "FT8 160 m", "mode": "ft8", "freq": 1840000},
    {"label": "FT8 80 m", "mode": "ft8", "freq": 3573000},
    {"label": "FT8 40 m", "mode": "ft8", "freq": 7074000},
    {"label": "FT8 30 m", "mode": "ft8", "freq": 10136000},
    {"label": "FT8 20 m", "mode": "ft8", "freq": 14074000},
    {"label": "FT8 17 m", "mode": "ft8", "freq": 18100000},
    {"label": "FT8 15 m", "mode": "ft8", "freq": 21074000},
    {"label": "FT8 10 m", "mode": "ft8", "freq": 28074000},
    {"label": "FT4 20 m", "mode": "ft4", "freq": 14080000},
    {"label": "PSK31 40 m", "mode": "psk31", "freq": 7071000},
    {"label": "PSK31 20 m", "mode": "psk31", "freq": 14071000},
    {"label": "Fax DWD 3855 kHz", "mode": "wefax", "freq": 3855000},
    {"label": "Fax DWD 7880 kHz", "mode": "wefax", "freq": 7880000},
    {"label": "Fax DWD 13882,5 kHz", "mode": "wefax", "freq": 13882500},
    {"label": "Fax Northwood 8040 kHz", "mode": "wefax", "freq": 8040000},
    {"label": "SSTV 40 m", "mode": "sstv", "freq": 7165000},
    {"label": "SSTV 20 m", "mode": "sstv", "freq": 14230000},
    {"label": "Olivia 8/250 40 m", "mode": "olivia", "freq": 7072500, "params": {"tones": 8, "bw": 250}},
    {"label": "Olivia 8/250 20 m", "mode": "olivia", "freq": 14072500, "params": {"tones": 8, "bw": 250}},
    {"label": "Olivia 32/1000 20 m", "mode": "olivia", "freq": 14075400, "params": {"tones": 32, "bw": 1000}},
]


def public_catalog():
    """Ce que l'interface a besoin de savoir (sans les fabriques Python)."""
    out = []
    for m in MODES:
        out.append({k: v for k, v in m.items() if k != "make"})
    return {"modes": out, "presets": PRESETS}


def default_params(mode):
    return {p["key"]: p["def"] for p in mode.get("params", [])}


def bandwidth(mode, params):
    if mode.get("whole"):
        return 3000
    if mode.get("bw_from") == "shift":
        return float(params.get("shift", 170.0)) + 2 * float(params.get("baud", 45.45))
    if mode.get("bw_from") == "bw":
        return float(params.get("bw", mode.get("bw", 200)))
    return mode.get("bw", 200)
