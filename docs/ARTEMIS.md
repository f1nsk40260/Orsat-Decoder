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
| ASCII 110 bd | « CQ … DE F6CTE » |
| SITOR-A | « THIS IS DL6MAA TESTING… » |
| Packet 300 bd | trames TSTR1>TSTR2 |
| DSC | appel 002320001 ↔ 005030001 sur 8291,0 kHz et son accusé |
| Selcall CCIR 493 | « de 5678 à 1234 » |
| ALE 2G | appel complet avec message AMD |
| THROB1 | « THE QUICK BROWN FOX… » |
| WSPR | K3GAU EM89 33 et KL7EZ EM66 23 |
| DGPS 200 bd | station 466, corrections de type 9 (55 trames en 60 s) |
| PACTOR I 200 bd | télex « ZCZC CTR015 09H48 10/12/00 COPIE DE CTR0… » (CRC justes) |
| ACARS | ZK-OKM vol NZ0006, label H1, et 9 autres blocs |
| HFDL | squitters d'Auckland et un bloc ACARS montant vers VH-OQK (300 bits/s) |
| JS8 | « FOR GOD SO LOVED THE WORLD IN THIS WAY; HE GAVE HIS ONE AND ONLY SO… » |
| JT65A | « TEST12345 » (extrait de 30 s : symboles manquants traités en effacements) |
| JT9 | « TEST » |

## 2. Identification automatique (`orsatdec/signal_id.py`)

**Dans l'interface** : choisir **Identifier** (en tête de la colonne des modes), puis cliquer sur un
signal du waterfall. Le canal écoute 10 s (réglable), affiche les mesures et les candidats en une
seconde environ, puis fait tourner les décodeurs des candidats décodables. Si l'un d'eux sort un texte
lisible, le bon canal s'ouvre tout seul à la bonne fréquence (réglage « Mode trouvé : Ouvrir ») ou
reste proposé avec un bouton (« Proposer »). Pour un mode lent (Olivia 8/250, MFSK8…) reconnu mais
pas encore lisible, l'écoute est prolongée automatiquement jusqu'à 30 s. Chaque candidat renvoie à sa
fiche sigidwiki ; les candidats décodables ont un bouton « Ouvrir ». « Relancer » recommence l'écoute.

**En ligne de commande** :

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
   générés (PSK63, RTTY 50/450, Olivia 32/1000…), mesurées à trois niveaux de bruit, car sigidwiki
   n'a qu'un enregistrement par famille.
3. **Confirmation** : les décodeurs des meilleurs candidats décodables tournent sur l'audio ; un texte
   lisible fait passer le mode en tête.

Taux mesurés :

| Test | 1er | 3 premiers | 10 premiers |
|---|---|---|---|
| 244 enregistrements sigidwiki, paramètres déclarés seuls | 5 % | 8 % | 15 % |
| idem avec les empreintes (1re moitié = référence, 2e = question) | 58 % | 73 % | 81 % |
| idem en donnant la fréquence d'écoute | 66 % | 77 % | 85 % |

La ligne « empreintes » est optimiste (référence et question viennent de la même émission).
Test indépendant (`tests/test_sigid.py`) : 40 sous-modes générés, autre texte, autre fréquence audio,
à +6 et 0 dB dans 2500 Hz : **70 sur 80 identifiés et confirmés par décodage**. Les échecs : Olivia
64/2000, Contestia 32/1000 et MT63 à 0 dB (signal large à peine au-dessus du bruit), MFSK64, et deux
cas MFSK/DominoEX lents. La confirmation n'accepte qu'un texte contenant des mots du trafic radio ou des
indicatifs : sur du bruit, aucun des 292 réglages de décodeur essayés n'atteint le seuil.

Essai de bout en bout (`tests/e2e_ident.py`, navigateur sans écran, faux récepteur TCI) : RTTY météo
DWD 50 bd / 450 Hz inversé, Olivia 8/250 et MFSK16, cliqués dans le waterfall sans dire ce qu'ils
sont, s'ouvrent chacun dans le bon mode et décodent (12 s, 80 s avec écoute prolongée, 12 s).

Ce que l'identification ne sait pas faire : séparer deux modes de même modulation sans les décoder
(Olivia et Contestia ; DominoEX et THOR), ni reconnaître sûrement un mode dont la base n'a qu'un extrait
de quelques secondes.

## 3. Nouveaux décodeurs suggérés par la base

Les 249 signaux HF de moins de 3,5 kHz de la base, triés par intérêt et faisabilité. Faits depuis :
signaux horaires, Packet, ASCII, SITOR-A, DSC, Selcal, CCIR 493, DTMF, ALE 2G, PACTOR I, WSPR, JT65,
JT9, JS8, FSQ, THROB, HFDL, DGPS, ACARS. Restent :

| Signal (fiches Artemis) | Pourquoi | Effort |
|---|---|---|
| **FST4 / FST4W**, MSK144 | signaux faibles amateurs (LDPC proches de FT8) | moyen |
| **DominoF**, RSID de fldigi | compléments fldigi | moyen |
| **FreeDV** (voix numérique, libcodec2) | on entendrait la voix : rejoint la voix numérique du jalon 4 | moyen (bibliothèque) |
| **TOR militaires** : ARQ-E/E3, ARQ-M2/M4, FEC-A, ARQ6-90/98, SWED-ARQ, POL-ARQ, Coquelet, Piccolo | jalon 5, au cas par cas ; trafic le plus souvent chiffré | moyen chacun |
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
python3 tests/e2e_ident.py                         # bout en bout dans l'interface (playwright)
```

Licence : Artemis et Artemis-DB sont sous GPL-3 ; les fiches viennent de sigidwiki.com. `sigid.json`
n'en garde que les paramètres (titre, fréquences, largeur, modulation, ACF) et nos propres mesures, sans
texte ni média. Si Orsat-Decoder est un jour publié, il faudra le distribuer sous une licence compatible
GPL-3 et citer la source.
