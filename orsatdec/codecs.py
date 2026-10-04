"""Décodage des flux audio compressés envoyés par PhantomSDR-Plus : FLAC (flux continu découpé en
paquets) et Opus (un paquet par trame). Sortie : float32 mono, ±1."""
import ctypes
import ctypes.util
import logging
import threading

import numpy as np

log = logging.getLogger("orsat.codecs")


class FlacStream:
    """Décodeur FLAC en continu. feed(octets) ; les échantillons décodés arrivent par on_pcm(x, fs)."""

    def __init__(self, on_pcm):
        import pyflac
        self.on_pcm = on_pcm
        self._lock = threading.Lock()
        self._dec = pyflac.StreamDecoder(write_callback=self._write)

    def _write(self, data, sample_rate, num_channels, num_samples):
        x = np.asarray(data)
        if x.dtype.kind == "i":                    # entiers 16 bits -> ±1, avant tout mélange de voies
            x = x.astype(np.float32) / 32768.0
        if x.ndim == 2:
            x = x.mean(axis=1)
        self.on_pcm(np.ascontiguousarray(x, dtype=np.float32), int(sample_rate))

    def feed(self, data):
        with self._lock:
            self._dec.process(bytes(data))

    def close(self):
        try:
            self._dec.finish()
        except Exception:
            pass


class OpusStream:
    """Décodeur Opus minimal (libopus via ctypes)."""

    _lib = None

    @classmethod
    def available(cls):
        return cls._load() is not None

    @classmethod
    def _load(cls):
        if cls._lib is None:
            path = ctypes.util.find_library("opus")
            if not path:
                return None
            lib = ctypes.CDLL(path)
            lib.opus_decoder_create.restype = ctypes.c_void_p
            lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            lib.opus_decode_float.restype = ctypes.c_int
            lib.opus_decode_float.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                              ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_int]
            lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
            cls._lib = lib
        return cls._lib

    def __init__(self, on_pcm, sample_rate=12000, channels=1):
        lib = self._load()
        if lib is None:
            raise RuntimeError("libopus introuvable")
        # Opus ne décode qu'à 8, 12, 16, 24 ou 48 kHz
        self.fs = min((8000, 12000, 16000, 24000, 48000), key=lambda r: abs(r - sample_rate))
        self.ch = max(1, int(channels))
        err = ctypes.c_int()
        self._dec = lib.opus_decoder_create(self.fs, self.ch, ctypes.byref(err))
        if err.value != 0 or not self._dec:
            raise RuntimeError(f"opus_decoder_create : erreur {err.value}")
        self._buf = (ctypes.c_float * (5760 * self.ch))()
        self.on_pcm = on_pcm

    def feed(self, data):
        data = bytes(data)
        n = self._lib.opus_decode_float(self._dec, data, len(data), self._buf, 5760, 0)
        if n > 0:
            x = np.frombuffer(self._buf, np.float32, n * self.ch).copy()
            if self.ch > 1:
                x = x.reshape(-1, self.ch).mean(axis=1)
            self.on_pcm(x, self.fs)

    def close(self):
        if self._dec:
            self._lib.opus_decoder_destroy(self._dec)
            self._dec = None
