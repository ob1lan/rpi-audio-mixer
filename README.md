# Bureau Audio 3.0

Console web locale pour recevoir plusieurs sources Bluetooth, les mélanger en stéréo sur un Raspberry Pi et écouter le résultat sur une seule sortie audio. Chaque source possède son fader et son MUTE. MASTER règle le volume commun et le gain disponible.

```text
Téléphone / ordinateurs → récepteurs Bluetooth du Pi → BlueALSA
                         → mélange ALSA stéréo → jack / USB / HDMI
```

Pour des écouteurs sans fil, branche un émetteur Bluetooth à la sortie du Pi. L’application ne gère pas la transmission Bluetooth sortante ni le micro.

Le paquet ne contient aucune adresse Bluetooth, source ou carte audio imposée. La détection est automatique ; la sortie peut être choisie dans le terminal à l’installation ou dans la page web. Il peut servir sur d’autres Pi sous Debian / Raspberry Pi OS avec systemd ; la compatibilité sonore avec toutes les radios et cartes n’est pas garantie par les tests simulés.

## Installation

Prérequis : Python 3.9+, BlueZ, ALSA et BlueALSA avec `bluealsa-aplay`, un récepteur Bluetooth A2DP et une sortie ALSA stéréo. Une carte USB peut remplacer le jack absent de certains modèles. L’application accepte les formats de volume BlueALSA `uint16` et tableau d’octets, ainsi que les propriétés `Sampling` et `Rate`.

### Une seule commande

Quand ces fichiers sont publiés sur `main` et que le dépôt est public, colle cette commande dans le terminal du Pi :

```sh
curl -fsSL https://raw.githubusercontent.com/ob1lan/rpi-audio-mixer/main/bootstrap.sh | sh
```

Le lanceur télécharge un instantané complet du dépôt, vérifie l’archive, puis demande `sudo` et exécute l’installation et la configuration. Il installe les dépendances, configure les services au démarrage, débloque les radios Bluetooth par logiciel, active les récepteurs internes et USB détectés et charge `snd-aloop` pour le vumètre, y compris aux prochains démarrages. Si une seule sortie audio existe, elle est sélectionnée ; sinon, choisis son numéro dans le terminal. Le volume logiciel réel **Bureau Audio Master** est sélectionné. L’adresse web et le code d’accès sont affichés à la fin. L’archive temporaire est supprimée.

Une nouvelle exécution met à jour l’application en conservant code d’accès, appairages, sorties et volumes. Une ancienne route ALSA personnalisée reste conservée. Un défaut matériel, une radio toujours bloquée ou une sortie inutilisable est signalé avec un code de sortie d’échec ; le service installé reste disponible pour terminer la configuration. `rfkill` ne peut pas lever un blocage matériel ni réactiver un contrôleur absent / désactivé dans le firmware.

Pour une installation sans question, donne l’identifiant ALSA et le numéro du périphérique (exemple à adapter à ta carte) :

```sh
curl -fsSL https://raw.githubusercontent.com/ob1lan/rpi-audio-mixer/main/bootstrap.sh | sh -s -- --non-interactive --output Headphones:0
```

Sans sortie déjà enregistrée et avec plusieurs destinations possibles, `--non-interactive` exige `--output` et affiche les identifiants disponibles. Il ne choisit pas une carte arbitraire. `--repo OWNER/REPO` et `--ref BRANCH_OU_TAG` permettent de télécharger un autre dépôt ou une version déterminée ; ils précèdent les options de configuration.

Pour un **dépôt privé**, exporte `GH_TOKEN` avec un jeton GitHub autorisé à lire le contenu du dépôt, puis utilise l’API authentifiée pour télécharger le lanceur :

```sh
read -rsp 'GitHub token: ' GH_TOKEN; printf '\n'; export GH_TOKEN
printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw+json"\n' "$GH_TOKEN" | curl --config - -fsSL 'https://api.github.com/repos/ob1lan/rpi-audio-mixer/contents/bootstrap.sh?ref=main' | sh
unset GH_TOKEN
```

Le jeton ne figure pas dans les arguments de `curl`, n’est pas enregistré et n’est pas transmis à l’installation privilégiée. Le dépôt doit contenir `bootstrap.sh` avant d’utiliser ces commandes.

Le lecteur doit proposer `--volume=software`. L’installation automatique conserve un lecteur compatible existant. Sinon, elle compile [BlueALSA 4.3.1](https://github.com/arkq/bluez-alsa/blob/v4.3.1/INSTALL.md), avec le codec SBC, dans `/opt/bureau-audio-tools/bluealsa-4.3.1`, à partir d’une archive dont le SHA-256 est vérifié. Les binaires Debian et les versions compilées par l’utilisateur ne sont pas écrasés. Cette compilation peut prendre plusieurs minutes sur un Pi. Les [options du lecteur 4.3.1](https://github.com/arkq/bluez-alsa/blob/v4.3.1/doc/bluealsa-aplay.1.rst) décrivent le volume logiciel.

Depuis le dossier du projet copié sur le Pi :

```sh
sudo bash install.sh --configure
```

La même configuration automatique est disponible depuis un clone local. `sudo bash install.sh` sans `--configure` conserve le parcours manuel et exige un lecteur compatible déjà disponible. Pour reprendre une configuration interrompue : `sudo python3 /opt/bureau-audio/configure.py`.

1. Pour modifier la sortie choisie à l’installation, ouvre **Configuration de la sortie**, choisis la carte, la sortie et le contrôle de volume, puis **Appliquer la sortie**. Le contrôle logiciel **Bureau Audio Master** propose volume, MUTE et gain jusqu’à +4 dB.
2. Dans **Bluetooth et appareils**, renomme les récepteurs si nécessaire : c’est le nom visible depuis les sources.
3. Clique sur **Appairer** sur le récepteur souhaité. Pendant trois minutes, sélectionne ce récepteur dans les réglages Bluetooth du téléphone ou de l’ordinateur.
4. Sélectionne-le comme sortie son sur cet appareil et lance une lecture. Sa tranche apparaît automatiquement.

Sans daemon BlueALSA actif, l’installation crée son service de réception A2DP sur tous les contrôleurs, sans liste d’adresses imposée. Un daemon actif est conservé ; avec `--configure`, son unité standard `bluealsa.service` ou `bluealsad.service` est activée au démarrage si elle était désactivée. Cet état est sauvegardé pour la désinstallation. L’application gère le lecteur `bluealsa-aplay` : un ancien service de lecture standard est arrêté et désactivé, avec son état sauvegardé pour la désinstallation.

Un daemon préexistant doit exposer `org.bluealsa`, accepter A2DP sink sur les récepteurs utilisés et démarrer automatiquement avec le Pi. Un lecteur lancé manuellement ou sous une autre unité doit être arrêté avant de choisir une sortie.

L’installation nécessite root. Ensuite, l’interface et le lecteur fonctionnent sous `bureau-audio`, membre du groupe `audio`. Le bouton **Activer** une radio utilise une unité privilégiée à commande fixe.

## Mise à jour depuis 2.x ou 3.0

Copie le nouveau dossier sur le Pi puis lance :

```sh
sudo bash upgrade.sh --configure
```

Le code d’accès, les appairages, les affectations, les noms Bluetooth, les appareils retirés et les volumes sont conservés. Une ancienne route ALSA personnalisée est conservée quand `BUREAU_CARD` est présent ; `BUREAU_PCM` précise son nom, avec `hub_mix` comme valeur de migration par défaut.

**Le lecteur audio change de propriétaire : une brève interruption est attendue.** Le service web lance désormais le lecteur pour appliquer réellement les changements de sortie. Le script sauvegarde code, préférences, unité web et état immédiat du lecteur. Si le serveur ne démarre pas, il restaure la version précédente. `/health` confirme la version web, pas la réception ni l’écoute effective : vérifie les flux après la migration.

Les fichiers ALSA globaux ne sont pas réécrits. Les sauvegardes restent dans `/var/backups/bureau-audio`. Recharge la page et saisis le code habituel. Pour remplacer uniquement la présentation d’un serveur déjà en 3.0, suis les instructions de la section **Sources et interface**.

## Sources et interface

Les tranches utilisent **Name**, le nom distant connu par BlueZ, et non un alias personnalisé local. Le nom est mémorisé pour les sources déconnectées ; l’adresse est affichée si aucun nom n’est disponible. Seuls les appareils capables d’envoyer un flux A2DP sont ajoutés automatiquement.

Les affectations persistent après redémarrage. Les adresses des récepteurs les identifient indépendamment des numéros `hci`. Pour déplacer une source, choisis son récepteur dans **Tes appareils** puis **Appliquer**. L’ancien lien est déconnecté et bloqué ; appaire ensuite la source sur le nouveau récepteur si nécessaire. Un récepteur absent n’entraîne pas de bascule automatique.

**Retirer** masque et déconnecte une source, en conservant son appairage. **Rétablir** la réactive sur le récepteur choisi. Une nouvelle source ou un remplacement de téléphone ne nécessite aucune modification du code.

Le volume par source agit sur son flux BlueALSA stéréo. Lance une lecture si son fader est indisponible. MASTER agit sur le contrôle choisi pour la sortie, avec le gain permis par celui-ci. Les petits voyants des sources indiquent un flux actif : il peut transporter du silence. Le vumètre MASTER mesure séparément les crêtes numériques gauche et droite quand ALSA Loopback est disponible.

La présentation reprend le style du [Behringer XENYX X2222USB](https://www.behringer.com/en/products/0601-ADA) pour les seules commandes effectives : panneaux graphite, surface inférieure argentée, capuchons de faders noirs pour les sources et clairs pour MAIN MIX. Les poussoirs MUTE noirs à triangle ont un voyant ambré à droite de la glissière. Le voyant vert FLUX indique un flux actif, sans mesurer son niveau individuel. Les deux colonnes de voyants ronds du vumètre sont placées à droite, au-dessus des faders généraux liés. Aucun bouton EQ, AUX, FX ou BAL décoratif ni fausse prise ne figure dans la console.

GAIN, MUTE, LINK et les faders commandent les fonctions audio existantes. Les graduations des faders restent en pourcentage et celles du vumètre en dBFS. Les gains de source vont de 0 à +4 dB, indépendamment des faders. Fais glisser un bouton GAIN vers le haut ou vers la droite pour augmenter sa valeur ; les flèches du clavier fonctionnent également. LINK déconnecte ou reconnecte la source ; son infobulle précise l’action. Les deux faders MAIN MIX règlent le même volume stéréo et portent la mention « L / R LIÉS ».

Les noms sont inscrits sous les faders et disposent de deux lignes. Les sources et MAIN MIX conservent leurs faders verticaux sur téléphone. Les flèches de banque donnent accès aux autres sources, tandis que MAIN MIX reste visible à droite. La longueur des glissières et le nombre de tranches visibles s’adaptent à la fenêtre. Une fenêtre très basse peut nécessiter un défilement vertical. Bluetooth et la configuration de sortie s’ouvrent dans des fenêtres dédiées, défilantes, qui se ferment avec **Fermer**, Échap ou un clic à l’extérieur.

La révision audio ajoute des commandes côté serveur. Pour un serveur 3.0 existant, sauvegarde `/opt/bureau-audio` et les préférences, puis remplace `app.py`, `audio.py` et les quatre fichiers `static/index.html`, `static/style.css`, `static/app.js`, `static/knob-scale.svg`. Redémarre `sudo systemctl restart bureau-audio` puis recharge complètement la page. Une brève interruption audio est attendue. Si la révision audio est déjà installée et que tu remplaces uniquement la présentation, les quatre fichiers statiques suffisent et aucun redémarrage audio n’est nécessaire. Le SVG contient les graduations des boutons rotatifs. Le fichier `upgrade.sh` cité plus haut est absent de ce checkout : ne lance pas `install.sh` comme remplacement d’une mise à jour.

L’installation avec `--configure` active les niveaux réels automatiquement. Pour les activer séparément sur un Pi utilisant une sortie **gérée** :

```sh
sudo modprobe snd-aloop
printf '%s\n' snd-aloop | sudo tee /etc/modules-load.d/bureau-audio-meter.conf
sudo systemctl restart bureau-audio
```

Le serveur détecte la carte `Loopback` et dirige le mélange vers elle, puis transfère les mêmes échantillons stéréo à la sortie physique. Les deux colonnes de 12 LED montrent les crêtes en dBFS : vert, orange près du maximum, rouge à l’écrêtage. Une crête reste visible environ 650 ms ; CLIP reste allumé une seconde. Les mesures sont transmises dix fois par seconde et s’éteignent en cas de données périmées. Le transfert ajoute des tampons et peut augmenter la latence ; sa fluidité reste à valider sur le Pi. Les mesures précèdent le DAC et tout réglage analogique matériel ; elles ne mesurent pas le niveau acoustique. Sans Loopback ou avec une ancienne route personnalisée, le vumètre indique **INACTIF** et l’écoute conserve sa route directe. `BUREAU_METER_CARD=off` dans l’environnement du service désactive ce transfert ; redémarre le service après modification. L’aperçu local est explicitement **SIMULÉ**, sans capture du son de l’ordinateur.

## Audio et limites

Pour une nouvelle sortie, le moteur crée une configuration ALSA privée : conversion `plug`, gain `softvol` par source, volume commun `softvol` si choisi, puis mélange `dmix`. Chaque lecteur BlueALSA est filtré par adresse Bluetooth. Le gain des sources est mémorisé, y compris après retrait et rétablissement. Une ancienne route personnalisée conserve son lecteur unique et ne propose pas de gain par source. La sortie matérielle est testée en **stéréo S16_LE, 48 kHz**, avec périodes de 40 ms et cinq périodes de tampon. Une carte incompatible échoue avec un diagnostic. Un changement raté restaure la sortie précédente.

Changer de destination interrompt brièvement le lecteur ; renommer la même destination ne le redémarre pas. Le gain matériel dépend du maximum dB déclaré par ALSA ; les contrôles sans mute utilisable ne sont pas proposés. Le volume logiciel reste disponible sur une sortie sans contrôle physique.

Le volume logiciel ne modifie pas les contrôles matériels de la carte. Si l’un d’eux est observé muet ou à zéro, un diagnostic le signale : il peut bloquer l’écoute, selon son lien avec la sortie choisie. Vérifie ce contrôle ou sélectionne-le comme contrôle de volume ; l’application n’ouvre pas arbitrairement toutes les sorties de la carte.

Le gain positif et la somme de sources fortes peuvent saturer : réduis les faders si tu entends une distorsion. Aucun limiteur n’est intégré. Le vumètre nécessite la carte Loopback ; l’ajout ou le retrait d’une source régénère les étages de gain et relance brièvement les lecteurs. La fluidité, le nombre de flux et la latence dépendent des radios et des sources. Plusieurs récepteurs permettent de répartir les flux, sans garantir un nombre illimité de lectures simultanées. Une console de jeu sans sortie A2DP nécessite une entrée ou un émetteur séparé.

## Données de chaque installation

| Emplacement | Rôle |
| --- | --- |
| `/opt/bureau-audio` | Code et interface |
| `/etc/bureau-audio.env` | Code d’accès, adresse et port web |
| `/etc/bureau-audio-routes.json` | Routes initiales, facultatives sur une installation neuve |
| `/var/lib/bureau-audio/settings.json` | Récepteurs, sources, volumes et destination choisis |
| `/var/lib/bureau-audio/alsa-playback.conf` | Configuration ALSA privée |
| `/var/lib/bureau-audio-install` | Métadonnées root pour restaurer les services |

Le `routing.json` distribué est vide. N’ajoute pas les réglages d’un Pi réel au dépôt. Le `.gitignore` exclut préférences, environnements, états de démonstration et sauvegardes usuels. L’application est destinée au réseau local ; l’installation ne configure ni accès Internet ni HTTPS.

## Développement et dépôt GitHub

Copie ce dossier tel quel dans ton dépôt privé : code, tests, scripts, documentation et workflow GitHub Actions sont inclus. Aucun remote ou dépôt n’est imposé. Le workflow vérifie Python, JavaScript et shell ; il ne déploie pas sur un Pi.

Sur Mac ou Linux, pour l’aperçu sans matériel :

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
BUREAU_STATE=./demo-state BUREAU_DEMO_EXTRA=2 .venv/bin/python app.py --demo --host 127.0.0.1 --port 8088
```

Ouvre `http://127.0.0.1:8088`, code `demo`. Le bandeau signale que ces commandes ne pilotent aucun matériel. Pour tester la première configuration, ajoute `BUREAU_DEMO_SETUP=1` avec un nouveau dossier d’état.

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check static/app.js
for script in *.sh; do bash -n "$script"; done
```

Les tests couvrent les contrats D-Bus, les deux formats de volume, les routes dynamiques, les accès web, le lecteur, ses échecs et la migration avec retour arrière. Les radios, cartes et commandes sont simulées. Une précédente présentation a été inspectée dans le navigateur sur ordinateur et mobile. Cette révision est vérifiée par tests Python et DOM ; sa validation visuelle complète et sa validation sonore restent à effectuer sur le matériel cible.

## Diagnostic et désinstallation

```sh
sudo systemctl status bureau-audio --no-pager
sudo journalctl -u bureau-audio -n 60 --no-pager
sudo sed -n 's/^BUREAU_KEY=//p' /etc/bureau-audio.env
```

Pour désinstaller, garde les récepteurs branchés puis lance `sudo bash uninstall.sh`. Les drapeaux Bluetooth encore contrôlés par l’application et l’état sauvegardé de l’ancien lecteur sont restaurés. Les données et appairages sont conservés. Le daemon créé par cette installation est retiré ; un daemon préexistant est conservé. Si un récepteur est absent, rebranche-le et relance la désinstallation pour terminer sa restauration.

## Références techniques

- [BlueZ : propriétés des appareils](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Device.rst).
- [API PCM BlueALSA 4.3.1](https://github.com/arkq/bluez-alsa/blob/v4.3.1/doc/org.bluealsa.PCM1.7.rst) et [API actuelle](https://github.com/arkq/bluez-alsa/blob/master/doc/org.bluealsa.PCM1.7.rst).
- [Référence visuelle XENYX X2222USB](https://www.behringer.com/en/products/0601-ADA).
- [ALSA Loopback](https://www.alsa-project.org/wiki/Matrix:Module-aloop).
- [ALSA dmix](https://github.com/alsa-project/alsa-lib/blob/master/src/pcm/pcm_dmix.c) et [softvol](https://github.com/alsa-project/alsa-lib/blob/master/src/pcm/pcm_softvol.c).
- [GitHub Actions checkout](https://github.com/actions/checkout) et [setup-python](https://github.com/actions/setup-python).
