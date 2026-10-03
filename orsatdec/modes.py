"""Catalogue des modes d'Orsat-Decoder : décodeur, paramètres, démodulation et position audio.

« af » est l'endroit où le signal tombe dans l'audio démodulé : un clic sur le waterfall à la
fréquence F accorde le serveur sur F - af en BLU haute, de sorte que le signal arrive à af Hz.
"""
from .decoders.psk import PSK
from .decoders.fsk import RTTY
from .decoders.cw import CW
from .decoders.sitor import SitorB
from .decoders.ft8 import FT8

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
    return mode.get("bw", 200)
