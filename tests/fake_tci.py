"""Faux serveur TCI pour les essais : se comporte comme un récepteur TCI vu du client.

Envoie l'audio de réception (float32, 12 kHz) d'une bande BLU synthétique qui dépend de la fréquence
du VFO, et accepte les commandes vfo / modulation / audio_start.

    python tests/fake_tci.py [port]          (défaut 50001)
"""
import asyncio
import os
import struct
import sys
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orsatdec.gen.encoders import psk_encode, rtty_encode, cw_encode  # noqa: E402

FS = 12000
# Signaux présents « sur l'air » (fréquence RF du signal)
AF0 = 1500.0     # les mires sont générées autour de 1500 Hz, puis déplacées à leur place
STATIONS = [
    (7_070_800, lambda: psk_encode("CQ CQ DE F1NSK F1NSK TCI TEST PSK31 ", fs=FS, af=AF0)),
    (7_071_500, lambda: rtty_encode("RYRYRY CQ DE F1NSK TCI RTTY TEST ", fs=FS, af=AF0)),
    (7_069_900, lambda: cw_encode("CQ CQ DE F1NSK TCI K", fs=FS, af=AF0, wpm=22)),
]

# Jeux de mires du jalon 2 : FAKE_TCI_SET=texte (cadran 14070 kHz) ou images (cadran 14230 kHz)
if os.environ.get("FAKE_TCI_SET") == "texte":
    from orsatdec.gen.mfsk import mfsk_encode
    from orsatdec.gen.olivia import olivia_encode
    from orsatdec.gen.images import hell_encode
    STATIONS = [
        (14_071_000, lambda: np.concatenate([np.zeros(FS * 8), mfsk_encode("CQ CQ DE F1NSK MFSK16 TEST ORSAT DECODER K ", fs=FS, af=AF0)])),
        (14_072_000, lambda: olivia_encode("CQ DE F1NSK OLIVIA 8/250 ORSAT ", fs=FS, af=AF0, tones=8, bw=250)),
        (14_072_700, lambda: hell_encode("CQ CQ DE F1NSK FELD HELL ", fs=FS, af=AF0)),
    ]
elif os.environ.get("FAKE_TCI_SET") == "images":
    from orsatdec.gen.images import sstv_encode, test_image, wefax_encode, chart_image
    STATIONS = [
        (14_231_900, lambda: sstv_encode(test_image(320, 240), mode="robot36", fs=FS, af=AF0)),
        (14_229_900, lambda: wefax_encode(chart_image(1809, 80), fs=FS, af=AF0, phasing=10)),
    ]


# Jeu « ident » (cadran 10 100 kHz) : signaux à identifier, sans dire lesquels
if os.environ.get("FAKE_TCI_SET") == "ident":
    from orsatdec.gen.mfsk import mfsk_encode
    from orsatdec.gen.olivia import olivia_encode
    STATIONS = [
        (10_100_800, lambda: rtty_encode("RYRYRY ZCZC DDK9 WEATHER REPORT GALE WARNING NORTH SEA NNNN " * 3,
                                        fs=FS, af=AF0, baud=50.0, shift=450.0, reverse=True)),
        (10_101_700, lambda: olivia_encode("CQ CQ DE F1NSK F1NSK OLIVIA 8/250 TEST PSE K ", fs=FS, af=AF0, tones=8, bw=250)),
        (10_102_400, lambda: np.concatenate([mfsk_encode("CQ CQ DE F1NSK MFSK16 TEST ORSAT DECODER K " * 2, fs=FS, af=AF0),
                                             np.zeros(FS * 2)])),
    ]
    DIAL0 = 10_100_000
elif os.environ.get("FAKE_TCI_SET") == "hfdl":
    # cadran 8942 kHz (HFDL Shannon) : squitter et positions de trois vols, deux fois chacun
    from orsatdec.gen import utility as U

    def _hfdl():
        fr = [(U.hfdl_spdu(7, 0b101), 0)]
        vols = [(31, "AF0123", (48.9, -12.0), (49.4, -15.5)), (44, "BA0178", (53.1, -20.0), (53.6, -24.0)),
                (52, "DL0045", (46.0, -30.0), (46.8, -26.5))]
        for k in range(2):
            for ac, vol, a, b in vols:
                lat, lon = (a, b)[k]
                fr.append((U.hfdl_perf_mpdu(7, ac, vol, lat, lon, 12 * 3600 + 600 * k + ac), 2))
        fr += [(U.hfdl_logon_mpdu(7, 60, 0x3C6545), 1), (U.hfdl_logon_mpdu(7, 61, 0x4CA1B2), 1)]
        return U.hfdl_encode(fr, af=AF0, foff=0.0, gap=0.4)
    STATIONS = [(8_943_440, _hfdl)]
    DIAL0 = 8_942_000
else:
    DIAL0 = 7_069_000


class Air:
    def __init__(self):
        self.bb = []
        for f, gen in STATIONS:
            x = gen()
            from scipy.signal import hilbert
            a = hilbert(np.concatenate([x, np.zeros(FS)]))
            self.bb.append((f, a))
        self.pos = 0

    def block(self, dial, n):
        t = (self.pos + np.arange(n)) / FS
        out = np.zeros(n, np.complex128)
        for f, a in self.bb:
            off = f - dial
            if 100 < off < 4000:
                idx = (self.pos + np.arange(n)) % len(a)
                out += a[idx] * np.exp(2j * np.pi * (off - AF0) * t)
        self.pos += n
        y = np.real(out) * 0.3 + np.random.default_rng(self.pos).normal(0, 0.01, n)
        return y.astype(np.float32)


async def handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    st = {"dial": DIAL0, "mode": "usb", "audio": False}
    air = Air()
    await ws.send_str("protocol:ExpertSDR3,2.0;device:Démo;receive_only:true;trx_count:1;channels_count:2;")
    await ws.send_str(f"vfo:0,0,{st['dial']};modulation:0,{st['mode']};ready;")

    async def stream():
        n = FS // 20
        while not ws.closed:
            if st["audio"]:
                x = air.block(st["dial"], n)
                hdr = struct.pack("<8I", 0, FS, 3, 0, 0, len(x), 1, 1) + bytes(32)
                await ws.send_bytes(hdr + x.tobytes())
            await asyncio.sleep(n / FS)

    task = asyncio.create_task(stream())
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        for cmd in msg.data.split(";"):
            name, _, args = cmd.strip().partition(":")
            a = args.split(",")
            if name == "audio_start":
                st["audio"] = True
            elif name == "audio_stop":
                st["audio"] = False
            elif name == "vfo" and len(a) >= 3:
                st["dial"] = int(float(a[2]))
                await ws.send_str(f"vfo:0,0,{st['dial']};")
            elif name == "modulation" and len(a) >= 2:
                st["mode"] = a[1]
                await ws.send_str(f"modulation:0,{st['mode']};")
    task.cancel()
    return ws


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 50001
    app = web.Application()
    app.router.add_get("/", handler)
    web.run_app(app, host="127.0.0.1", port=port, print=lambda *a: None)


if __name__ == "__main__":
    main()
