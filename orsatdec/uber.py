"""Client du protocole natif des récepteurs UberSDR (ka9q-radio + RX888, 10 kHz à 30 MHz ou plus).

- POST /connection : chaque identifiant d'utilisateur (UUID) doit être annoncé avant ses WebSocket ;
- UberAudio : WebSocket /ws?frequency=…&mode=usb&format=pcm-zstd&version=3 ; chaque message binaire est une
  trame zstd contenant un en-tête « PC » (complet, 37 octets) ou « PM » (minimal, 13 octets) puis du PCM
  16 bits gros-boutiste ; réaccord par {"type": "tune", …} ;
- UberWaterfall : WebSocket /ws/user-spectrum?mode=binary8&version=2 ; configuration en JSON gzippé,
  lignes « SPEC » (trames complètes 0x05 et différentielles 0x06) ; zoom par {"type": "zoom", …}.

Un serveur UberSDR n'accepte qu'un flux audio par UUID et, par défaut, deux UUID par adresse IP : le
waterfall partage l'UUID du premier canal, chaque canal suivant en prend un nouveau.

Mêmes interfaces que phantom.AudioChannel / phantom.Waterfall (start, close, retune, set_view, info).
"""
import asyncio
import gzip
import json
import logging
import struct
import uuid as uuidlib
from urllib.parse import urlencode, urlparse

import aiohttp
import numpy as np
import zstandard

log = logging.getLogger("orsat.uber")

UA = "Orsat-Decoder"
# démodulation Orsat -> (mode UberSDR, passe-bande en Hz autour de la fréquence envoyée)
MODES = {"USB": ("usb", 100, 3000), "CWN": ("usb", 600, 1400), "CW": ("usb", 100, 3000), "LSB": ("lsb", -3000, -100),
         "AM": ("am", -5000, 5000), "FM": ("nfm", -5500, 5500)}


def base_url(server):
    """Adresse de base (schéma + hôte + port) : l'API d'UberSDR est à la racine, même si l'adresse donnée
    est celle d'une page de l'interface (…/v2/)."""
    u = urlparse(server if "://" in server else "http://" + server)
    scheme = "https" if u.scheme in ("https", "wss") else "http"
    return f"{scheme}://{u.netloc}"


def ws_url(server, path, **q):
    b = base_url(server)
    return ("wss" + b[5:] if b.startswith("https") else "ws" + b[4:]) + path + "?" + urlencode(q)


async def connection_check(session, server, uid, password=""):
    """Annonce un UUID au serveur. -> (autorisé, raison, réponse)."""
    body = {"user_session_id": uid}
    if password:
        body["password"] = password
    try:
        async with session.post(base_url(server) + "/connection", json=body, headers={"User-Agent": UA},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
            d = await r.json(content_type=None)
    except Exception as e:                       # serveur muet : la WebSocket donnera la vraie erreur
        log.debug("uber /connection %s : %s", server, e)
        return True, None, {}
    return bool(d.get("allowed", True)), d.get("reason"), d


def new_uuid():
    return str(uuidlib.uuid4())


def parse_pcm(raw):
    """Message binaire décompressé -> (en-tête dict ou None, octets PCM)."""
    if len(raw) < 13:
        return None, b""
    magic, ver = struct.unpack_from("<HB", raw, 0)
    if magic == 0x5043:                          # « PC » : en-tête complet
        size = 29 if ver == 1 else 37
        if len(raw) < size:
            return None, b""
        fs, ch = struct.unpack_from("<IB", raw, 20)
        hdr = {"fs": fs, "channels": ch}
        if ver >= 2:
            hdr["power"], hdr["noise"] = struct.unpack_from("<ff", raw, 25)
        return hdr, raw[size:]
    if magic == 0x504D:                          # « PM » : en-tête minimal (mêmes réglages)
        return {}, raw[13:]
    return None, b""


class _UberConn:
    path = "/ws"

    def __init__(self, server, uid, password="", session=None, on_state=None):
        self.server, self.uid, self.password = server, uid, password or ""
        self.on_state = on_state or (lambda s, info=None: None)
        self.ws = None
        self.info = None
        self._session = session
        self._own_session = session is None
        self._task = None
        self._closing = False
        self._busy = False

    def start(self):
        self._task = asyncio.ensure_future(self._run())

    async def close(self):
        self._closing = True
        if self.ws is not None and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        if self._own_session and self._session:
            await self._session.close()

    async def send(self, obj):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(obj))

    async def _keepalive(self):
        while self.ws is not None and not self.ws.closed:
            await asyncio.sleep(10)
            try:
                await self.send({"type": "ping"})
            except Exception:
                return

    def _url(self):
        raise NotImplementedError

    async def _run(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        delay = 1.0
        while not self._closing:
            ka = None
            try:
                ok, why, _ = await connection_check(self._session, self.server, self.uid, self.password)
                if not ok:
                    self._busy = True
                    raise RuntimeError(why or "connexion refusée par le serveur")
                async with self._session.ws_connect(self._url(), heartbeat=None, max_msg_size=0,
                                                    headers={"User-Agent": UA}) as ws:
                    self.ws = ws
                    self._busy = False
                    await self._opened()
                    ka = asyncio.ensure_future(self._keepalive())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            await self._binary(msg.data)
                        elif msg.type == aiohttp.WSMsgType.TEXT:
                            await self._text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                    delay = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.info("uber %s %s : %s", self.server, self.path, e)
                self.on_state("error", {"error": str(e) or e.__class__.__name__})
            finally:
                if ka:
                    ka.cancel()
            if self._closing:
                return
            self.on_state("reconnecting")
            await asyncio.sleep(30 if self._busy else delay)       # serveur plein : on ne le harcèle pas
            delay = min(delay * 2, 15)

    async def _text(self, data):
        try:
            m = json.loads(data)
        except ValueError:
            return
        await self._json(m)

    async def _json(self, m):
        if m.get("type") == "error":
            why = m.get("error") or m.get("message") or "erreur du serveur"
            self._busy = m.get("status") == 429 or "maximum" in str(why).lower()
            self.on_state("error", {"error": why})
            if self.ws is not None:
                await self.ws.close()

    async def _opened(self):
        pass

    async def _binary(self, data):
        pass


class UberAudio(_UberConn):
    """Flux audio d'un canal. on_pcm(np.float32[], fs) pour chaque bloc."""
    path = "/ws"

    def __init__(self, server, freq, mode="USB", uid=None, password="", on_pcm=None, on_state=None, session=None):
        super().__init__(server, uid or new_uuid(), password, session, on_state)
        self.freq, self.mode = float(freq), mode.upper()
        self.on_pcm = on_pcm or (lambda x, fs: None)
        self.fs = None
        self.channels = 1
        self.smeter = None
        self.packets = 0
        self._dctx = zstandard.ZstdDecompressor()

    def _url(self):
        mod, lo, hi = MODES.get(self.mode, MODES["USB"])
        q = {"frequency": int(round(self.freq)), "mode": mod, "format": "pcm-zstd", "version": 3,
             "user_session_id": self.uid, "bandwidthLow": lo, "bandwidthHigh": hi}
        if self.password:
            q["password"] = self.password
        return ws_url(self.server, "/ws", **q)

    async def retune(self, freq=None, mode=None):
        if freq is not None:
            self.freq = float(freq)
        if mode is not None:
            self.mode = mode.upper()
        mod, lo, hi = MODES.get(self.mode, MODES["USB"])
        await self.send({"type": "tune", "frequency": int(round(self.freq)), "mode": mod,
                         "bandwidthLow": lo, "bandwidthHigh": hi})

    async def _binary(self, data):
        try:
            raw = self._dctx.decompressobj().decompress(data)
        except zstandard.ZstdError:
            return                                # trame Opus ou inconnue : ignorée
        hdr, pcm = parse_pcm(raw)
        if hdr is None:
            return
        if hdr.get("fs"):
            if self.fs != hdr["fs"]:
                self.fs = float(hdr["fs"])
                self.info = {"sample_rate": self.fs}
                self.on_state("connected", self.info)
            self.channels = max(1, hdr.get("channels") or 1)
            if hdr.get("power") is not None and hdr["power"] > -900:
                self.smeter = hdr["power"]
        if not self.fs or not pcm:
            return
        x = np.frombuffer(pcm[:len(pcm) // 2 * 2], ">i2")
        if self.channels == 2:                    # IQ stéréo : voie I
            x = x[::2]
        self.packets += 1
        self.on_pcm(x.astype(np.float32) / 32768.0, int(round(self.fs)))


class UberWaterfall(_UberConn):
    """Waterfall. on_line({freq0, freq1, bins, raw}) ; info = {basefreq, total_bandwidth, ...}."""
    path = "/ws/user-spectrum"

    def __init__(self, server, uid=None, password="", on_line=None, on_info=None, on_state=None, session=None,
                 fmin=10e3, fmax=30e6, name=None):
        super().__init__(server, uid or new_uuid(), password, session, on_state)
        self.on_line = on_line or (lambda d: None)
        self.on_info = on_info or (lambda i: None)
        self.fmin, self.fmax, self.name = float(fmin), float(fmax), name
        self.view = None
        self.center = None
        self.bin_bw = None
        self.bin_count = 1024
        self.codes = None
        self.scale = None
        self._ready = False

    def _url(self):
        q = {"user_session_id": self.uid, "mode": "binary8", "version": 2}
        if self.password:
            q["password"] = self.password
        return ws_url(self.server, "/ws/user-spectrum", **q)

    async def set_view(self, f0, f1):
        self.view = (f0, f1)
        if not self._ready:
            return
        span = max(1000.0, f1 - f0)
        bins = self.bin_count or 1024
        half = min(span, self.fmax - self.fmin) / 2
        cf = min(max((f0 + f1) / 2, self.fmin + half), self.fmax - half)
        await self.send({"type": "zoom", "frequency": int(round(cf)), "binBandwidth": span / bins})

    async def _opened(self):
        self._ready = False
        self.codes = self.scale = None

    async def _binary(self, data):
        if data[:4] != b"SPEC":
            if data[:2] == b"\x1f\x8b":               # JSON gzippé (configuration, erreurs)
                try:
                    await self._json(json.loads(gzip.decompress(data)))
                except (OSError, ValueError):
                    pass
            return
        if len(data) < 22:
            return
        ver, flags = data[4], data[5]
        hl, ts = (24, 8) if ver == 2 else (22, 6)
        if len(data) < hl:
            return
        freq = struct.unpack_from("<Q", data, ts + 8)[0]
        body = data[hl:]
        if not self._apply(flags, body):
            return
        if self.scale is not None:
            ref, step = self.scale
            db = (ref + self.codes.astype(np.float32) * step) / 100.0
        else:
            db = self.codes.astype(np.float32) - 256.0
        if freq:
            self.center = float(freq)
        if self.center is None or not self.bin_bw:
            return
        span = self.bin_bw * len(db)
        v = np.clip((db + 150.0) * 2.0, 0, 255).astype(np.uint8)
        self.on_line({"freq0": self.center - span / 2, "freq1": self.center + span / 2, "bins": v, "raw": True})

    def _apply(self, flags, body):
        if flags == 0x05:                             # v2 complète : [ref i16][pas u8][codes]
            if len(body) < 3 or body[2] == 0:
                return False
            self.scale = (struct.unpack_from("<h", body, 0)[0], body[2])
            self.codes = np.frombuffer(body[3:], np.uint8).copy()
            return True
        if flags == 0x06:                             # v2 différentielle : [masque][valeurs]
            if self.codes is None or self.scale is None:
                return False
            n = len(self.codes)
            ml = (n + 7) >> 3
            if len(body) < ml:
                return False
            mask = np.unpackbits(np.frombuffer(body[:ml], np.uint8), bitorder="little")[:n].astype(bool)
            vals = np.frombuffer(body[ml:], np.uint8)
            if len(vals) != int(np.count_nonzero(np.unpackbits(np.frombuffer(body[:ml], np.uint8)))):
                return False
            k = int(mask.sum())
            if len(vals) < k:
                return False
            self.codes[mask] = vals[:k]
            return True
        if flags == 0x03:                             # v1 complète
            self.scale = None
            self.codes = np.frombuffer(body, np.uint8).copy()
            return True
        if flags == 0x04:                             # v1 différentielle : [n u16][(indice u16, valeur u8)…]
            if self.codes is None or len(body) < 2:
                return False
            cnt = struct.unpack_from("<H", body, 0)[0]
            for i in range(cnt):
                o = 2 + 3 * i
                if o + 3 > len(body):
                    return False
                idx, val = struct.unpack_from("<HB", body, o)
                if idx < len(self.codes):
                    self.codes[idx] = val
            return True
        return False

    async def _json(self, m):
        if m.get("type") == "config":
            self.center = float(m.get("centerFreq") or self.center or 0)
            self.bin_bw = float(m.get("binBandwidth") or self.bin_bw or 0)
            self.bin_count = int(m.get("binCount") or self.bin_count)
            first = not self._ready
            self._ready = True
            if first:
                self.info = {"basefreq": self.fmin, "total_bandwidth": self.fmax - self.fmin,
                             "fft_result_size": int(self.bin_count * (self.fmax - self.fmin) / max(self.bin_bw, 1)),
                             "name": self.name}
                self.on_info(self.info)
                self.on_state("connected", self.info)
                if self.view:
                    await self.set_view(*self.view)
            return
        await super()._json(m)
