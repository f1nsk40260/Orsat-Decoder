"""Bancs des décodeurs du jalon 3 (modes utilitaires) sur mires générées.

    python3 tests/test_utility.py          # tous les cas, à plusieurs niveaux de bruit
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import ascii_encode, add_noise  # noqa: E402
from orsatdec.gen import utility as U  # noqa: E402
from orsatdec.modes import BY_ID, default_params  # noqa: E402

FS = 12000


def make(mode, af, **p):
    m = BY_ID[mode]
    return m["make"](FS, af, {**default_params(m), **p})


def cases():
    """(libellé, signal, fabrique du décodeur, texte attendu, S/B de l'autotest)"""
    out = []
    t = "CQ CQ de F1NSK : ascii 0123456789"
    out.append(("ASCII 110/170", ascii_encode(t, af=1000), lambda: make("ascii", 1000), t, 0))
    t = "CQ CQ CQ DE FFL QSL 73 TEST SITOR ARQ 0123456789"
    out.append(("SITOR-A", U.sitora_encode(t, repeat=(5, 9), irs=0.5), lambda: make("sitora", 1000), t, 0))
    fr = U.ax25_bytes("F1NSK-9", "APRS", "!4903.50N/00201.75E>Orsat-Decoder", ["WIDE1-1"])
    t = "F1NSK-9>APRS,WIDE1-1: !4903.50N/00201.75E>Orsat-Decoder  (49.0583, 2.0292)\n"
    out.append(("Packet 1200", U.packet_encode([fr]), lambda: make("packet1200", 0), t, 12))
    out.append(("Packet 300", U.packet_encode([fr], baud=300.0, mark=1800.0, space=1600.0), lambda: make("packet300", 1700), t, 6))
    from orsatdec.decoders import dsc as D
    fields = [0, 50, 30, 0, 10, 100, 0, 23, 20, 0, 10, 109, 126, 8, 29, 10, 8, 29, 10]
    t = D.parse(U.dsc_message(120, fields, 122)[0] + [U.dsc_message(120, fields, 122)[1]])[0] + "\n"
    out.append(("DSC HF", U.dsc_encode(120, fields, 122), lambda: make("dsc", 1700), t, -3))
    out.append(("DSC VHF", U.dsc_encode(120, fields, 122, baud=1200.0, shift=800.0), lambda: make("dsc_vhf", 0), t, 10))
    t = "SELCAL AB-CD\n"
    out.append(("Selcal OACI", U.icao_selcal_encode("AB-CD"), lambda: make("selcal", 0), t, 0))
    t = "DTMF 0612345789*#\n"
    out.append(("DTMF", U.dtmf_encode("0612345789*#"), lambda: make("dtmf", 0), t, 6))
    t = "ZVEI2 12334  (65 ms, écart 0.0 %)\n"
    out.append(("5 tons ZVEI2", U.selcall_encode("12334", "ZVEI2"), lambda: make("selcall5", 0), "ZVEI2 12334", 3))
    msgs = [(1234567, 0, "0612345678", False), (2000008, 3, "Intervention VSAV rue de la Paix", True)]
    t = "POCSAG1200 1234567 f0 : 0612345678\nPOCSAG1200 2000008 f3 : Intervention VSAV rue de la Paix\n"
    out.append(("POCSAG 1200", U.pocsag_encode(msgs, 1200), lambda: make("pocsag", 0), t, 6))
    mins = [(2026, 10, 6, 2, 14, 23), (2026, 10, 6, 2, 14, 24)]
    for k, exp, snr in (("dcf77", "DCF77 : mardi 06/10/2026 14:23 heure d'été", 0), ("msf", "MSF : mardi 06/10/2026 14:23 GMT", 0),
                        ("wwvb", "WWVB : 06/10/2026 14:23 UTC", 0), ("jjy", "JJY : 06/10/2026 14:23 UTC", 0),
                        ("wwv", "WWV/WWVH : 06/10/2026 14:23 UTC", 6), ("tdf", "TDF : mardi 06/10/2026 14:24 heure d'été", 10)):
        out.append((k.upper(), U.timecode_encode(k, mins), (lambda k: lambda: make(k, 1000))(k), exp, snr))
    out.append(("CHU", U.chu_encode(2026, 279, 14, 23), lambda: make("chu", 0), "CHU : jour 279, 14:23", 6))
    from orsatdec.gen.pskx import qpsk_encode, pskr_encode
    t = "CQ CQ DE F1NSK ORSAT TEST 0123456789"
    out.append(("QPSK31", qpsk_encode(t, af=1003), lambda: make("qpsk31", 1000), t, -6))
    out.append(("PSK250R", pskr_encode(t + "  ", af=1003, baud=250.0, depth=80), lambda: make("psk250r", 1000), t, -3))
    t = "CQ CQ DE F1NSK ORSAT 73 TEST?"
    out.append(("THROB2", U.throb_encode(t, "THROB2"), lambda: make("throb2", 1000), t, -10))
    out.append(("THROBX1", U.throb_encode(t, "THROBX1"), lambda: make("throbx1", 1000), t, -10))
    t = "f1nsk:allcall Hello from Orsat-Decoder, 73! 0123456789"
    out.append(("FSQ 3", U.fsq_encode(t, 3.0), lambda: make("fsq", 1500), t, -10))
    out.append(("IFKP", U.fsq_encode(t, 2, variant="ifkp"), lambda: make("ifkp", 1500), t, -6))
    w = U.ale_call("F1NSK", "ORSAT", "HELLO FROM ORSAT DECODER")
    out.append(("ALE 2G", U.ale_encode(w), lambda: make("ale", 1625),
                "ALE TO F1NSK · CMD HELLO FROM ORSAT DECODER · TIS ORSAT\n", -3))
    return out


def selftest_cases():
    return [(lab, sig, mk(), exp, snr) for lab, sig, mk, exp, snr in cases()]


def main():
    for lab, sig, mk, exp, _ in cases():
        x = np.concatenate([np.zeros(FS // 2), sig, np.zeros(FS // 2)])
        res = []
        for snr in (20, 10, 6, 3, 0, -3):
            res.append(f"{snr:>3} dB {score(exp, run(mk(), add_noise(x, snr, FS, seed=7))):4.0%}")
        print(f"{lab:<16}", "  ".join(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
