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
    print(f"  {'OK ' if ok else 'ÉCHEC'}  {label:<16} {snr:>4} dB   {sc:5.0%}")
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
    # Comme en direct : le créneau reçu n'a jamais exactement 15 s (blocs FLAC de 420, etc.)
    from orsatdec.decoders.ft8 import FT8
    d = FT8(FS, 1500)
    d._decode(np.concatenate([x, np.zeros(420)]), 0)
    ok2 = any("CQ F1NSK JN03" in r["text"] for r in d.results)
    print(f"  {'OK ' if ok2 else 'ÉCHEC'}  FT8 direct  -14 dB   {'décodé' if ok2 else 'non décodé (créneau trop long)'}")
    return ok and ok2


def check_wspr():
    """WSPR : deux balises à -24 dB dans un créneau de 2 minutes, décodées par wsprd (native/)."""
    if not (ROOT / "native" / "bin" / "wspr_decode_iq").exists():
        print("  ÉCHEC  WSPR       décodeur natif absent (native/build.sh)")
        return False
    from orsatdec.gen.utility import wspr_encode
    from orsatdec.decoders.wspr import to_iq, decode_iq
    x = add_noise(wspr_encode([("F1NSK JN03 30", -20, 1.0), ("K1JT FN20 37", 45, 1.0)]), -24, FS, seed=3)
    got = {m["call"] for m in decode_iq(to_iq(x, FS))}
    ok = {"F1NSK", "K1JT"} <= got
    print(f"  {'OK ' if ok else 'ÉCHEC'}  WSPR        -24 dB   {len(got)} balise(s) décodée(s)")
    return ok


def check_js8():
    """JS8 normal : deux trames (texte libre compressé JSC et balise) à -12 dB dans un créneau de 15 s."""
    from orsatdec.decoders import js8 as J
    m1 = J.data_frame("HELLO WORLD 73")
    x = add_noise(J.js8_audio([(1000.0, m1, J.DATA | J.FIRST | J.LAST), (1700.0, "ABCDEFGHIJKL", 1)]), -12, FS, seed=5)
    got = [J.frame_text(d["bits72"], d["i3"])[0] for d in J.decode_window(x, FS, "A")]
    ok = "HELLO WORLD 73" in got and len(got) == 2
    print(f"  {'OK ' if ok else 'ÉCHEC'}  JS8         -12 dB   {len(got)} trame(s) : {', '.join(g.strip() for g in got)}")
    return ok


def check_flac():
    """Chaîne FLAC des serveurs PhantomSDR-Plus : encodage en blocs de 256, décodage en continu."""
    try:
        import pyflac
        from orsatdec.codecs import FlacStream
    except Exception as e:
        print(f"  ÉCHEC  FLAC        module absent ({e})")
        return False
    import time
    msg = "CQ CQ DE F1NSK FLAC TEST 0123456789"
    x = np.concatenate([np.zeros(FS // 2), psk_encode(msg, fs=FS, af=1000), np.zeros(FS // 2)])
    pcm = (np.clip(x, -1, 1) * 32000).astype(np.int16)
    chunks = []
    enc = pyflac.StreamEncoder(write_callback=lambda buf, nb, ns, cf: chunks.append(bytes(buf)),
                               sample_rate=FS, blocksize=256)
    for i in range(0, len(pcm), 1024):
        enc.process(pcm[i:i + 1024])
    enc.finish()
    out = []
    dec_psk = PSK(FS, 1000)
    stream = FlacStream(lambda y, fs: out.extend(e["text"] for e in dec_psk.process(y)))
    for c in chunks:
        stream.feed(c)
    time.sleep(0.5)
    stream.close()
    sc = score(msg, "".join(out))
    ok = sc >= 0.9
    print(f"  {'OK ' if ok else 'ÉCHEC'}  FLAC         flux   {sc:5.0%}")
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
        check_wspr(),
        check_js8(),
        check_flac(),
    ]
    # jalon 2 : chaque banc de test fournit ses cas rapides
    import importlib
    for name in ("test_mfsk", "test_olivia", "test_mt63", "test_utility"):
        try:
            cases = importlib.import_module(name).selftest_cases()
        except Exception as e:
            print(f"  ÉCHEC  {name[5:]:<10} {e}")
            results.append(False)
            continue
        for label, sig, dec, expected, snr in cases:
            results.append(check(label, sig, dec, expected, snr))
    try:
        for label, fn in importlib.import_module("test_images").selftest_cases():
            ok, detail = fn()
            print(f"  {'OK ' if ok else 'ÉCHEC'}  {label:<16} {detail}")
            results.append(ok)
    except Exception as e:
        print(f"  ÉCHEC  images     {e}")
        results.append(False)
    try:
        for label, fn in importlib.import_module("test_sigid").selftest_cases():
            ok, detail = fn()
            print(f"  {'OK ' if ok else 'ÉCHEC'}  {label:<16} {detail}")
            results.append(ok)
    except Exception as e:
        print(f"  ÉCHEC  identification {e}")
        results.append(False)
    n = sum(results)
    print(f"{n}/{len(results)} décodeurs validés.")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
