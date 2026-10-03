"""Client du protocole WebSocket de PhantomSDR-Plus / Orsat-SDR.

- AudioChannel : un flux /audio en PCM brut (int16), accordé sur une fréquence et une démodulation ;
  même méthode que le client interne autorun/audiotap.js du serveur.
- Waterfall : le flux /waterfall (zstd continu + CBOR), lignes d'octets sur la bande demandée.

Toutes les fréquences sont en Hz « affichées » (basefreq inclut un éventuel décalage LNB).
"""
import asyncio
import json
import logging
from urllib.parse import urlencode

import aiohttp
import cbor2
import numpy as np
import zstandard

log = logging.getLogger("orsat.phantom")

# Bande passante audio extraite autour de la fréquence d'accord, par démodulation (Hz)
SPANS = {"USB": (0, 3000), "LSB": (-3000, 0), "AM": (-5000, 5000), "FM": (-6000, 6000), "CW": (0, 3000)}


def ws_url(server, path, rx=None, tap=None):
    base = server.rstrip("/")
    if base.startswith("http://"):
        base = "ws://" + base[7:]
    elif base.startswith("https://"):
        base = "wss://" + base[8:]
    elif not base.startswith(("ws://", "wss://")):
        base = "ws://" + base
    q = {}
    if rx:
        q["rx"] = rx
    if tap:
        q["tap"] = tap
    return f"{base}{path}" + (("?" + urlencode(q)) if q else "")


class AudioChannel:
    """Flux audio d'un canal. on_pcm(np.float32[]) est appelé pour chaque paquet reçu."""

    def __init__(self, server, freq, mode="USB", rx=None, tap=None, on_pcm=None, on_state=None, session=None):
        self.server, self.freq, self.mode, self.rx, self.tap = server, float(freq), mode.upper(), rx, tap
        self.on_pcm = on_pcm or (lambda x: None)
        self.on_state = on_state or (lambda s, info=None: None)
        self.info = None
        self.sample_rate = None
        self.ws = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    def _bin(self, f):
        i = self.info
        return (f - i["basefreq"]) / i["total_bandwidth"] * i["fft_result_size"]

    async def retune(self, freq=None, mode=None):
        if freq is not None:
            self.freq = float(freq)
        if mode is not None:
            self.mode = mode.upper()
        await self._tune()

    async def _send(self, obj):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(obj))

    async def _tune(self):
        if not self.info:
            return
        lo, hi = SPANS.get(self.mode, (0, 3000))
        demod = "USB" if self.mode == "CW" else self.mode
        m = self._bin(self.freq)
        l, r = int(np.floor(self._bin(self.freq + lo))), int(np.ceil(self._bin(self.freq + hi)))
        await self._send({"cmd": "demodulation", "demodulation": demod})
        await self._send({"cmd": "window", "l": l, "m": m, "r": r})

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            try:
                url = ws_url(self.server, "/audio", self.rx, self.tap)
                async with self._session.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    got_info = False
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT and not got_info:
                            self.info = json.loads(msg.data)
                            self.sample_rate = self.info.get("audio_max_sps")
                            got_info = True
                            await self._send({"cmd": "set_codec", "codec": "pcm"})
                            await self._send({"cmd": "agc_enable", "enabled": True})
                            await self._tune()
                            # le serveur ignore une démodulation reçue dans ses 100 premières ms
                            await asyncio.sleep(0.3)
                            await self._tune()
                            self.on_state("connected", self.info)
                            delay = 1.0
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            try:
                                pkt = cbor2.loads(msg.data)
                            except Exception:
                                continue
                            if pkt.get("codec") != "pcm" or not pkt.get("data"):
                                continue
                            raw = pkt["data"]
                            n = len(raw) // 2
                            pcm = np.frombuffer(raw[:n * 2], "<i2").astype(np.float32) / 32768.0
                            self.on_pcm(pcm)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("audio %s : %s", self.server, e)
                self.on_state("error", {"error": str(e)})
            if self._closing:
                return
            self.on_state("reconnecting")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


class Waterfall:
    """Flux waterfall. on_line(dict) reçoit {freq0, freq1, bins: list[int]}."""

    def __init__(self, server, rx=None, on_line=None, on_info=None, session=None):
        self.server, self.rx = server, rx
        self.on_line = on_line or (lambda d: None)
        self.on_info = on_info or (lambda i: None)
        self.info = None
        self.ws = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False
        self.view = None            # (l, r) en bins de la FFT complète

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    def freq_of_bin(self, b):
        i = self.info
        return i["basefreq"] + b / i["fft_result_size"] * i["total_bandwidth"]

    def bin_of_freq(self, f):
        i = self.info
        return (f - i["basefreq"]) / i["total_bandwidth"] * i["fft_result_size"]

    async def set_view(self, f0, f1):
        """Demande au serveur la portion de bande [f0, f1] Hz (zoom)."""
        if not self.info:
            return
        n = self.info["fft_result_size"]
        l = int(max(0, min(n - 1, self.bin_of_freq(f0))))
        r = int(max(l + 1, min(n, self.bin_of_freq(f1))))
        self.view = (l, r)
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps({"cmd": "window", "l": l, "r": r}))

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            try:
                url = ws_url(self.server, "/waterfall", self.rx)
                async with self._session.ws_connect(url, heartbeat=20, max_msg_size=0) as ws:
                    self.ws = ws
                    dctx = zstandard.ZstdDecompressor().decompressobj()
                    buf = b""
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            if self.info is None or not buf:
                                try:
                                    self.info = json.loads(msg.data)
                                    self.on_info(self.info)
                                    if self.view:
                                        await ws.send_str(json.dumps({"cmd": "window", "l": self.view[0], "r": self.view[1]}))
                                except ValueError:
                                    pass
                        elif msg.type == aiohttp.WSMsgType.BINARY and self.info:
                            try:
                                buf += dctx.decompress(msg.data)
                            except zstandard.ZstdError:
                                dctx = zstandard.ZstdDecompressor().decompressobj()
                                buf = b""
                                continue
                            # un paquet zstd vidé (flush) contient exactement un objet CBOR
                            try:
                                pkt = cbor2.loads(buf)
                            except Exception:
                                continue
                            buf = b""
                            data = pkt.get("data")
                            if data is None:
                                continue
                            l, r = pkt.get("l", 0), pkt.get("r", 0)
                            bins = np.frombuffer(bytes(data), np.int8).astype(np.int16)
                            self.on_line({"freq0": self.freq_of_bin(l), "freq1": self.freq_of_bin(r),
                                          "bins": bins})
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("waterfall %s : %s", self.server, e)
            if self._closing:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)
