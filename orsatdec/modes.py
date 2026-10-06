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
from .decoders.sitora import SitorA
from .decoders.packet import AX25
from .decoders.tones import DTMF, Selcall, ICAOSelcal, SELCALL
from .decoders.dsc import DSC
from .decoders.pocsag import POCSAG
from .decoders.timecode import TimeCode, CHU
from .decoders.ale import ALE
from .decoders.throb import Throb
from .decoders.fsq import FSQ
from .decoders.wspr import WSPR
from .decoders.dgps import DGPS
from .decoders.pactor import PactorI
from .decoders.acars import ACARS
from .decoders.hfdl import HFDL
from .decoders.js8 import JS8
from .decoders.ident import Identifier, SPAN as IDENT_SPAN

DIR = {"key": "reverse", "label": "Sens", "opts": [[False, "Normal"], [True, "Inversé"]], "def": False}

MODES = [
    {"id": "ident", "label": "Identifier", "family": "Identification", "kind": "ident",
     "desc": "Cliquez sur un signal inconnu : Orsat-Decoder le reconnaît (base Artemis) et ouvre le bon mode.",
     "af": 1500, "bw": 2 * IDENT_SPAN,
     "params": [{"key": "seconds", "label": "Écoute", "opts": [[6.0, "6 s"], [10.0, "10 s"], [20.0, "20 s"], [30.0, "30 s"]], "def": 10.0},
                {"key": "auto", "label": "Mode trouvé", "opts": [[True, "Ouvrir"], [False, "Proposer"]], "def": True}],
     "make": lambda fs, af, p: Identifier(fs, af, seconds=p.get("seconds", 10.0))},
    {"id": "psk31", "label": "PSK31", "family": "PSK", "desc": "Le mode clavier le plus utilisé.", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=31.25), "bw": 60},
    {"id": "psk63", "label": "PSK63", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=62.5), "bw": 100},
    {"id": "psk125", "label": "PSK125", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=125.0), "bw": 180},
    {"id": "psk250", "label": "PSK250", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=250.0), "bw": 350},
    {"id": "psk500", "label": "PSK500", "family": "PSK", "desc": "", "af": 1000,
     "make": lambda fs, af, p: PSK(fs, af, baud=500.0), "bw": 700},
    {"id": "psk1000", "label": "PSK1000", "family": "PSK", "desc": "", "af": 1500,
     "make": lambda fs, af, p: PSK(fs, af, baud=1000.0), "bw": 1400},
] + [
    {"id": f"qpsk{b}", "label": f"QPSK{b}", "family": "PSK", "desc": "QPSK avec FEC (K=5), plus robuste que le BPSK." if b == 31 else "",
     "af": 1000, "bw": int(b * 1.8) + 20, "make": (lambda b: lambda fs, af, p: PSK(fs, af, baud=float(b if b != 31 else 31.25)
                                                                                   if b != 63 else 62.5, kind="qpsk"))(b)}
    for b in (31, 63, 125, 250, 500)
] + [
    {"id": f"psk{b}r", "label": f"PSK{b}R", "family": "PSK", "desc": "PSK robuste de fldigi (FEC K=7, entrelacement), PSKmail." if b == 125 else "",
     "af": 1000 if b < 1000 else 1500, "bw": int(b * 1.4) + 20,
     "make": (lambda b, dp: lambda fs, af, p: PSK(fs, af, baud=float(b), kind="pskr", depth=dp))(b, dp)}
    for b, dp in ((125, 40), (250, 80), (500, 160), (1000, 160))
] + [
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
    {"id": "wspr", "label": "WSPR", "family": "Signaux faibles", "desc": "Balises de propagation, créneaux de 2 min (minutes paires).",
     "af": 0, "whole": True, "kind": "msg", "make": lambda fs, af, p: WSPR(fs)},
    {"id": "js8", "label": "JS8 normal", "family": "Signaux faibles", "desc": "JS8Call : créneaux de 15 s, toute la bande audio (balises, CQ, messages dirigés, texte libre).",
     "af": 0, "whole": True, "kind": "msg", "crc": True, "make": lambda fs, af, p: JS8(fs, "A")},
    {"id": "js8b", "label": "JS8 rapide", "family": "Signaux faibles", "desc": "JS8Call rapide : créneaux de 10 s.",
     "af": 0, "whole": True, "kind": "msg", "crc": True, "make": lambda fs, af, p: JS8(fs, "B")},
    {"id": "js8c", "label": "JS8 turbo", "family": "Signaux faibles", "desc": "JS8Call turbo : créneaux de 6 s.",
     "af": 0, "whole": True, "kind": "msg", "crc": True, "make": lambda fs, af, p: JS8(fs, "C")},
    {"id": "js8e", "label": "JS8 lent", "family": "Signaux faibles", "desc": "JS8Call lent : créneaux de 30 s.",
     "af": 0, "whole": True, "kind": "msg", "crc": True, "make": lambda fs, af, p: JS8(fs, "E")},
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

# Jalon 3 : modes utilitaires (réception seule)
MODES += [
    {"id": "ascii", "label": "ASCII FSK", "family": "RTTY et FSK", "desc": "Téléimprimeurs ASCII 7 ou 8 bits (agences, météo, modems).",
     "af": 1000, "bw_from": "shift",
     "params": [{"key": "baud", "label": "Vitesse", "opts": [[110.0, "110 bd"], [75.0, "75 bd"], [100.0, "100 bd"], [150.0, "150 bd"],
                                                            [200.0, "200 bd"], [300.0, "300 bd"], [50.0, "50 bd"]], "def": 110.0},
                {"key": "shift", "label": "Shift", "opts": [[170.0, "170 Hz"], [200.0, "200 Hz"], [425.0, "425 Hz"], [450.0, "450 Hz"],
                                                           [850.0, "850 Hz"]], "def": 170.0},
                {"key": "bits", "label": "Bits", "opts": [[7, "7"], [8, "8"]], "def": 7}, DIR],
     "make": lambda fs, af, p: RTTY(fs, af, baud=p.get("baud", 110.0), shift=p.get("shift", 170.0), bits=p.get("bits", 7),
                                    reverse=p.get("reverse", False))},
    {"id": "sitora", "label": "SITOR-A / AMTOR", "family": "Maritime", "desc": "Mode ARQ (blocs de 3 caractères), en écoute ; répétitions retirées.",
     "af": 1000, "bw": 340, "params": [DIR],
     "make": lambda fs, af, p: SitorA(fs, af, reverse=p.get("reverse", False))},
    {"id": "packet300", "label": "Packet 300 (HF)", "family": "Packet", "desc": "AX.25 300 bauds, 200 Hz (APRS HF, BBS).",
     "af": 1700, "bw": 500, "kind": "msg", "crc": True,
     "make": lambda fs, af, p: AX25(fs, af, baud=300.0, shift=200.0)},
    {"id": "packet1200", "label": "Packet 1200 / APRS", "family": "Packet", "desc": "AX.25 1200 bauds AFSK en FM (APRS 144,800 MHz). Cliquez au centre du signal.",
     "af": 0, "whole": True, "demod": "FM", "kind": "msg", "crc": True,
     "make": lambda fs, af, p: AX25(fs, 1700.0, baud=1200.0, shift=1000.0)},
    {"id": "dsc", "label": "DSC / ASN HF-MF", "family": "Maritime", "desc": "Appel sélectif numérique (2187,5 kHz, 8414,5 kHz…) et selcall HF CCIR 493-4 (Codan, Barrett).",
     "af": 1700, "bw": 340, "kind": "msg", "crc": True,
     "make": lambda fs, af, p: DSC(fs, af, baud=100.0, shift=170.0)},
    {"id": "dsc_vhf", "label": "DSC / ASN VHF", "family": "Maritime", "desc": "Canal 70 (156,525 MHz), 1200 bauds en FM.",
     "af": 0, "whole": True, "demod": "FM", "kind": "msg", "crc": True,
     "make": lambda fs, af, p: DSC(fs, 1700.0, baud=1200.0, shift=800.0)},
    {"id": "pactor", "label": "PACTOR I (écoute)", "family": "TOR / ARQ", "desc": "Liaisons PACTOR I en ARQ ou FEC, 100 et 200 bauds, 200 Hz (Memory-ARQ, Huffman). Cliquez au centre des deux tonalités.",
     "af": 1500, "bw": 600, "kind": "text", "crc": True,
     "make": lambda fs, af, p: PactorI(fs, af)},
    {"id": "dgps", "label": "DGPS 200 bauds", "family": "Maritime", "desc": "Balises de correction GPS (RTCM SC-104, 283,5 à 325 kHz), MSK 200 bauds. Cliquez au centre du signal.",
     "af": 1000, "bw": 300, "kind": "msg", "crc": True,
     "make": lambda fs, af, p: DGPS(fs, af, baud=200.0)},
    {"id": "dgps100", "label": "DGPS 100 bauds", "family": "Maritime", "desc": "Balises de correction GPS (RTCM SC-104), MSK 100 bauds.",
     "af": 1000, "bw": 150, "kind": "msg", "crc": True,
     "make": lambda fs, af, p: DGPS(fs, af, baud=100.0)},
    {"id": "acars", "label": "ACARS", "family": "Aviation", "desc": "Messages des avions en VHF (131,725 / 131,525 MHz en Europe), AM, MSK 2400 bits/s. Cliquez au centre du canal.",
     "af": 0, "whole": True, "demod": "AM", "kind": "msg", "crc": True,
     "make": lambda fs, af, p: ACARS(fs)},
    {"id": "hfdl", "label": "HFDL", "family": "Aviation", "desc": "Liaison de données HF des avions (positions, ACARS, squitters des 16 stations au sol), PSK 1800 bauds. Cliquez au centre du signal (porteuse + 1440 Hz).",
     "af": 1440, "bw": 2400, "kind": "msg", "crc": True,
     "make": lambda fs, af, p: HFDL(fs, af)},
    {"id": "selcal", "label": "Selcal (aviation)", "family": "Sélectifs", "desc": "Appel sélectif OACI des avions en HF (AB-CD). Cliquez sur la porteuse.",
     "af": 0, "whole": True, "carrier": True, "kind": "msg",
     "make": lambda fs, af, p: ICAOSelcal(fs)},
    {"id": "selcall5", "label": "Appels à 5 tons", "family": "Sélectifs", "desc": "CCIR, ZVEI, EEA, EIA… (taxis, pompiers, PMR), en FM.",
     "af": 0, "whole": True, "demod": "FM", "kind": "msg",
     "params": [{"key": "std", "label": "Standard", "opts": [["auto", "Automatique"]] + [[k, k] for k in SELCALL], "def": "auto"}],
     "make": lambda fs, af, p: Selcall(fs, standard=p.get("std", "auto"))},
    {"id": "dtmf", "label": "DTMF", "family": "Sélectifs", "desc": "Fréquences vocales (touches de téléphone), en FM.",
     "af": 0, "whole": True, "demod": "FM", "kind": "msg",
     "make": lambda fs, af, p: DTMF(fs)},
    {"id": "pocsag", "label": "POCSAG (pagers)", "family": "Sélectifs", "desc": "Récepteurs d'appel 512, 1200 et 2400 bauds, en FM (VHF / UHF).",
     "af": 0, "whole": True, "demod": "FM", "kind": "msg", "crc": True,
     "make": lambda fs, af, p: POCSAG(fs, compensate=p.get("_src") == "phantom")},
]

MODES += [
    {"id": m.lower(), "label": m.replace("THROBX", "THROBX "), "family": "THROB",
     "desc": "Paires de tonalités (G3PPT), très lent et très robuste." if m == "THROB1" else "",
     "af": 1000, "bw": 72 if m.endswith("1") or m.endswith("2") else 140, "params": [DIR],
     "make": (lambda m: lambda fs, af, p: Throb(fs, af, mode=m, reverse=p.get("reverse", False)))(m)}
    for m in ("THROB1", "THROB2", "THROB4", "THROBX1", "THROBX2", "THROBX4")
]

MODES += [
    {"id": "fsq", "label": "FSQ", "family": "MFSK", "desc": "FSQ de ZL1BPU (NVIS, messages dirigés « indicatif: »), toutes vitesses.",
     "af": 1500, "bw": 300, "make": lambda fs, af, p: FSQ(fs, af)},
    {"id": "ifkp", "label": "IFKP", "family": "MFSK", "desc": "IFK+ de fldigi (33 tonalités), toutes vitesses.",
     "af": 1500, "bw": 390, "make": lambda fs, af, p: FSQ(fs, af, variant="ifkp")},
]

MODES += [
    {"id": "ale", "label": "ALE 2G", "family": "ALE et liaisons HF", "desc": "MIL-STD-188-141 : appels, adresses, messages AMD (8 tonalités, 2 kHz).",
     "af": 1625, "bw": 2000, "kind": "msg", "crc": True, "make": lambda fs, af, p: ALE(fs, af)},
]

# Signaux horaires : cliquez sur la porteuse (elle tombe à 1000 Hz dans l'audio)
_TIME = [("dcf77", "DCF77", "77,5 kHz, Allemagne (baisses de porteuse)."), ("msf", "MSF", "60 kHz, Royaume-Uni."),
         ("tdf", "TDF / ALS162", "162 kHz, Allouis (modulation de phase)."), ("wwvb", "WWVB", "60 kHz, États-Unis."),
         ("jjy", "JJY", "40 et 60 kHz, Japon."), ("wwv", "WWV / WWVH", "2,5 à 20 MHz, sous-porteuse 100 Hz.")]
MODES += [
    {"id": k, "label": lab, "family": "Signaux horaires", "desc": d + " Cliquez sur la porteuse ; une minute pour l'heure.",
     "af": 1000, "bw": 60, "kind": "msg", "make": (lambda k: lambda fs, af, p: TimeCode(fs, af, k))(k)}
    for k, lab, d in _TIME
] + [
    {"id": "chu", "label": "CHU", "family": "Signaux horaires", "desc": "3330, 7850, 14670 kHz, Canada (FSK 300 bauds). Cliquez sur la porteuse.",
     "af": 0, "whole": True, "carrier": True, "kind": "msg", "make": lambda fs, af, p: CHU(fs, 2125.0)},
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
    {"label": "JS8 80 m", "mode": "js8", "freq": 3578000},
    {"label": "JS8 40 m", "mode": "js8", "freq": 7078000},
    {"label": "JS8 30 m", "mode": "js8", "freq": 10130000},
    {"label": "JS8 20 m", "mode": "js8", "freq": 14078000},
    {"label": "JS8 15 m", "mode": "js8", "freq": 21078000},
    {"label": "JS8 10 m", "mode": "js8", "freq": 28078000},
    {"label": "FT8 80 m", "mode": "ft8", "freq": 3573000},
    {"label": "FT8 40 m", "mode": "ft8", "freq": 7074000},
    {"label": "FT8 30 m", "mode": "ft8", "freq": 10136000},
    {"label": "FT8 20 m", "mode": "ft8", "freq": 14074000},
    {"label": "FT8 17 m", "mode": "ft8", "freq": 18100000},
    {"label": "FT8 15 m", "mode": "ft8", "freq": 21074000},
    {"label": "FT8 10 m", "mode": "ft8", "freq": 28074000},
    {"label": "FT4 20 m", "mode": "ft4", "freq": 14080000},
    {"label": "WSPR 80 m", "mode": "wspr", "freq": 3568600},
    {"label": "WSPR 40 m", "mode": "wspr", "freq": 7038600},
    {"label": "WSPR 30 m", "mode": "wspr", "freq": 10138700},
    {"label": "WSPR 20 m", "mode": "wspr", "freq": 14095600},
    {"label": "WSPR 17 m", "mode": "wspr", "freq": 18104600},
    {"label": "WSPR 10 m", "mode": "wspr", "freq": 28124600},
    {"label": "PSK31 40 m", "mode": "psk31", "freq": 7071000},
    {"label": "PSK31 20 m", "mode": "psk31", "freq": 14071000},
    {"label": "Fax DWD 3855 kHz", "mode": "wefax", "freq": 3855000},
    {"label": "Fax DWD 7880 kHz", "mode": "wefax", "freq": 7880000},
    {"label": "Fax DWD 13882,5 kHz", "mode": "wefax", "freq": 13882500},
    {"label": "Fax Northwood 8040 kHz", "mode": "wefax", "freq": 8040000},
    {"label": "SSTV 40 m", "mode": "sstv", "freq": 7165000},
    {"label": "SSTV 20 m", "mode": "sstv", "freq": 14230000},
    {"label": "Olivia 8/250 40 m", "mode": "olivia", "freq": 7072500, "params": {"tones": 8, "bw": 250}},
    {"label": "APRS 144,800 MHz", "mode": "packet1200", "freq": 144800000},
    {"label": "HFDL Shannon 5547 kHz", "mode": "hfdl", "freq": 5548440},
    {"label": "HFDL Shannon 6532 kHz", "mode": "hfdl", "freq": 6533440},
    {"label": "HFDL Shannon 8942 kHz", "mode": "hfdl", "freq": 8943440},
    {"label": "HFDL Shannon 11384 kHz", "mode": "hfdl", "freq": 11385440},
    {"label": "HFDL Canaries 8948 kHz", "mode": "hfdl", "freq": 8949440},
    {"label": "HFDL Canaries 13303 kHz", "mode": "hfdl", "freq": 13304440},
    {"label": "HFDL Reykjavik 8977 kHz", "mode": "hfdl", "freq": 8978440},
    {"label": "ACARS Europe 131,725 MHz", "mode": "acars", "freq": 131725000},
    {"label": "ACARS Europe 131,525 MHz", "mode": "acars", "freq": 131525000},
    {"label": "ACARS États-Unis 131,550 MHz", "mode": "acars", "freq": 131550000},
    {"label": "DSC 2187,5 kHz", "mode": "dsc", "freq": 2187500},
    {"label": "DSC 4207,5 kHz", "mode": "dsc", "freq": 4207500},
    {"label": "DSC 6312 kHz", "mode": "dsc", "freq": 6312000},
    {"label": "DSC 8414,5 kHz", "mode": "dsc", "freq": 8414500},
    {"label": "DSC 12577 kHz", "mode": "dsc", "freq": 12577000},
    {"label": "DSC 16804,5 kHz", "mode": "dsc", "freq": 16804500},
    {"label": "DSC VHF canal 70", "mode": "dsc_vhf", "freq": 156525000},
    {"label": "Selcal Shanwick 8879 kHz", "mode": "selcal", "freq": 8879000},
    {"label": "DCF77 77,5 kHz", "mode": "dcf77", "freq": 77500},
    {"label": "MSF 60 kHz", "mode": "msf", "freq": 60000},
    {"label": "TDF 162 kHz", "mode": "tdf", "freq": 162000},
    {"label": "WWV 10 MHz", "mode": "wwv", "freq": 10000000},
    {"label": "WWV 15 MHz", "mode": "wwv", "freq": 15000000},
    {"label": "CHU 7850 kHz", "mode": "chu", "freq": 7850000},
    {"label": "CHU 3330 kHz", "mode": "chu", "freq": 3330000},
    {"label": "Selcal Shanwick 5598 kHz", "mode": "selcal", "freq": 5598000},
    {"label": "APRS HF 10147,6 kHz", "mode": "packet300", "freq": 10149300},
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
