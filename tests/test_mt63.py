"""Banc d'essai MT63 (500/1000/2000 Hz, entrelacement court/long ; bruit blanc, convention 2500 Hz).

Pour chaque sous-mode : taux de caractères corrects à plusieurs S/B, S/B le plus bas avec >= 90 %,
acquisition avec un clic décalé, dérive lente, bruit seul (60 s) et vitesse (x temps réel).
Puis, si les sources de fldigi et g++ sont présents, contre-vérification avec le code MT63 de fldigi
compilé seul (MT63tx/MT63rx de mt63base.cxx + dsp.cxx) :
  - notre générateur doit reproduire l'audio de MT63tx échantillon par échantillon ;
  - notre décodeur doit lire l'audio produit par MT63tx ;
  - MT63rx de fldigi doit lire l'audio de notre générateur.

    /tmp/claude-0/venv/bin/python tests/test_mt63.py            # tout
    /tmp/claude-0/venv/bin/python tests/test_mt63.py 1000       # filtre sur le nom du sous-mode
    /tmp/claude-0/venv/bin/python tests/test_mt63.py --rapide   # balayage S/B seulement
    FLDIGI_SRC=/chemin/fldigi/src ...                           # emplacement des sources fldigi
"""
import os
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy.signal import hilbert, resample_poly

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from harness import run, score  # noqa: E402
from orsatdec.gen.encoders import add_noise  # noqa: E402
from orsatdec.gen.mt63 import mt63_encode, mt63_encode_8k  # noqa: E402
from orsatdec.decoders.mt63 import MT63  # noqa: E402

FS = 12000
TEXT = ("CQ CQ CQ DE F1NSK F1NSK PSE K. THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG 0123456789. "
        "NOW IS THE TIME FOR ALL GOOD MEN TO COME TO THE AID OF THE PARTY, 73 ES GL.")
SUBMODES = [(bw, il) for bw in (500, 1000, 2000) for il in ("court", "long")]
SNRS = {500: [-10, -11, -12, -13], 1000: [-7, -8, -9, -10], 2000: [-4, -5, -6, -7]}
SEEDS = (1, 2, 3)


def name(bw, il):
    return f"MT63-{bw}{'L' if il == 'long' else 'S'}"


def tail(bw):
    """Silence final : laisse sortir les caractères encore dans les tuyaux (~5 s en 1000 Hz)."""
    return np.zeros(int(FS * 7 * 1000 / bw))


def signal(bw, il, text=TEXT, af=None, tones=0.0):
    af0 = 500 + bw / 2
    x = mt63_encode(text, FS, af if af is not None else af0, bw, il == "long", tones=tones)
    return np.concatenate([np.zeros(FS // 2), x, tail(bw)])


def decode(bw, il, x, af=None, **kw):
    d = MT63(FS, af if af is not None else 500 + bw / 2, bw, il, **kw)
    return run(d, x, block=480), d


def sweep(bw, il):
    x0 = signal(bw, il)
    res = {}
    for snr in SNRS[bw]:
        res[snr] = float(np.mean([score(TEXT, decode(bw, il, add_noise(x0, snr, FS, seed=s))[0]) for s in SEEDS]))
    ok = [s for s, v in res.items() if v >= 0.9]
    low = min(ok) if ok else None
    print(f"  {name(bw, il):<11} " + "  ".join(f"{s:>4} dB {v:4.0%}" for s, v in res.items())
          + f"   -> >=90 % jusqu'à {low} dB")
    return low


def offset_test(bw, il):
    sp = bw / 64                       # écart entre porteuses
    out = []
    for off in (-2.3 * sp, -1.2 * sp, 1.6 * sp, 3.1 * sp):
        x = add_noise(signal(bw, il, af=500 + bw / 2 + off), {500: -9, 1000: -6, 2000: -3}[bw], FS, seed=4)
        txt, d = decode(bw, il, x)
        out.append(f"{off:+.0f} Hz {score(TEXT, txt):4.0%}")
    print(f"  {name(bw, il):<11} clic décalé : " + "  ".join(out))


def drift_test(bw, il, rate):
    x = signal(bw, il)
    t = np.arange(len(x)) / FS
    f0 = 500 + bw / 2
    x = np.real(hilbert(x) * np.exp(2j * np.pi * 0.5 * rate * t ** 2))
    snr = {500: -9, 1000: -6, 2000: -3}[bw]
    sc = [score(TEXT, decode(bw, il, add_noise(x, snr, FS, seed=s), af=f0)[0]) for s in (1, 2)]
    print(f"  {name(bw, il):<11} dérive {rate} Hz/s ({rate * len(x) / FS:.0f} Hz en {len(x) / FS:.0f} s), "
          f"{snr} dB : {np.mean(sc):4.0%}")


def noise_test(bw, il):
    x = np.random.default_rng(7).normal(0, 0.25, FS * 60)
    txt, _ = decode(bw, il, x)
    print(f"  {name(bw, il):<11} bruit seul 60 s : {len(txt)} caractère(s) {txt[:30]!r}")
    return len(txt)


def speed_test(bw, il, fs=FS):
    x = add_noise(np.concatenate([np.zeros(fs // 2),
                                  mt63_encode(TEXT[:60], fs, 500 + bw / 2, bw, il == "long")]), -5, fs)
    d = MT63(fs, 500 + bw / 2, bw, il)
    t0 = time.process_time()
    run(d, x, block=int(fs * 0.04))
    dt = time.process_time() - t0
    return len(x) / fs / max(dt, 1e-9)


# --------------------------------------------------------------------- contre-vérification fldigi
HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <string>
#include "dsp.h"
#define private public
#include "mt63base.h"
#undef private
static void emit(MT63tx &Tx, FILE *f, bool norm, double &maxval) {
    int len = Tx.Comb.Output.Len;
    for (int i = 0; i < len; i++) if (norm && fabs(Tx.Comb.Output.Data[i]) > maxval) maxval = fabs(Tx.Comb.Output.Data[i]);
    for (int i = 0; i < len; i++) { float v = norm ? Tx.Comb.Output.Data[i] / maxval : Tx.Comb.Output.Data[i]; fwrite(&v, 4, 1, f); }
}
static void put(int c) { if ((c >= 32 && c < 127) || c == '\n') putchar(c); }
int main(int argc, char **argv) {
    std::string mode = argv[1];
    int bw = atoi(argv[2]), lng = atoi(argv[3]); double freq = atof(argv[4]);
    if (mode == "tx") {           // tx BW LONG FREQ texte sortie.f32 zero raw
        FILE *tf = fopen(argv[5], "rb"); std::string text; int c;
        while ((c = fgetc(tf)) != EOF) text += (char)c;
        fclose(tf);
        FILE *of = fopen(argv[6], "wb");
        bool zero = atoi(argv[7]), raw = atoi(argv[8]);
        MT63tx Tx;
        Tx.Preset(freq, bw, lng); Tx.Preset(freq, bw, lng);   // 2e appel : FFT.Size initialisé, comme dans fldigi
        if (zero) memset(Tx.Encoder.IntlvPipe, 0, Tx.Encoder.IntlvSize);
        double maxval = 0;
        for (int i = 0; i < Tx.DataInterleave; i++) { Tx.SendChar(0); emit(Tx, of, !raw, maxval); }
        for (size_t k = 0; k < text.size(); k++) {
            int ch = (unsigned char)text[k];
            if (ch > 127) { Tx.SendChar(127); emit(Tx, of, !raw, maxval); ch &= 127; }
            Tx.SendChar(ch); emit(Tx, of, !raw, maxval);
        }
        int flush = Tx.DataInterleave;
        while (--flush) { Tx.SendChar(0); emit(Tx, of, !raw, maxval); }
        if (!zero) { Tx.SendJam(); maxval = 0; emit(Tx, of, !raw, maxval); }
        fclose(of);
        return 0;
    }
    // rx BW LONG FREQ INTEG entree.f32
    int integ = atoi(argv[5]);
    FILE *f = fopen(argv[6], "rb");
    MT63rx Rx; Rx.Preset(freq, bw, lng, integ); Rx.Preset(freq, bw, lng, integ);
    double_buff In; float buf[512]; size_t n;
    while ((n = fread(buf, 4, 512, f)) > 0) {
        In.EnsureSpace(n); for (size_t i = 0; i < n; i++) In.Data[i] = buf[i]; In.Len = n;
        Rx.Process(&In);
        for (int i = 0; i < Rx.Output.Len; i++) put((unsigned char)Rx.Output.Data[i]);
    }
    for (int k = 0; k < 400 && Rx.SYNC_LockStatus(); k++) {      // comme mt63::rx_flush
        In.EnsureSpace(512); for (int i = 0; i < 512; i++) In.Data[i] = 0; In.Len = 512;
        Rx.Process(&In);
        for (int i = 0; i < Rx.Output.Len; i++) put((unsigned char)Rx.Output.Data[i]);
    }
    return 0;
}
'''


def build_fldigi(tmp):
    src = Path(os.environ.get("FLDIGI_SRC", "/tmp/claude-0/fldigi/src"))
    if not (src / "mt63" / "mt63base.cxx").exists():
        return None
    (Path(tmp) / "config.h").write_text("")
    (Path(tmp) / "h.cxx").write_text(HARNESS)
    exe = Path(tmp) / "mt63tool"
    r = subprocess.run(["g++", "-O2", "-w", f"-I{tmp}", f"-I{src}/include", f"-I{src}/mt63", "-o", str(exe),
                        str(Path(tmp) / "h.cxx"), str(src / "mt63" / "mt63base.cxx"), str(src / "mt63" / "dsp.cxx")],
                       capture_output=True, text=True)
    return exe if r.returncode == 0 else None


def resample(x, fs_in, fs_out):
    fr = Fraction(fs_out / fs_in).limit_denominator(1000)
    return resample_poly(x, fr.numerator, fr.denominator)


def fldigi_crosscheck():
    print("\nContre-vérification avec le code MT63 de fldigi compilé seul :")
    with tempfile.TemporaryDirectory() as tmp:
        exe = build_fldigi(tmp)
        if exe is None:
            print("  (sources fldigi ou g++ absents : test sauté)")
            return True
        ok = True
        tf = Path(tmp) / "t.txt"
        text8 = TEXT + " Éé àç"
        tf.write_bytes(text8.encode("latin-1"))
        raw = Path(tmp) / "a.f32"
        for bw, il in SUBMODES:
            lng = 1 if il == "long" else 0
            af = 500 + bw / 2
            # 1) audio identique (interleaveur prérempli de zéros, sans normalisation)
            errs = []
            for f in (af, 1234.5):
                subprocess.run([str(exe), "tx", str(bw), str(lng), str(f), str(tf), str(raw), "1", "1"], check=True)
                r = np.fromfile(raw, np.float32).astype(float)
                y = mt63_encode_8k(text8, bw, il == "long", f, prefill=None, jam=False, normalize=False)
                s0 = 200 * {500: 8, 1000: 4, 2000: 2}[bw] + 200   # fldigi perd son 1er demi-symbole (EnsureSpace)
                errs.append(np.abs(r[s0:] - y[s0:len(r)]).max() / np.abs(r).max() if len(r) == len(y) else 1.0)
            e = max(errs)
            # 2) notre décodeur lit l'audio de fldigi (rééchantillonné à 12 kHz), propre et à S/B modéré
            subprocess.run([str(exe), "tx", str(bw), str(lng), str(af), str(tf), str(raw), "0", "0"], check=True)
            x = resample(np.fromfile(raw, np.float32).astype(float), 8000, FS)
            x = np.concatenate([np.zeros(FS // 2), 0.5 * x, tail(bw)])
            snr = {500: -9, 1000: -6, 2000: -3}[bw]
            got = run(MT63(FS, af, bw, il), add_noise(x, snr, FS, seed=5), block=480)
            s_moi = score(text8, got)
            # 3) MT63rx de fldigi lit notre audio (8 kHz)
            y = mt63_encode_8k(TEXT, bw, il == "long", af)
            y = np.concatenate([np.zeros(4000), 0.5 * y, np.zeros(8000)])
            y = add_noise(y, snr, 8000, seed=6).astype(np.float32)
            y.tofile(raw)
            out = subprocess.run([str(exe), "rx", str(bw), str(lng), str(af), "16", str(raw)],
                                 capture_output=True, text=True).stdout
            s_fl = score(TEXT, out)
            good = e < 1e-6 and s_moi >= 0.95 and s_fl >= 0.9
            ok &= good
            print(f"  {'OK ' if good else 'ÉCHEC'} {name(bw, il):<11} audio TX écart max {e:.1e} ; "
                  f"nous <- fldigi TX ({snr} dB) {s_moi:4.0%} ; fldigi RX <- nous ({snr} dB) {s_fl:4.0%}")
        return ok


def selftest_cases():
    """Cas rapides pour l'autotest global : (libellé, signal, décodeur, texte attendu, S/B)."""
    msg = "CQ DE F1NSK ORSAT MT63 73"
    out = []
    for bw, il, snr in ((1000, "long", -6), (500, "court", -9)):
        x = np.concatenate([mt63_encode(msg, FS, 500 + bw / 2, bw, il == "long"), tail(bw)])
        out.append((name(bw, il), x, MT63(FS, 500 + bw / 2, bw, il), msg, snr))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--rapide" in sys.argv
    modes = [m for m in SUBMODES if not args or any(a in name(*m) for a in args)]
    print("Sensibilité (score moyen sur %d tirages, bruit dans 2500 Hz ; MultiPSK annonce -8 dB pour MT63) :"
          % len(SEEDS))
    for m in modes:
        sweep(*m)
    if quick:
        return 0
    print("\nAcquisition (clic décalé) :")
    for m in modes:
        offset_test(*m)
    print("\nDérive lente :")
    for bw, il in modes:
        if il == "long":
            drift_test(bw, il, {500: 0.15, 1000: 1.0, 2000: 1.0}[bw])
    print("\nBruit seul :")
    nz = sum(noise_test(*m) for m in modes)
    print("\nVitesse (x temps réel, temps CPU, blocs de 40 ms) :")
    for bw, il in modes:
        sp = {fs: speed_test(bw, il, fs) for fs in (12000, 48000)}
        print(f"  {name(bw, il):<11} " + "  ".join(f"fs={fs}: x{v:.0f}" for fs, v in sp.items()))
    ok = fldigi_crosscheck() if not args else True
    return 0 if ok and nz <= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
