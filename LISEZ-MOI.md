# Orsat-Decoder

Décodeur multimode natif Linux. Un seul logiciel, sans Wine, qui se branche sur plusieurs types de sources.

## Sources

Le menu **Source**, en haut à gauche, choisit d'où vient le signal. Les sources se règlent dans les réglages (bouton en forme de roue).

| Source | Pour | Fonctionnement |
|---|---|---|
| **PhantomSDR / Orsat-SDR** | ton serveur ORSAT, Orsat-SDR, tout PhantomSDR-Plus | waterfall large bande du serveur ; chaque canal a son propre flux audio et son propre accord. Audio PCM, FLAC ou Opus. |
| **TCI** | AetherSDR (port 50001), ExpertSDR / SunSDR, Thetis… | audio et fréquence du récepteur sur une seule connexion ; le champ de fréquence en haut réaccorde le récepteur. |
| **Entrée audio** | carte son du transceiver, sortie de n'importe quel logiciel (son « Monitor »), entrées DAX d'AetherSDR | CAT rigctld facultatif (port 4532 : AetherSDR, rigctld, flrig) pour afficher les vraies fréquences et réaccorder. |

Avec TCI et l'entrée audio, tous les canaux partagent l'audio BLU du récepteur, et le waterfall montre cette bande audio. Quand on réaccorde le récepteur, chaque canal reste sur sa station. Sans CAT, les fréquences affichées sont les fréquences audio (Hz).

## Installation

Copiez cette ligne dans un terminal :

```bash
curl -fsSL https://raw.githubusercontent.com/f1nsk40260/Orsat-Decoder/main/get.sh | bash
```

C'est tout : ni git, ni compte GitHub. Seul le mot de passe de votre session Linux peut être demandé (sudo), une fois, s'il manque des paquets.

**Mise à jour** : la même ligne. La configuration et les canaux sont conservés.

L'installateur :
- installe Python et un compilateur C s'ils manquent (sudo demandé une fois) ;
- installe tout dans le dossier **`~/Orsat-Decoder`** de votre dossier personnel (créé s'il n'existe pas) ;
- y crée son environnement Python (`venv/`) ;
- compile les décodeurs natifs embarqués ;
- ajoute **Orsat-Decoder** au menu des applications ;
- termine par un autotest de chaque décodeur.

Contenu de `~/Orsat-Decoder` :

```
orsatdec/  web/  native/  tests/   le logiciel
venv/                              son environnement Python
config.json                        vos sources, canaux et réglages
orsat-decoder.log                  journal de la dernière session
images/                            fax et images SSTV reçus (PNG)
install.sh  get.sh  uninstall.sh   installation, mise à jour, désinstallation
```

Le lanceur `orsat-decoder` est dans `~/.local/bin`, l'entrée de menu dans `~/.local/share/applications`. Une installation d'une version précédente (dans `~/.local/share/orsat-decoder`) est reprise et nettoyée automatiquement.

## Utilisation

1. Choisissez la source en haut à gauche.
2. Choisissez un mode dans la colonne de gauche, puis **cliquez sur un signal** dans le waterfall : un canal s'ouvre. Chaque décodeur retrouve seul son signal dans environ ±100 Hz autour du clic.
3. Ou ouvrez directement une **fréquence connue** : Navtex 518 et 490 kHz, météo DWD, FT8 sur toutes les bandes… Avec TCI ou CAT, le récepteur est réaccordé automatiquement.

Dans le waterfall :
- **molette : accorde le canal actif**, par pas de 10 Hz (Maj : 1 Hz, Alt : 100 Hz) ;
- Ctrl+molette : zoom ;
- glisser : se déplacer dans la bande ;
- glisser un marqueur de canal : réaccorder ce canal ;
- si la résolution est trop faible pour viser (plus de 20 Hz par pixel), un premier clic zoome autour du signal, le second ouvre le canal.

Chaque carte de canal a un **mini-spectre** très résolu (environ 1 Hz par pixel), centré sur le décodeur. Le trait de couleur marque la fréquence demandée, le pointillé blanc l'endroit où le décodeur s'est calé. Cliquez sur le signal, ou tournez la molette sur le mini-spectre ou sur la fréquence, pour un accord précis. Les petits déplacements se font dans l'audio déjà reçu, sans coupure. La carte active (la dernière touchée) est entourée de sa couleur.

Dans une carte de canal : fréquence modifiable au clavier, paramètres du mode, pause, enregistrement du texte, et bouton **Écouter** pour entendre l'audio que reçoit ce décodeur. La ligne du bas affiche le niveau audio reçu (dBFS). Si rien n'arrive pendant 5 s, la carte passe en rouge « pas d'audio reçu » et indique la raison donnée par le serveur.

| Commande | Effet |
|---|---|
| `orsat-decoder` | lance l'application |
| `orsat-decoder --check` | autotest des décodeurs (sans serveur) |
| `orsat-decoder --lan` | interface accessible depuis le réseau local (`http://ip:8074/`) |
| `orsat-decoder --log` | journal de la dernière session |
| `orsat-decoder --identify fichier.wav [FREQ_kHz]` | identifie le signal d'un enregistrement (base Artemis / sigidwiki) et le confirme en le décodant |
| `orsat-decoder --probe URL FREQ_kHz` | diagnostic de connexion : ce que le serveur envoie, niveau, enregistrement de 12 s dans `~/orsat-probe.wav` |

Les serveurs Orsat-SDR récents refusent les clients qui ne déclarent pas leur version (`min_client_version`). Orsat-Decoder la déclare (`?v=2`) automatiquement quand le serveur l'exige.

## Modes

| Famille | Modes | Sensibilité mesurée (bruit dans 2500 Hz) |
|---|---|---|
| PSK | PSK31, 63, 125 | ≈ −11 / −8 / −5 dB |
| RTTY | 45 à 100 bd, shift réglable | ≈ −8 dB à 45 bd |
| CW | vitesse automatique 5-60 mpm | ≈ −8 dB à 20 mpm |
| Maritime | Navtex / SITOR-B | ≈ −8 dB |
| Signaux faibles | FT8, FT4 (ft8_lib, MIT) | ≈ −18 dB |
| MFSK | MFSK4 à 128, DominoEX (Micro à 88, FEC MultiPSK en option), THOR (Micro à 100) | MFSK16 −13 dB, THOR 11 −15 dB, DominoEX 11 −13 dB |
| Olivia | Olivia et Contestia, 4 à 64 tonalités, 125 à 2000 Hz | Olivia 32/1000 −14 dB, 8/250 −16 dB |
| MT63 | 500, 1000, 2000, entrelacement court ou long | MT63-1000 long −8 dB |
| Images | Fax météo (IOC 576/288, 60 à 240 l/min), SSTV (Martin, Scottie, Robot, PD, Wraase, code VIS automatique), Hellschreiber (Feld, Slow, X5, X9, FSK Hell, Hell 80) | |

Les modes MFSK, Olivia et MT63 suivent l'émetteur de fldigi bit pour bit (vérifié en compilant le code de fldigi). Sur du bruit seul, aucun ne doit rien imprimer : la squelch s'appuie sur le code correcteur de chaque mode. Revers de la médaille : un canal MFSK/THOR qu'on vient d'ouvrir reste muet quelques secondes (7 s en MFSK16), le temps de remplir son désentrelaceur, et MT63 affiche le texte avec une dizaine de secondes de retard.

**Images** : elles s'affichent dans la carte du canal au fil de la réception. Le fax démarre seul sur la tonalité de départ et se cale sur les lignes de phasage ; pris en cours de route, il démarre quand même et s'aligne sur le cadre de la carte. Le SSTV démarre sur le code VIS. Chaque image terminée est enregistrée en PNG dans `~/Orsat-Decoder/images/` ; le bouton Enregistrer télécharge la dernière. Le Hell s'affiche comme une bande, ligne après ligne, imprimée deux fois en hauteur comme sur un vrai téléscripteur Hell.

## Identification automatique

`orsat-decoder --identify` mesure le signal (largeur, tonalités, vitesse, PSK, ACF), le compare aux
257 signaux de bande audio de la base [Artemis](https://github.com/AresValley/Artemis) (sigidwiki),
puis fait tourner les décodeurs des meilleurs candidats : un texte lisible confirme le mode.
Détails, taux de réussite et nouveaux décodeurs suggérés par la base : [docs/ARTEMIS.md](docs/ARTEMIS.md).

## Limite de connexions

Chaque canal est un auditeur pour le serveur. Un PhantomSDR-Plus limite en général à **3 auditeurs par adresse IP** (`per_ip` dans `config.toml`).

Lancé **sur la machine du serveur**, Orsat-Decoder n'a pas cette limite. S'il trouve le fichier `.tap_token` d'Orsat-SDR (dans `~/Orsat-SDR`), ses canaux sont même comptés comme clients internes, comme le client autorun, et n'apparaissent pas parmi les auditeurs.

## Pour le développement

- `tests/selftest.py` : autotest rapide.
- `tests/harness.py` : banc de mesure de sensibilité.
- `tests/bandgen.py` : génère une bande HF synthétique en IQ pour alimenter un `spectrumserver` (driver `stdin`, `f32`, `iq`) et tester tout le logiciel sans antenne.
- `tests/artemis_bench.py`, `tests/sigid_eval.py`, `tests/test_sigid.py` : décodeurs sur les enregistrements réels d'Artemis, taux d'identification.
- `tools/make_sigid.py` : régénère la base d'identification `orsatdec/data/sigid.json`.
- `tests/fake_tci.py` : faux serveur TCI (audio et VFO) pour tester la source TCI sans AetherSDR.
- `orsatdec/sources.py` : les sources ; `orsatdec/codecs.py` : décodage FLAC et Opus.
- `orsatdec/decoders/` : un fichier par famille de décodeurs.
- `orsatdec/modes.py` : catalogue des modes et fréquences connues.
