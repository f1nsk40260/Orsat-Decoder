# Orsat-Decoder et la base Artemis

[Artemis](https://github.com/AresValley/Artemis) est un catalogue de signaux radio (583 fiches issues de
sigidwiki.com, dont 543 avec un enregistrement audio de 8 à 60 s). Artemis n'identifie rien tout seul :
on y filtre à la main par fréquence, largeur, modulation et ACF. Orsat-Decoder s'en sert de trois façons.

## 1. Banc d'essai sur signaux réels

`tests/artemis_bench.py` fait passer chaque décodeur d'Orsat-Decoder sur l'enregistrement sigidwiki
de son mode (de vraies réceptions, avec fading, filtres BLU et parasites).

| Mode | Résultat sur l'enregistrement sigidwiki |
|---|---|
| PSK31 | texte parfait |
| RTTY 45 bd / 170 Hz | texte parfait |
| Navtex / SITOR-B | texte parfait (« ZCZC CQ DE OST QTC LIST… ») |
| CW | indicatif lisible (HA8A) |
| FT4 | `IU7GTP N6LZ R-15` |
| FT8 | rien : ft8_lib ne trouve aucun message dans cet extrait de 13 s (cause non établie) |
| MFSK16, DominoEX 11, THOR 11 | texte parfait |
| Olivia 16/500, Contestia 8/250 | texte parfait |
| MT63-1000 (entrelacement long) | texte parfait |
| SSTV | Robot 36, mire BBC impeccable |
| Fax météo | carte d'Europe nette à 120 l/min |
| Feld Hell | texte parfaitement lisible |

## 2. Identification automatique (`orsatdec/signal_id.py`)

```
orsat-decoder --identify enregistrement.wav [FREQ_kHz]
```

1. **Mesures** sur quelques secondes d'audio : zone occupée (99 % de la puissance), tonalités FSK
   (histogramme de la fréquence instantanée, peigne régulier), vitesse de manipulation (raie de
   l'enveloppe ou des sauts de fréquence, ramenée au fondamental), enveloppe constante ou non,
   ordre PSK (raie de z^M), période d'ACF, part de silences.
2. **Comparaison** à la base `orsatdec/data/sigid.json` (257 signaux qui tiennent dans l'audio BLU) :
   paramètres déclarés, ACF, plage de fréquences, et surtout **l'empreinte** mesurée sur l'enregistrement
   de référence de chaque signal. Pour les modes décodables s'ajoutent 40 empreintes de sous-modes
   générés (PSK63, RTTY 50/450, Olivia 32/1000…), car sigidwiki n'a qu'un enregistrement par famille.
3. **Confirmation** : les décodeurs des meilleurs candidats décodables tournent sur l'audio ; un texte
   lisible fait passer le mode en tête.

Taux mesurés :

| Test | 1er | 3 premiers | 10 premiers |
|---|---|---|---|
| 236 enregistrements sigidwiki, paramètres déclarés seuls | 2 % | 4 % | 12 % |
| idem avec les empreintes (1re moitié = référence, 2e = question) | 58 % | 63 % | 80 % |
| idem en donnant la fréquence d'écoute | 64 % | 69 % | 86 % |

La ligne « empreintes » est optimiste (référence et question viennent de la même émission).
Test indépendant (`tests/test_sigid.py`) : 40 sous-modes générés, autre texte, autre fréquence audio,
à +6 et 0 dB dans 2500 Hz : **69 sur 80 identifiés et décodés** après confirmation. Les échecs restants
sont à 0 dB : RTTY, Navtex, MFSK32/64, Olivia 64/2000, MT63-2000, Feld Hell.

Ce que l'identification ne sait pas faire : séparer deux modes de même modulation sans les décoder
(Olivia et Contestia ; DominoEX et THOR), ni reconnaître sûrement un mode dont la base n'a qu'un extrait
de quelques secondes.

## 3. Nouveaux décodeurs suggérés par la base

Les 249 signaux HF de moins de 3,5 kHz de la base, triés par intérêt et faisabilité.
Spécification publique et contenu en clair, du plus simple au plus long :

| Signal (fiches Artemis) | Pourquoi | Effort |
|---|---|---|
| **Signaux horaires** : DCF77, MSF, TDF, CHU, WWVB, JJY, RWM, BPC | heure décodée, très parlant avec le RX888 en ondes longues ; codes publics | faible |
| **Packet AX.25 300 bd** (PACKET), APRS HF | trames lisibles, indicatifs ; démodulateur FSK déjà là | faible |
| **ASCII FSK**, Baudot 50-75 bd des agences de presse | variantes du RTTY existant | faible |
| **SITOR-A / AMTOR** | même code que Navtex, côté ARQ | faible |
| **DSC (ASN maritime)**, Selcal OACI, CCIR 493-4, DTMF | déjà au jalon 3 ; mesures OK (100 bd, 170 Hz) | moyen |
| **ALE 2G** (MIL-STD-188-141) | très fréquent en HF, adresses en clair ; 8 tonalités / 125 bd retrouvés par l'identification | moyen |
| **PACTOR I** (FEC et écoute ARQ) | trafic maritime et radioamateur | moyen |
| **WSPR, JT65, JT9, FST4/FST4W, JS8** | signaux faibles amateurs ; JS8 est très proche de FT8 (ft8_lib adaptable) | moyen |
| **FSQ, THROB, DominoF** | modes fldigi qui complètent le jalon 2 | moyen |
| **HFDL** | positions d'avions (jalon 3) | élevé |
| **FreeDV** (voix numérique, libcodec2) | on entendrait la voix : rejoint la voix numérique du jalon 4 | moyen (bibliothèque) |
| **TOR militaires** : ARQ-E/E3, ARQ-M2/M4, FEC-A, ARQ6-90/98, SWED-ARQ, POL-ARQ, Coquelet, Piccolo | jalon 5, au cas par cas | moyen chacun |
| **STANAG 4285, MIL-STD-188-110** | démodulation publique (l'identification trouve déjà 2400 bd et l'ACF de 106,7 ms) ; contenu le plus souvent chiffré | élevé |

À écarter pour le décodage : CLOVER, PACTOR II-IV, VARA (propriétaires), les modems militaires et les
brouilleurs de voix (chiffrés). L'identification les reconnaît quand même et le dit, comme les radars
transhorizon, ionosondes et stations de chiffres de la base.

## Mise à jour de la base

```bash
git clone --depth 1 https://github.com/AresValley/Artemis-DB ~/Artemis-DB   # ≈ 300 Mo
python3 tools/make_sigid.py ~/Artemis-DB          # régénère orsatdec/data/sigid.json (≈ 70 Ko, ffmpeg requis)
python3 tests/artemis_bench.py ~/Artemis-DB       # décodeurs sur signaux réels
python3 tests/sigid_eval.py ~/Artemis-DB          # taux d'identification
python3 tests/test_sigid.py                        # identification sur mires générées
```

Licence : Artemis et Artemis-DB sont sous GPL-3 ; les fiches viennent de sigidwiki.com. `sigid.json`
n'en garde que les paramètres (titre, fréquences, largeur, modulation, ACF) et nos propres mesures, sans
texte ni média. Si Orsat-Decoder est un jour publié, il faudra le distribuer sous une licence compatible
GPL-3 et citer la source.
