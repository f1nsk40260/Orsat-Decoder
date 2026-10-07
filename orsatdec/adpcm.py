"""Décodeur IMA ADPCM (4 bits par échantillon, quartet de poids faible d'abord).

- decode() : flux continu (KiwiSDR compressé, lignes de waterfall OpenWebRX) ;
- decode_sync() : flux audio OpenWebRX, ponctué toutes les 1000 octets d'un mot « SYNC » suivi de
  l'index de pas et du prédicteur (deux int16 petit-boutistes), qui permet de se recaler.
"""
import numpy as np

INDEX = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88,
         97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658,
         724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660,
         4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
         18500, 20350, 22385, 24623, 27086, 29794, 32767)
SYNC = b"SYNC"


class ImaAdpcm:
    def __init__(self):
        self.reset()

    def reset(self):
        self.index = 0
        self.pred = 0
        self.step = 0
        self.synced = 0
        self.pending = b""

    def _nibble(self, n):
        self.index = min(max(self.index + INDEX[n], 0), 88)
        d = self.step >> 3
        if n & 1:
            d += self.step >> 2
        if n & 2:
            d += self.step >> 1
        if n & 4:
            d += self.step
        if n & 8:
            d = -d
        self.pred = min(max(self.pred + d, -32768), 32767)
        self.step = STEPS[self.index]
        return self.pred

    def decode(self, data):
        out = np.empty(len(data) * 2, np.int16)
        k = 0
        for b in data:
            out[k] = self._nibble(b & 0x0F)
            out[k + 1] = self._nibble(b >> 4)
            k += 2
        return out

    def decode_sync(self, data):
        """Flux audio OpenWebRX : tout ce qui se trouve entre l'en-tête d'un « SYNC » et le « SYNC »
        suivant est de l'audio. On ne compte pas les octets : le décodage reste juste même si
        l'intervalle entre deux SYNC change d'une version de serveur à l'autre."""
        buf = self.pending + bytes(data)
        self.pending = b""
        out = []
        i, n = 0, len(buf)
        while i < n:
            if not self.synced:
                j = buf.find(SYNC, i)
                if j < 0 or j + 8 > n:                     # pas (encore) d'en-tête complet
                    self.pending = buf[j:] if j >= 0 else buf[max(i, n - 3):]
                    break
                self.index = min(max(int.from_bytes(buf[j + 4:j + 6], "little", signed=True), 0), 88)
                self.pred = int.from_bytes(buf[j + 6:j + 8], "little", signed=True)
                self.step = STEPS[self.index]
                self.synced = 1
                i = j + 8
            else:
                j = buf.find(SYNC, i)
                end = j if j >= 0 else max(i, n - 3)       # les 3 derniers octets peuvent commencer un SYNC
                for b in buf[i:end]:
                    out.append(self._nibble(b & 0x0F))
                    out.append(self._nibble(b >> 4))
                if j >= 0:
                    self.synced = 0
                    i = j
                else:
                    self.pending = buf[end:]
                    break
        return np.array(out, np.int16)


def encode(x, state=None):
    """Codeur IMA ADPCM (pour les faux serveurs de test). x : int16. -> (octets, état)."""
    index, pred = state or (0, 0)
    out = bytearray()
    nib = []
    step = STEPS[index]
    for s in np.asarray(x, np.int32):
        d = int(s) - pred
        n = 0
        if d < 0:
            n, d = 8, -d
        diff = step >> 3
        if d >= step:
            n |= 4; d -= step; diff += step
        if d >= step >> 1:
            n |= 2; d -= step >> 1; diff += step >> 1
        if d >= step >> 2:
            n |= 1; diff += step >> 2
        pred = min(max(pred - diff if n & 8 else pred + diff, -32768), 32767)
        index = min(max(index + INDEX[n], 0), 88)
        step = STEPS[index]
        nib.append(n)
    if len(nib) % 2:
        nib.append(0)
    for a, b in zip(nib[::2], nib[1::2]):
        out.append(a | (b << 4))
    return bytes(out), (index, pred)
