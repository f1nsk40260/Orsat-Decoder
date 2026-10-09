"""Faux serveurs KiwiSDR, OpenWebRX et UberSDR pour les essais, sur la même bande synthétique que fake_tci.py
(PSK31, RTTY et CW vers 7070 kHz).

    python tests/fake_websdr.py kiwi [port]     (défaut 8073)
    python tests/fake_websdr.py owrx [port]     (défaut 8074)
    python tests/fake_websdr.py uber [port]     (défaut 8080)

FAKE_KIWI_PASS=… impose un mot de passe ; FAKE_KIWI_SLOTS=n limite le nombre de connexions (défaut 4).
Le faux Kiwi envoie l'audio non compressé si on le demande (SET compression=0), sinon en IMA ADPCM.
"""
import asyncio
import gzip
import json
import os
import struct
import sys
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_tci import Air, FS, STATIONS  # noqa: E402
from orsatdec.adpcm import encode  # noqa: E402
import zstandard as zstd  # noqa: E402

BLOCK = FS // 20


def fake_spectrum(f0, f1, n, rng):
    """Spectre de démonstration (dB) : bruit et une bosse par station présente dans [f0, f1]."""
    f = np.linspace(f0, f1, n, endpoint=False)
    db = -115 + rng.normal(0, 3, n)
    for fs, _ in STATIONS:
        db = np.maximum(db, -60 - ((f - fs) / max((f1 - f0) / n, 30)) ** 2)
    return db


# ===================================================================================== KiwiSDR
class Kiwi:
    def __init__(self):
        self.users = 0
        self.slots = int(os.environ.get("FAKE_KIWI_SLOTS", 4))
        self.password = os.environ.get("FAKE_KIWI_PASS", "")

    async def status(self, request):
        return web.Response(text=f"status=active\noffline=no\nname=Kiwi de démo\nusers={self.users}\n"
                                 f"users_max={self.slots}\nbands=0-30000000\ngps=(48.8, 2.3)\n")

    async def ws(self, request):
        which = request.match_info["which"]
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.users >= self.slots:
            await ws.send_bytes(b"MSG too_busy=%d" % self.slots)
            await ws.close()
            return ws
        self.users += 1
        try:
            await (self.snd(ws) if which == "SND" else self.wf(ws))
        finally:
            self.users -= 1
        return ws

    async def _auth(self, ws, txt):
        if txt.startswith("SET auth"):
            p = dict(kv.split("=", 1) for kv in txt.split()[2:] if "=" in kv)
            ok = not self.password or p.get("p") == self.password
            await ws.send_bytes(b"MSG badp=" + (b"0" if ok else b"1"))
            return ok
        return None

    async def snd(self, ws):
        st = {"dial": 7_069_000, "comp": True, "ok": False, "mod": "usb"}
        air = Air()

        async def stream():
            seq, adpcm = 0, (0, 0)
            while not ws.closed:
                if st["ok"]:
                    x = air.block(st["dial"], BLOCK)
                    pcm = np.clip(x * 32767, -32768, 32767).astype(np.int16)
                    if st["comp"]:
                        data, adpcm = encode(pcm, adpcm)
                        flags = 0x10
                    else:
                        data, flags = pcm.astype(">i2").tobytes(), 0
                    await ws.send_bytes(b"SND" + struct.pack("<BI", flags, seq) + struct.pack(">H", 600) + data)
                    seq += 1
                await asyncio.sleep(BLOCK / FS)

        task = asyncio.create_task(stream())
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            t = msg.data
            a = await self._auth(ws, t)
            if a is False:
                await ws.close()
                break
            if a:
                await ws.send_bytes(b"MSG audio_rate=12000")
                await ws.send_bytes(b"MSG sample_rate=12000.000")
            elif t.startswith("SET mod="):
                p = dict(kv.split("=", 1) for kv in t.split()[1:])
                st["mod"] = p["mod"]
                st["dial"] = int(round(float(p["freq"]) * 1000))
                st["ok"] = True
            elif t.startswith("SET compression="):
                st["comp"] = t.endswith("1")
        task.cancel()

    async def wf(self, ws):
        st = {"zoom": 0, "cf": 15_000_000.0, "ok": False}
        rng = np.random.default_rng(1)

        async def stream():
            seq = 0
            while not ws.closed:
                if st["ok"]:
                    span = 30e6 / 2 ** st["zoom"]
                    f0 = max(0.0, min(30e6 - span, st["cf"] - span / 2))
                    x_bin = int(round(f0 / 30e6 * (1024 << 14)))
                    db = fake_spectrum(f0, f0 + span, 1024, rng)
                    b = np.clip(db + 255, 0, 255).astype(np.uint8).tobytes()
                    await ws.send_bytes(b"W/F\x00" + struct.pack("<III", x_bin, st["zoom"], seq) + b)
                    seq += 1
                await asyncio.sleep(0.1)

        task = asyncio.create_task(stream())
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            t = msg.data
            a = await self._auth(ws, t)
            if a is False:
                await ws.close()
                break
            if a:
                await ws.send_bytes(b"MSG bandwidth=30000000 wf_setup")
            elif t.startswith("SET zoom="):
                p = dict(kv.split("=", 1) for kv in t.split()[1:])
                st["zoom"] = int(p["zoom"])
                st["cf"] = float(p.get("cf", 15000)) * 1000
                st["ok"] = True
        task.cancel()


# ===================================================================================== UberSDR
class Uber:
    """Protocole natif d'UberSDR : /api/description, POST /connection, /ws (pcm-zstd v3), /ws/user-spectrum
    (SPEC v2). Comme un vrai serveur : 2 UUID par adresse IP, un flux audio par UUID (le nouveau remplace)."""
    MAX_UUID_IP = int(os.environ.get("FAKE_UBER_UUIDS", 2))

    def __init__(self):
        self.known = {}          # uuid -> User-Agent annoncé par /connection
        self.audio = {}          # uuid -> ws audio en cours
        self.zc = zstd.ZstdCompressor()

    async def description(self, request):
        return web.json_response({"receiver": {"name": "UberSDR de démo"}, "description": "UberSDR de démo",
                                  "tuning_range": {"min_frequency": 10000, "max_frequency": 30000000,
                                                   "spectrum_span_hz": 30000000}, "public_uuid": "x", "version": "0.1.66"})

    async def status(self, request):            # imite OpenWebRX, comme le vrai
        return web.json_response({"receiver": {"name": "UberSDR de démo"}, "max_clients": 20, "version": "0.1.66",
                                  "sdrs": [{"name": "UberSDR", "type": "SDR", "profiles": []}]})

    async def connection(self, request):
        d = await request.json()
        uid = d.get("user_session_id", "")
        live = {u for u in self.known}
        if uid not in live and len(live) >= self.MAX_UUID_IP:
            return web.json_response({"allowed": False, "reason": f"maximum unique users per IP reached ({self.MAX_UUID_IP})"})
        self.known[uid] = request.headers.get("User-Agent", "")
        return web.json_response({"allowed": True, "bypassed": False, "allowed_iq_modes": [], "max_session_time": 0})

    def _check(self, request):
        uid = request.query.get("user_session_id", "")
        if uid not in self.known or self.known[uid] != request.headers.get("User-Agent", ""):
            return None
        return uid

    async def ws_audio(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        uid = self._check(request)
        if uid is None:
            await ws.send_str(json.dumps({"type": "error", "error": "Session not registered: call /connection first"}))
            await ws.close()
            return ws
        old = self.audio.get(uid)
        if old is not None and not old.closed:
            await old.close()                    # un flux audio par UUID : le nouveau remplace
        self.audio[uid] = ws
        q = request.query
        st = {"dial": int(q.get("frequency", 7_069_000)), "mode": q.get("mode", "usb")}
        air = Air()

        async def stream():
            first = True
            while not ws.closed:
                x = air.block(st["dial"], BLOCK)
                pcm = np.clip(x * 32767, -32768, 32767).astype(">i2").tobytes()
                if first:
                    hdr = struct.pack("<HBBQQIBfffI"[:-1], 0x5043, 3, 2, 0, 0, FS, 1, -60.0, -110.0, 0)
                    first = False
                else:
                    hdr = struct.pack("<HBQH", 0x504D, 3, 0, 0)
                await ws.send_bytes(self.zc.compress(hdr + pcm))
                await asyncio.sleep(BLOCK / FS)

        task = asyncio.create_task(stream())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                if m.get("type") == "tune":
                    st["dial"] = int(m.get("frequency", st["dial"]))
                    st["mode"] = m.get("mode", st["mode"])
        finally:
            task.cancel()
            if self.audio.get(uid) is ws:
                del self.audio[uid]
                self.known.pop(uid, None)        # plus aucun flux : l'UUID est libéré
        return ws

    async def ws_spectrum(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self._check(request) is None:
            await ws.send_bytes(gzip.compress(json.dumps({"type": "error", "error": "Session not registered"}).encode()))
            await ws.close()
            return ws
        st = {"cf": 15_000_000.0, "bbw": 30e6 / 1024}
        rng = np.random.default_rng(3)

        async def config():
            await ws.send_bytes(gzip.compress(json.dumps({"type": "config", "centerFreq": int(st["cf"]), "binCount": 1024,
                                                          "binBandwidth": st["bbw"], "totalBandwidth": st["bbw"] * 1024}).encode()))

        async def stream():
            seq, prev = 0, None
            while not ws.closed:
                span = st["bbw"] * 1024
                db = fake_spectrum(st["cf"] - span / 2, st["cf"] + span / 2, 1024, rng)
                ref, step = -16000, 60                       # dB = (ref + code × step) / 100
                codes = np.clip(np.round((db * 100 - ref) / step), 0, 255).astype(np.uint8)
                head = b"SPEC" + struct.pack("<BBHQQ", 2, 0, seq & 0xFFFF, 0, int(st["cf"]))
                if prev is None or seq % 10 == 0:
                    body = struct.pack("<hB", ref, step) + codes.tobytes()
                    head = head[:5] + b"\x05" + head[6:]
                else:
                    ch = codes != prev
                    body = np.packbits(ch, bitorder="little").tobytes() + codes[ch].tobytes()
                    head = head[:5] + b"\x06" + head[6:]
                prev = codes
                await ws.send_bytes(head + body)
                seq += 1
                await asyncio.sleep(0.1)

        await config()
        task = asyncio.create_task(stream())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                if m.get("type") in ("zoom", "pan"):
                    if m.get("frequency"):
                        st["cf"] = float(m["frequency"])
                    if m.get("binBandwidth"):
                        st["bbw"] = max(1.0, float(m["binBandwidth"]))
                    await config()
        finally:
            task.cancel()
        return ws


# ===================================================================================== OpenWebRX
PROFILES = {"rtl|40m": ("40 m", 7_070_000, 48_000), "rtl|20m": ("20 m", 14_070_000, 48_000)}


class Owrx:
    def __init__(self):
        self.profile = "rtl|40m"
        self.clients = set()

    def config(self):
        name, cf, sr = PROFILES[self.profile]
        sdr, prof = self.profile.split("|")
        return {"type": "config", "value": {"center_freq": cf, "samp_rate": sr, "fft_size": 2048,
                                            "audio_compression": "adpcm", "fft_compression": "adpcm",
                                            "sdr_id": sdr, "profile_id": prof, "start_mod": "usb",
                                            "start_offset_freq": 0, "max_clients": 20}}

    async def status(self, request):
        return web.json_response({"receiver": {"name": "OpenWebRX de démo", "admin": "", "gps": {}},
                                  "max_clients": 20, "version": "v1.2.2",
                                  "sdrs": [{"name": "RTL", "type": "rtl_sdr", "profiles": list(PROFILES)}]})

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        st = {"offset": 0, "mod": "usb", "run": False}
        air = Air()
        self.clients.add(ws)

        async def stream():
            rng = np.random.default_rng(2)
            adpcm, buf, k = (0, 0), b"", 0
            while not ws.closed:
                name, cf, sr = PROFILES[self.profile]
                k += 1
                if k % 2 == 0:                       # FFT 10 fois par seconde
                    db = fake_spectrum(cf - sr / 2, cf + sr / 2, 2048, rng)
                    v = np.concatenate([np.zeros(10), db * 100]).astype(np.int16)
                    data, _ = encode(v, (0, 0))
                    await ws.send_bytes(b"\x01" + data)
                if st["run"]:
                    x = air.block(cf + st["offset"], BLOCK)
                    pcm = np.clip(x * 32767, -32768, 32767).astype(np.int16)
                    # trames : SYNC, index, prédicteur, puis 1000 octets (2000 échantillons)
                    for i in range(0, len(pcm), 2000):
                        hdr = b"SYNC" + struct.pack("<hh", *adpcm)
                        enc, adpcm = encode(pcm[i:i + 2000], adpcm)
                        buf += hdr + enc
                    await ws.send_bytes(b"\x02" + buf[:700])     # découpage arbitraire des paquets
                    buf = buf[700:]
                await asyncio.sleep(BLOCK / FS)

        task = None
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            t = msg.data
            if t.startswith("SERVER DE CLIENT"):
                await ws.send_str("CLIENT DE SERVER server=openwebrx version=v1.2.2")
                continue
            m = json.loads(t)
            if m.get("type") == "connectionproperties":
                await ws.send_str(json.dumps(self.config()))
                await ws.send_str(json.dumps({"type": "profiles", "value": [{"id": k, "name": v[0]} for k, v in PROFILES.items()]}))
                task = task or asyncio.create_task(stream())
            elif m.get("type") == "dspcontrol":
                if m.get("action") == "start":
                    st["run"] = True
                p = m.get("params") or {}
                if "offset_freq" in p:
                    st["offset"] = int(p["offset_freq"])
                if "mod" in p:
                    st["mod"] = p["mod"]
            elif m.get("type") == "selectprofile":
                prof = (m.get("params") or {}).get("profile")
                if prof in PROFILES and prof != self.profile:
                    self.profile = prof
                    for c in list(self.clients):
                        if not c.closed:
                            await c.send_str(json.dumps(self.config()))
        if task:
            task.cancel()
        self.clients.discard(ws)
        return ws


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "kiwi"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else {"kiwi": 8073, "uber": 8080}.get(kind, 8074)
    app = web.Application()
    if kind == "uber":
        u = Uber()
        app.router.add_get("/api/description", u.description)
        app.router.add_get("/status.json", u.status)
        app.router.add_post("/connection", u.connection)
        app.router.add_get("/ws", u.ws_audio)
        app.router.add_get("/ws/user-spectrum", u.ws_spectrum)
        app.router.add_get("/v2/", lambda r: web.Response(text="<html><title>UberSDR</title></html>", content_type="text/html"))
    elif kind == "kiwi":
        k = Kiwi()
        app.router.add_get("/status", k.status)
        app.router.add_get("/{ts}/{which:SND|W/F}", k.ws)
    else:
        o = Owrx()
        app.router.add_get("/status.json", o.status)
        app.router.add_get("/ws/", o.ws)
        app.router.add_get("/", lambda r: web.Response(text="<html><title>OpenWebRX</title></html>", content_type="text/html"))
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
