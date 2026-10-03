"""Autotest d'Orsat-Decoder : chaque décodeur doit relire une mire bruitée qu'on vient de générer.
Aucun serveur nécessaire. Code de sortie 0 si tout passe."""
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import (psk_encode, rtty_encode, cw_encode, sitorb_encode, navtex_message,  # noqa: E402
                                   add_noise)
from orsatdec.decoders.psk import PSK  # noqa: E402
from orsatdec.decoders.fsk import RTTY  # noqa: E402
from orsatdec.decoders.cw import CW  # noqa: E402
from orsatdec.decoders.sitor import SitorB  # noqa: E402

FS = 12000


def check(label, sig, dec, expected, snr):
    x = np.concatenate([np.zeros(FS // 2), sig, np.zeros(FS // 2)])
    x = add_noise(x, snr, FS, seed=11)
    sc = score(expected, run(dec, x))
    ok = sc >= 0.9
    print(f"  {'OK ' if ok else 'ÉCHEC'}  {label:<10} {snr:>4} dB   {sc:5.0%}")
    return ok


def check_ft8():
    bindir = ROOT / "native" / "bin"
    gen, dec = bindir / "gen_ft8", bindir / "decode_ft8"
    if not (gen.exists() and dec.exists()):
        print("  ÉCHEC  FT8        décodeur natif absent (native/build.sh)")
        return False
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.wav"
        subprocess.run([str(gen), "CQ F1NSK JN03", str(p), "1500"], capture_output=True)
        w = wave.open(str(p)); x = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768; w.close()
        x = add_noise(x, -14, FS, seed=5)
        x = x / np.max(np.abs(x)) * 0.9
        w = wave.open(str(p), "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(FS)
        w.writeframes((x * 32767).astype("<i2").tobytes()); w.close()
        out = subprocess.run([str(dec), str(p)], capture_output=True, text=True).stdout
    ok = "CQ F1NSK JN03" in out
    print(f"  {'OK ' if ok else 'ÉCHEC'}  FT8         -14 dB   {'décodé' if ok else 'non décodé'}")
    return ok


def main():
    print("Autotest des décodeurs (mires générées, bruit ajouté) :")
    m1 = "CQ CQ DE F1NSK F1NSK ORSAT DECODER TEST 0123456789"
    nav = navtex_message("ORSAT DECODER AUTOTEST NAVTEX 1234")
    results = [
        check("PSK31", psk_encode(m1, fs=FS, af=1004), PSK(FS, 1000), m1, -6),
        check("PSK63", psk_encode(m1, fs=FS, af=1004, baud=62.5), PSK(FS, 1000, baud=62.5), m1, -3),
        check("RTTY", rtty_encode("RYRYRY " + m1, fs=FS, af=1000), RTTY(FS, 1000), "RYRYRY " + m1, 0),
        check("CW", cw_encode(m1, fs=FS, af=700, wpm=20), CW(FS, 700), m1, 0),
        check("Navtex", sitorb_encode(nav, fs=FS, af=1000), SitorB(FS, 1000), nav, 0),
        check_ft8(),
    ]
    n = sum(results)
    print(f"{n}/{len(results)} décodeurs validés.")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
