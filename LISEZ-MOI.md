# Orsat-Decoder

Décodeur multimode natif Linux pour **Orsat-SDR** et **PhantomSDR-Plus**. Un seul logiciel, sans Wine.

Orsat-Decoder se connecte au serveur comme la page web : il affiche son waterfall large bande, et chaque canal de décodage ouvre son propre flux audio sur sa fréquence. On peut donc décoder plusieurs signaux en même temps, par exemple Navtex, météo RTTY et FT8.

## Installation

```bash
git clone https://github.com/f1nsk40260/Orsat-Decoder.git
cd Orsat-Decoder
./install.sh
```

L'installateur :
- installe Python et un compilateur C s'ils manquent (sudo demandé une fois) ;
- crée son environnement Python dans `~/.local/share/orsat-decoder` ;
- compile les décodeurs natifs embarqués ;
- ajoute **Orsat-Decoder** au menu des applications ;
- termine par un autotest de chaque décodeur.

Relancer `./install.sh` met à jour en gardant la configuration et les canaux.

## Utilisation

1. Choisissez le serveur en haut à gauche. ORSAT et Orsat-SDR local sont proposés ; les adresses se règlent dans les réglages.
2. Choisissez un mode dans la colonne de gauche, puis **cliquez sur un signal** dans le waterfall : un canal s'ouvre. Chaque décodeur retrouve seul son signal dans environ ±100 Hz autour du clic.
3. Ou ouvrez directement une **fréquence connue** : Navtex 518 et 490 kHz, météo DWD, FT8 sur toutes les bandes…

Dans le waterfall :
- molette : zoom ;
- glisser : se déplacer dans la bande ;
- glisser un marqueur de canal : réaccorder ce canal.

Dans une carte de canal : fréquence modifiable au clavier, paramètres du mode, pause, enregistrement du texte.

| Commande | Effet |
|---|---|
| `orsat-decoder` | lance l'application |
| `orsat-decoder --check` | autotest des décodeurs (sans serveur) |
| `orsat-decoder --lan` | interface accessible depuis le réseau local (`http://ip:8074/`) |
| `orsat-decoder --log` | journal de la dernière session |

## Modes du jalon 1

| Mode | Décodeur | Sensibilité mesurée (bruit dans 2500 Hz) |
|---|---|---|
| PSK31 / 63 / 125 | écrit pour Orsat-Decoder | ≈ −11 / −8 / −5 dB |
| RTTY (45 à 100 bd, shift réglable) | écrit pour Orsat-Decoder | ≈ −8 dB à 45 bd |
| CW, vitesse automatique 5-60 mpm | écrit pour Orsat-Decoder | ≈ −8 dB à 20 mpm |
| Navtex / SITOR-B | écrit pour Orsat-Decoder | ≈ −8 dB |
| FT8 / FT4 | ft8_lib (MIT), embarqué | ≈ −18 dB |

Comparaison faite sur les mêmes enregistrements avec MultiPSK : Orsat-Decoder est meilleur en PSK31, CW et Navtex, et à moins de 1 dB en RTTY.

## Limite de connexions

Chaque canal est un auditeur pour le serveur. Un PhantomSDR-Plus limite en général à **3 auditeurs par adresse IP** (`per_ip` dans `config.toml`).

Lancé **sur la machine du serveur**, Orsat-Decoder n'a pas cette limite. S'il trouve le fichier `.tap_token` d'Orsat-SDR (dans `~/Orsat-SDR`), ses canaux sont même comptés comme clients internes, comme le client autorun, et n'apparaissent pas parmi les auditeurs.

## Pour le développement

- `tests/selftest.py` : autotest rapide.
- `tests/harness.py` : banc de mesure de sensibilité.
- `tests/bandgen.py` : génère une bande HF synthétique en IQ pour alimenter un `spectrumserver` (driver `stdin`, `f32`, `iq`) et tester tout le logiciel sans antenne.
- `orsatdec/decoders/` : un fichier par famille de décodeurs.
- `orsatdec/modes.py` : catalogue des modes et fréquences connues.
