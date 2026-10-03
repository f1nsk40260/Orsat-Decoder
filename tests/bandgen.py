"""Génère une bande HF synthétique en IQ (float32) et la diffuse en temps réel sur la sortie standard,
pour alimenter un serveur Orsat-SDR / PhantomSDR (driver « stdin », format f32, signal « iq »).

Contenu (centre 7,050 MHz, 192 kHz) :
  CW 20 mpm      7,030 000 MHz
  RTTY 45/170    7,045 000 MHz (centre)
  SITOR-B        7,060 000 MHz (centre)
  PSK31          7,070 500 MHz
  FT8            7,074 000 + 1200 / 1750 Hz (créneaux UTC de 15 s)
"""
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import hilbert

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orsatdec.gen.encoders import psk_encode, rtty_encode, cw_encode, sitorb_encode, navtex_message  # noqa

FS = 192000
CENTER = 7_050_000
AFS = 12000
DUR = 60.0
NATIVE = Path(__file__).resolve().parent.parent / "native" / "bin"


def place(audio, rf_freq_of_audio_zero, n):
    """Audio réel (12 kHz) -> IQ à 192 kHz, l'audio 0 Hz tombant sur rf_freq_of_audio_zero (BLU haute)."""
    up = np.repeat(audio, FS // AFS)                          # suréchantillonnage grossier
    from scipy.signal import firwin, lfilter
    up = lfilter(firwin(255, 5000, fs=FS), 1, up)
    a = hilbert(up)                                           # signal analytique = BLU haute
    a = np.resize(a, n) if len(a) < n else a[:n]
    t = np.arange(n) / FS
    return a * np.exp(2j * np.pi * (rf_freq_of_audio_zero - CENTER) * t)


def loop_to(seconds, x):
    n = int(seconds * AFS)
    reps = int(np.ceil(n / max(1, len(x))))
    return np.tile(np.concatenate([x, np.zeros(AFS)]), reps + 1)[:n]


def build():
    n = int(DUR * FS)
    band = np.zeros(n, np.complex128)
    psk = psk_encode("CQ CQ DE F1NSK F1NSK PSE K ORSAT DECODER TEST BAND ", fs=AFS, af=1000)
    band += place(loop_to(DUR, psk), 7_070_500 - 1000, n)
    rtty = rtty_encode("RYRYRY CQ DE F1NSK TEST RTTY ORSAT 12345 ", fs=AFS, af=1000)
    band += place(loop_to(DUR, rtty), 7_045_000 - 1000, n)
    cw = cw_encode("CQ CQ DE F1NSK F1NSK K", fs=AFS, af=1000, wpm=20)
    band += place(loop_to(DUR, cw), 7_030_000 - 1000, n)
    nav = sitorb_encode(navtex_message("ORSAT BANC D ESSAI NAVTEX 1234"), fs=AFS, af=1000)
    band += place(loop_to(DUR, nav), 7_060_000 - 1000, n)
    rng = np.random.default_rng(7)
    band += (rng.normal(0, 0.002, n) + 1j * rng.normal(0, 0.002, n))
    return band.astype(np.complex64)


def ft8_slot_audio():
    """15 s d'audio FT8 contenant deux messages (générés par ft8_lib)."""
    out = np.zeros(int(15 * AFS))
    for msg, f, a in (("CQ F1NSK JN03", 1200, 0.3), ("F6CTE F1NSK -12", 1750, 0.15)):
        p = "/tmp/_ft8_%d.wav" % f
        subprocess.run([str(NATIVE / "gen_ft8"), msg, p, str(f)], capture_output=True)
        w = wave.open(p)
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2") / 32768
        out[:len(x)] += a * x[:len(out)]
    return out


def main():
    band = build()
    ft8 = place(ft8_slot_audio(), 7_074_000, int(15 * FS)).astype(np.complex64)
    chunk = FS // 20
    t0 = time.time()
    k = 0
    out = sys.stdout.buffer
    while True:
        i = (k * chunk) % len(band)
        blk = band[i:i + chunk].copy()
        # FT8 calé sur l'horloge UTC : début de créneau toutes les 15 s (+0,5 s)
        now = t0 + k * chunk / FS
        pos = int(((now % 15) - 0.5) * FS)
        if 0 <= pos < len(ft8):
            seg = ft8[pos:pos + chunk]
            blk[:len(seg)] += seg
        out.write(np.column_stack([blk.real, blk.imag]).astype(np.float32).tobytes())
        k += 1
        delay = t0 + k * chunk / FS - time.time()
        if delay > 0:
            time.sleep(delay)


if __name__ == "__main__":
    main()
