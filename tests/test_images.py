"""Essais des modes image : fac-similé (WEFAX), SSTV, Hellschreiber.

Mesures : PSNR de l'image décodée contre l'original à plusieurs S/B (bruit dans 2500 Hz), décalage
d'accord, erreur d'horloge (pente), départ manqué (roue libre), disparition du signal en cours d'image,
bruit seul, vitesse. Pour la SSTV, le décodeur est aussi validé avec un codeur indépendant (PySSTV).
Les images décodées sont enregistrées (PNG, Pillow) dans IMG_DIR pour inspection visuelle.

    /tmp/claude-0/venv/bin/python tests/test_images.py [fax|sstv|hell]
"""
import base64
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from orsatdec.gen.encoders import add_noise  # noqa: E402
from orsatdec.gen.images import (test_image, chart_image, wefax_encode, sstv_encode, hell_encode,  # noqa: E402
                                 hell_columns, HELL_GEN)
from orsatdec.decoders.wefax import WeFax  # noqa: E402
from orsatdec.decoders.sstv import SSTV, SSTV_MODES  # noqa: E402
from orsatdec.decoders.hell import Hell, ROWS  # noqa: E402

FS = 12000
IMG_DIR = Path(os.environ.get("ORSAT_IMG_DIR",
                              "/tmp/claude-0/-home-claude/fd561b07-b251-5449-a0fb-36def3538a2f/scratchpad/img"))


def save_png(name, arr):
    try:
        from PIL import Image
    except ImportError:
        return
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(arr, np.uint8)).save(IMG_DIR / name)


def run_img(dec, x, block=480):
    """Décode et reconstitue la (première) image ; renvoie (image, événements, x temps réel, instant de fin)."""
    evs = []
    t = time.time()
    t_end = None
    for i in range(0, len(x), block):
        e = dec.process(x[i:i + block])
        for ev in e:
            if ev["op"] == "end" and t_end is None:
                t_end = (i + block) / FS
        evs += e
    rt = len(x) / FS / max(time.time() - t, 1e-9)
    img = None
    W = fmt = None
    rows = {}
    for ev in evs:
        if ev["op"] == "new":
            if img is not None or rows:
                break
            W, fmt = ev["w"], ev["fmt"]
        elif ev["op"] == "rows":
            c = 3 if fmt == "rgb" else 1
            a = np.frombuffer(base64.b64decode(ev["data"]), np.uint8).reshape(ev["n"], W, c)
            for k in range(ev["n"]):
                rows[ev["y"] + k] = a[k]
        elif ev["op"] == "end":
            break
    if rows:
        H = max(rows) + 1
        img = np.zeros((H, W, 3 if fmt == "rgb" else 1), np.uint8)
        for k, r in rows.items():
            img[k] = r
        if fmt != "rgb":
            img = img[:, :, 0]
    return img, evs, rt, t_end


def psnr(a, b):
    m = np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2)
    return 10 * np.log10(255 ** 2 / max(m, 1e-9))


def best_psnr(dec_img, ref, max_dy=3):
    """PSNR après recalage vertical (la 1re ligne d'un fax est la ligne blanche de fin de phasage)."""
    if dec_img is None:
        return 0.0
    best = 0.0
    H = ref.shape[0] - max_dy
    for dy in range(0, max_dy + 1):
        if dec_img.shape[0] >= dy + H:
            best = max(best, psnr(dec_img[dy:dy + H], ref[:H]))
    return best


# ====================================================================== FAX
def fax_case(snr=20.0, ppm=0.0, off=0.0, lines=120, skip=0.0, seed=3, lpm=120, ioc=576, cut=None, tail=15):
    W = int(ioc * np.pi)
    ref = chart_image(W, lines)
    x = wefax_encode(ref, fs=FS, af=1900 + off, lpm=lpm, ioc=ioc, ppm=ppm)
    if skip:
        x = x[int(skip * FS):]
    if cut:
        x = x[:int(cut * FS)]
    x = np.concatenate([np.zeros(FS * 2), x, np.zeros(FS * tail)])
    x = add_noise(x, snr, FS, seed=seed)
    img, evs, rt, t_end = run_img(WeFax(FS, 1900, ioc=ioc, lpm=lpm), x)
    if skip and img is not None:
        # roue libre : la première ligne reçue est une ligne quelconque de l'original
        Tl = 60.0 / lpm
        first = int((skip - 5 - 21 * Tl) / Tl)
        best = 0.0
        for d0 in range(max(0, first - 3), first + 4):
            r = ref[d0:]
            for dy in range(0, 4):
                n = min(len(r), img.shape[0] - dy) - 2
                if n > 10:
                    best = max(best, psnr(img[dy:dy + n], r[:n]))
        return best, img, evs, rt, t_end
    return best_psnr(img, ref), img, evs, rt, t_end


def test_fax():
    print("== Fac-similé IOC 576, 120 l/min (carte 1809 px de large)")
    print("  S/B (dB)   PSNR (dB)")
    for snr in (30, 15, 10, 5, 0, -3):
        p, img, evs, rt, _ = fax_case(snr)
        print(f"  {snr:>6}     {p:6.1f}      (x{rt:.0f} temps réel)")
        save_png(f"fax_snr{snr}.png", img if img is not None else np.zeros((2, 2)))
    for ppm, off in ((300, 30), (-300, -30), (0, 100), (1000, 0)):
        p, img, *_ = fax_case(10, ppm=ppm, off=off)
        print(f"  horloge {ppm:+5.0f} ppm, accord {off:+4.0f} Hz, 10 dB : PSNR {p:5.1f} dB")
        save_png(f"fax_ppm{ppm}_off{off}.png", img if img is not None else np.zeros((2, 2)))
    p, img, evs, rt, _ = fax_case(15, ppm=200, skip=40, lines=200)
    print(f"  départ manqué (roue libre), +200 ppm, 15 dB : PSNR {p:5.1f} dB, "
          f"titre « {[e for e in evs if e['op'] == 'new'][0]['title'] if evs else '-'} »")
    save_png("fax_freerun.png", img if img is not None else np.zeros((2, 2)))
    p, img, evs, rt, t_end = fax_case(10, lines=200, cut=60, tail=20)
    print(f"  signal coupé à 60 s : fin d'image émise à {t_end} s (coupure à {62} s, délai {WeFax(FS).timeout:.0f} s)")
    p, img, *_ = fax_case(10, lpm=60, ioc=288, lines=60)
    print(f"  IOC 288, 60 l/min, 10 dB : PSNR {p:5.1f} dB")
    save_png("fax_ioc288.png", img if img is not None else np.zeros((2, 2)))


# ====================================================================== SSTV
_PY_CACHE = {}


def pysstv_audio(img, mode, ppm=0.0):
    key = (mode, ppm)
    if key not in _PY_CACHE:
        _PY_CACHE[key] = _pysstv_audio(img, mode, ppm)
    return _PY_CACHE[key]


def _pysstv_audio(img, mode, ppm=0.0):
    import pysstv.color as pc
    from PIL import Image
    cls = {"martin1": pc.MartinM1, "martin2": pc.MartinM2, "scottie1": pc.ScottieS1, "scottie2": pc.ScottieS2,
           "scottiedx": pc.ScottieDX, "robot36": pc.Robot36, "pd90": pc.PD90, "pd120": pc.PD120,
           "pd160": pc.PD160, "pd180": pc.PD180, "pd240": pc.PD240, "pd290": pc.PD290,
           "sc2_180": pc.WraaseSC2180}[mode]
    pim = Image.fromarray(img)
    if (cls.WIDTH, cls.HEIGHT) != pim.size:
        pim = pim.resize((cls.WIDTH, cls.HEIGHT), Image.BILINEAR)
    s = cls(pim, int(round(FS * (1 + ppm * 1e-6))), 16)
    return 0.5 * np.fromiter(s.gen_values(), np.float64)


def sstv_case(mode, snr=30.0, off=0.0, ppm=0.0, enc="orsat", cut=None, seed=5):
    m = SSTV_MODES[mode]
    ref = test_image(m["w"], m["h"])
    if enc == "orsat":
        x = sstv_encode(ref, mode, fs=FS, af=1900 + off, ppm=ppm)
    else:
        x = pysstv_audio(ref, mode, ppm)
        if off:
            from scipy.signal import hilbert
            t = np.arange(len(x)) / FS
            x = np.real(hilbert(x) * np.exp(2j * np.pi * off * t))
        if (m["w"], m["h"]) != (ref.shape[1], ref.shape[0]):
            pass
    if cut:
        x = x[:int(cut * FS)]
    x = np.concatenate([np.zeros(FS), x, np.zeros(FS * 8)])
    x = add_noise(x, snr, FS, seed=seed)
    img, evs, rt, t_end = run_img(SSTV(FS, 1900), x)
    if img is None:
        return 0.0, None, evs, rt, t_end
    if img.shape[0] < m["h"]:
        img = np.concatenate([img, np.zeros((m["h"] - img.shape[0],) + img.shape[1:], np.uint8)])
    if enc != "orsat" and pysstv_width(mode) != m["w"]:
        # PySSTV émet Martin M2 / Scottie S2 sur 160 points : même image de référence ré-échantillonnée
        from PIL import Image
        ref = np.array(Image.fromarray(ref).resize((pysstv_width(mode), m["h"]), Image.BILINEAR)
                       .resize((m["w"], m["h"]), Image.NEAREST))
    return psnr(img, ref), img, evs, rt, t_end


def pysstv_width(mode):
    return 160 if mode in ("martin2", "scottie2") else SSTV_MODES[mode]["w"]


def test_sstv():
    print("== SSTV : PSNR (dB) image décodée / originale, 30 dB de S/B")
    print("  mode           codeur Orsat   PySSTV")
    py_modes = {"martin1", "martin2", "scottie1", "scottie2", "scottiedx", "robot36", "pd90", "pd120", "pd180",
                "sc2_180", "pd160", "pd240", "pd290"}
    speeds = []
    for mode in ("martin1", "martin2", "scottie1", "scottie2", "scottiedx", "robot36", "robot72",
                 "pd50", "pd90", "pd120", "pd180", "sc2_180"):
        p1, img, evs, rt, _ = sstv_case(mode)
        speeds.append(rt)
        save_png(f"sstv_{mode}.png", img if img is not None else np.zeros((2, 2, 3)))
        p2 = "   -"
        if mode in py_modes:
            q, img2, *_ = sstv_case(mode, enc="pysstv")
            p2 = f"{q:5.1f}"
            save_png(f"sstv_{mode}_pysstv.png", img2 if img2 is not None else np.zeros((2, 2, 3)))
        print(f"  {SSTV_MODES[mode]['name']:<15} {p1:6.1f}        {p2}")
    print(f"  vitesse : x{min(speeds):.0f} à x{max(speeds):.0f} temps réel")
    print("== SSTV : PSNR selon le S/B (codeur Orsat / PySSTV)")
    snrs = (30, 15, 10, 5, 0, -5)
    print("  mode           " + "".join(f"{s:>12}" for s in snrs))
    for mode in ("martin1", "scottie1", "robot36", "pd120"):
        cells = []
        for snr in snrs:
            a, img, *_ = sstv_case(mode, snr=snr)
            b, img2, *_ = sstv_case(mode, snr=snr, enc="pysstv")
            cells.append(f"{a:5.1f}/{b:5.1f}")
            if mode == "martin1":
                save_png(f"sstv_martin1_snr{snr}.png", img if img is not None else np.zeros((2, 2, 3)))
        print(f"  {SSTV_MODES[mode]['name']:<15}" + "".join(f"{c:>12}" for c in cells))
    print("== SSTV : accord et horloge (Martin M1 / PD 120, 15 dB)")
    for mode in ("martin1", "pd120"):
        for off, ppm, enc in ((100, 0, "orsat"), (-100, 0, "orsat"), (60, 0, "pysstv"), (0, 1000, "orsat"),
                              (0, -1000, "pysstv"), (-50, 500, "orsat")):
            p, img, *_ = sstv_case(mode, snr=15, off=off, ppm=ppm, enc=enc)
            print(f"  {SSTV_MODES[mode]['name']:<10} accord {off:+4d} Hz, horloge {ppm:+5d} ppm ({enc:6}) : PSNR {p:5.1f} dB")
            if mode == "martin1" and ppm == 1000:
                save_png("sstv_martin1_ppm1000.png", img if img is not None else np.zeros((2, 2, 3)))
    p, img, evs, rt, t_end = sstv_case("martin1", snr=10, cut=60)
    rows = max([e["y"] + e["n"] for e in evs if e["op"] == "rows"] or [0])
    print(f"  signal coupé à 61 s (Martin M1, 10 dB) : fin d'image à {t_end} s, {rows} lignes reçues")
    save_png("sstv_martin1_cut.png", img if img is not None else np.zeros((2, 2, 3)))


# ====================================================================== HELL
TEXT = "CQ CQ DE F1NSK F1NSK ORSAT DECODER HELL TEST 0123456789"


def hell_ink_ref(text, mode):
    """Flux d'encre idéal (2 échantillons par point émis), dans l'ordre d'émission."""
    cols = hell_columns(text)
    bits = np.array([(c >> i) & 1 for c in cols for i in range(14)], np.float64)
    return np.repeat(bits, 2)


def hell_case(mode, snr=10.0, off=0.0, ppm=0.0, seed=2, save=None):
    x = hell_encode(TEXT, mode, fs=FS, af=1000 + off, ppm=ppm)
    x = np.concatenate([np.zeros(FS * 2), x, np.zeros(FS * 2)])
    x = add_noise(x, snr, FS, seed=seed)
    d = Hell(FS, 1000, mode=mode)
    evs = []
    t = time.time()
    for i in range(0, len(x), 480):
        evs += d.process(x[i:i + 480])
    rt = len(x) / FS / max(time.time() - t, 1e-9)
    cols = [np.frombuffer(base64.b64decode(e["data"]), np.uint8).reshape(e["n"], 2 * ROWS)
            for e in evs if e["op"] == "cols"]
    if not cols:
        return 0.0, rt
    tape = np.concatenate(cols)
    if save:
        save_png(save, tape.T)
    # flux reçu : moitié haute de chaque colonne (colonne courante), remise dans l'ordre d'émission
    uniq = tape[::d.hrep, :ROWS][:, ::-1].reshape(-1)
    ink = 1 - uniq / 255.0
    ref = hell_ink_ref(TEXT, mode)
    # corrélation normalisée maximale (le point de départ est quelconque)
    a = ink - ink.mean()
    b = ref - ref.mean()
    a = np.concatenate([np.zeros(len(b)), a, np.zeros(len(b))])
    N = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    c = np.fft.irfft(np.fft.rfft(a, N) * np.conj(np.fft.rfft(b, N)), N)[:len(a) - len(b) + 1]
    k = int(np.argmax(c))
    seg = a[k:k + len(b)]
    r = float(np.sum(seg * b) / np.sqrt(np.sum(seg * seg) * np.sum(b * b) + 1e-12))
    return r, rt


def test_hell():
    print("== Hellschreiber : corrélation du flux reçu avec le flux émis (1 = parfait ; lisible ≳ 0,5)")
    snrs = (20, 10, 5, 0, -5, -8)
    print("  mode          " + "".join(f"{s:>7}" for s in snrs) + "   vitesse")
    for mode in ("feld", "slow", "x5", "x9", "fsk245", "fsk105", "hell80"):
        cells, rts = [], []
        for snr in snrs:
            r, rt = hell_case(mode, snr, save=f"hell_{mode}_snr{snr}.png" if snr in (10, -5) else None)
            cells.append(f"{r:7.2f}")
            rts.append(rt)
        print(f"  {mode:<12}" + "".join(cells) + f"   x{min(rts):.0f}")
    for off in (30, -30):
        r, _ = hell_case("feld", 5, off=off)
        r2, _ = hell_case("fsk245", 5, off=off)
        print(f"  accord {off:+d} Hz, 5 dB : Feld {r:.2f}, FSK245 {r2:.2f}")


def test_noise():
    print("== Bruit seul, 60 s : aucune image ne doit démarrer")
    rng = np.random.default_rng(7)
    x = rng.normal(0, 0.1, FS * 60)
    for name, d in (("WEFAX", WeFax(FS)), ("SSTV", SSTV(FS)), ("Feld Hell", Hell(FS)),
                    ("FSK Hell 245", Hell(FS, mode="fsk245")), ("Hell X9", Hell(FS, mode="x9"))):
        evs = []
        for i in range(0, len(x), 480):
            evs += d.process(x[i:i + 480])
        n = sum(1 for e in evs if e["op"] in ("new", "rows", "cols"))
        print(f"  {name:<14} {'OK' if n == 0 else 'ÉCHEC'} ({n} événements image)")


# ====================================================================== autotest global
def _fax_quick():
    p, img, evs, rt, _ = fax_case(10, lines=24, tail=8)
    return p >= 15, f"PSNR {p:.1f} dB à 10 dB"


def _sstv_quick():
    p, img, evs, rt, _ = sstv_case("robot36", snr=10)
    return p >= 12, f"Robot 36, PSNR {p:.1f} dB à 10 dB"


def _hell_quick():
    r, _ = hell_case("feld", 0)
    return r >= 0.6, f"Feld Hell, corrélation {r:.2f} à 0 dB"


def selftest_cases():
    """Cas rapides pour l'autotest global : (libellé, fonction) ; fonction() -> (ok, détail)."""
    return [("WEFAX", _fax_quick), ("SSTV", _sstv_quick), ("Hell", _hell_quick)]


if __name__ == "__main__":
    what = sys.argv[1:] or ["fax", "sstv", "hell", "noise"]
    t0 = time.time()
    if "fax" in what:
        test_fax()
    if "sstv" in what:
        test_sstv()
    if "hell" in what:
        test_hell()
    if "noise" in what:
        test_noise()
    if "self" in what or not sys.argv[1:]:
        print("== Autotest rapide")
        for label, fn in selftest_cases():
            t = time.time()
            ok, det = fn()
            print(f"  {'OK ' if ok else 'ÉCHEC'}  {label:<6} {det}  ({time.time() - t:.1f} s)")
    print(f"(durée totale {time.time() - t0:.0f} s)")
