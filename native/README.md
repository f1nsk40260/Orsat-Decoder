# Décodeurs natifs embarqués

| Dossier | Origine | Licence | Usage |
|---|---|---|---|
| `ft8_lib/` | https://github.com/kgoba/ft8_lib | MIT | FT8 / FT4 |
| `wspr/` | wsprd de K1JT et K9AN (WSJT-X), version de VA2GKA : https://github.com/Guenael/rtlsdr-wsprd | GPL-3 | WSPR |

`wspr/fftw3.h` remplace FFTW par kiss_fft (celui de ft8_lib) : aucune bibliothèque à installer.
`wspr/wspr_decode_iq.c` (Orsat-Decoder) lit un créneau en bande de base à 375 Hz et imprime les messages ;
avec `-f`, il applique l'algorithme de Fano de wsprd à des symboles souples (JT9 utilise le même code K=32).

`build.sh` les compile dans `bin/` (appelé par `install.sh`).
