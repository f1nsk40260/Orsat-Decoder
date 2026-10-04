"""Diagnostic de connexion à un serveur PhantomSDR / Orsat-SDR :

    orsat-decoder --probe http://orsat.ddns.net:8080 7030

Se connecte comme un canal de décodage, affiche ce que le serveur annonce et envoie, mesure le
niveau audio pendant 12 s et enregistre ce qui a été reçu dans ~/orsat-probe.wav.
"""
import asyncio
import time
import wave
from pathlib import Path

import aiohttp
import numpy as np

from .phantom import AudioChannel


async def probe(url, freq=None, seconds=12):
    out = Path.home() / "orsat-probe.wav"
    print(f"Diagnostic Orsat-Decoder : {url}")
    blocks, events = [], []
    st = {"fs": None, "t0": None}

    def on_pcm(x, fs):
        st["fs"] = fs
        st["t0"] = st["t0"] or time.time()
        blocks.append(np.asarray(x, np.float32).copy())

    def on_state(s, info=None):
        events.append((round(time.time() - start, 1), s, info))

    start = time.time()
    async with aiohttp.ClientSession() as http:
        # fréquence par défaut : celle que le serveur propose
        if freq is None:
            freq = 7_030_000.0
        ch = AudioChannel(url, freq - 700, "USB", on_pcm=on_pcm, on_state=on_state, session=http)
        ch.start()
        await asyncio.sleep(seconds)
        info = ch.info or {}
        await ch.close()

    print()
    if info:
        print(f"  serveur      : bande {info.get('basefreq', 0) / 1e6:.3f} à "
              f"{(info.get('basefreq', 0) + info.get('total_bandwidth', 0)) / 1e6:.3f} MHz, "
              f"audio annoncé {info.get('audio_compression')} à {info.get('audio_max_sps')} Hz")
        if info.get("receivers"):
            print(f"  récepteurs   : {', '.join(str(r.get('id')) for r in info['receivers'])}")
    else:
        print("  serveur      : aucune réponse (adresse ou port ?)")
    print(f"  accord       : {freq / 1000:.3f} kHz (porteuse USB à {(freq - 700) / 1000:.3f} kHz)")
    print(f"  marqueur ?v= : {'oui (exigé par le serveur)' if ch.with_version else 'non nécessaire'}")
    print(f"  codec reçu   : {ch.codec or 'aucun'}   paquets audio : {ch.packets}")
    for t, s, i in events:
        if s in ("error",) or (s == "codec"):
            print(f"  à {t:5.1f} s    : {s} {i}")
    if ch.last_close and ch.last_close[0]:
        print(f"  fermeture    : code {ch.last_close[0]} {ch.last_close[1]}")
    if not blocks:
        print("\n  VERDICT : aucun audio reçu. Le serveur refuse le flux ou ne l'envoie pas (voir ci-dessus).")
        return 1
    x = np.concatenate(blocks)
    fs = st["fs"]
    dur = len(x) / fs
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x)))
    print(f"  audio reçu   : {dur:.1f} s à {fs} Hz ({len(x) / max(1e-3, time.time() - st['t0']):.0f} échantillons/s)")
    print(f"  niveau       : moyen {20 * np.log10(rms + 1e-12):.1f} dBFS, crête {20 * np.log10(peak + 1e-12):.1f} dBFS")
    sp = np.abs(np.fft.rfft(x[-min(len(x), 65536):] * np.hanning(min(len(x), 65536))))
    f = np.fft.rfftfreq(min(len(x), 65536), 1 / fs)
    k = np.argmax(sp[(f > 100) & (f < 3000)]) + np.argmax(f > 100)
    print(f"  pic audio    : {f[k]:.0f} Hz (soit {(freq - 700 + f[k]) / 1000:.3f} kHz)")
    w = wave.open(str(out), "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(fs))
    w.writeframes((np.clip(x / max(peak, 1e-6) * 0.8, -1, 1) * 32767).astype("<i2").tobytes())
    w.close()
    print(f"\n  enregistrement : {out}")
    if rms < 1e-5:
        print("  VERDICT : le flux arrive mais il est muet.")
        return 2
    print("  VERDICT : l'audio arrive correctement.")
    return 0
