# Vérification de la version 3.0

Adaptation tactile du 10 octobre 2026, vérifiée sans navigateur ni iPad :

- Zones GAIN de 56 × 56 px sur les appareils tactiles, gestes verticaux plus fins et vrais boutons −/+ de 44 × 44 px par pas de 0,1 dB. Les boutons suivent les limites et la disponibilité des gains existants.
- Glissement relatif des deux faders MAIN MIX, sans saut de volume au premier contact, avec capture du doigt, synchronisation gauche/droite et protection contre un second doigt concurrent. Les mises à jour de statut préservent le geste même si l’entrée ne reçoit pas le focus. Le fader des sources conserve son geste natif.
- 12 tests Node des gestionnaires JavaScript réels réussis : routes API et valeurs, précision, limites, contrôle indisponible, annulation/perte de capture, plusieurs doigts, lecture du statut pendant un geste, clavier et souris. Ils sont ajoutés au workflow GitHub Actions.
- Vérification du DOM avec le HTML et le JavaScript réels : boutons de gain reliés aux commandes, contrôles existants, sorties indisponibles, LED et banques. Géométrie tactile simulée à 1280 × 900, 1024 × 768, 390 × 844, 320 × 568 et 844 × 390 : respectivement 8, 6, 1, 1 et 5 sources par banque.
- CSS analysé sans erreur ; dimensions calculées à ces formats et à 768 × 1024, avec place suffisante pour les boutons dans chaque colonne. Ces calculs ne constituent pas une inspection visuelle. Syntaxe JavaScript et `git diff --check` valides. Le confort réel au doigt reste à vérifier sur l’iPad.

Accès public au dépôt, vérifié le 10 octobre 2026 :

- `https://github.com/ob1lan/rpi-audio-mixer`, le lanceur brut sur `main` et l’archive GitHub sont accessibles sans authentification. L’archive contient les scripts de setup / mise à jour, le configurateur et les fichiers nécessaires ; la syntaxe POSIX du lanceur téléchargé est valide.
- README adapté à l’accès public : commande directe sans jeton, clone Git et mise à jour depuis `main`. Les instructions de dépôt privé et la mention périmée d’un `upgrade.sh` absent ont été retirées. Aucun setup système ni contrôle d’application graphique effectué pour cette vérification.

Installation par téléchargement, vérifiée uniquement en ligne de commande :

- Lanceur POSIX `bootstrap.sh` : téléchargement complet avant `sudo`, validation des chemins et types de fichiers de l’archive, nettoyage temporaire et propagation des échecs. Accès GitHub privé via un jeton gardé hors des arguments des processus et retiré avant le setup.
- Configuration initiale via l’API locale authentifiée : choix ALSA explicite quand plusieurs sorties existent, conservation d’une destination enregistrée, contrôle logiciel fonctionnel et activation de chaque radio présente. Une sortie non prête ou un blocage restant est un échec, pas un succès annoncé.
- Installation/mise à jour unifiées, sauvegarde et restauration testées sur un Pi simulé. Le script `upgrade.sh`, précédemment absent, est maintenant fourni. Le chargement de `snd-aloop` est persistant ; son fichier est restauré ou retiré à la désinstallation.
- Repli BlueALSA 4.3.1 avec archive SHA-256 épinglée et installation privée des binaires. La compilation réelle et les pilotes Bluetooth/audio restent à valider sur le Raspberry Pi ; aucune installation système n’a été exécutée sur l’ordinateur.
- 123 tests Python réussis, dont 19 pour le téléchargement / configurateur et 16 pour installation / mise à jour / désinstallation. Le configurateur est testé contre la vraie API web en mode démo et avec un terminal POSIX non seekable. Syntaxes Python, JavaScript, Bash et POSIX sh, ainsi que `git diff --check`, valides.


Nettoyage des éléments inopérants, à la demande de l’utilisateur :

- Suppression complète des faux réglages EQ, AUX, FX et BAL, de leurs styles et des fausses prises. Suppression du repère de routage décoratif et de la mention du décor.
- Réduction et réalignement des panneaux ; seuls les gains, faders, MUTE, LINK, réglages Bluetooth/sortie et indicateurs liés aux données restent présents. Les deux faders généraux sont explicitement signalés comme liés.
- Vérification DOM en Node : chaque bouton rotatif et chaque capuchon de fader restant correspond à un input fonctionnel ; absence des anciens réglages et prises dans le HTML. Contrôles indépendants, synchronisation MAIN MIX, MUTE, LINK, capacités indisponibles, niveaux/CLIP, extinction des données périmées et banques à cinq dimensions de fenêtre vérifiés.
- HTML, CSS, SVG, syntaxe JavaScript et `git diff --check` valides. L’URL locale `http://127.0.0.1:8088/` sert bien le nouveau fichier HTML.
- Aucun navigateur ni application graphique utilisé. Les anciens comptes rendus ci-dessous décrivent les présentations précédentes.


Refonte fidèle au X2222USB du 9 octobre 2026, effectuée uniquement dans les fichiers et en ligne de commande :

- Référence : [photographie officielle du panneau supérieur Behringer X2222USB](https://cdn-media.empowertribe.com/ef62b3f10b6742708f3415e984f3ef78/Image_BE_0601-ADA_X2222USB_Top_XL.png). Tranches stéréo, couleurs des rangées rotatives, poussoirs triangulaires, voyants MUTE ambrés à droite des glissières, faders sources noirs et MAIN MIX clairs, vumètre rond à droite.
- EQ, AUX et BAL sont décoratifs et explicitement signalés. Les commandes audio existantes et les valeurs en pourcentage / dBFS sont conservées.
- Vérification DOM en Node avec le fichier HTML réel, le JavaScript réel et un snapshot du backend de démonstration : volumes et gains indépendants, synchronisation des deux faders généraux et de leurs capuchons, MUTE, LINK via le triangle enfant du bouton, sortie indisponible, gain absent, écrêtage, extinction des niveaux périmés, absence de sources et préservation du focus.
- Géométrie simulée à 1280 × 900, 1024 × 768, 390 × 844, 320 × 568 et 844 × 390 : banques de respectivement 8, 7, 2, 1 et 5 sources, MAIN MIX conservé à droite. Cette simulation vérifie les calculs des banques, sans constituer une validation du rendu CSS dans un navigateur.
- 55 tests de contrôles et 33 tests audio réussis. Syntaxe JavaScript, structure HTML, identifiants et associations des labels, absence de styles HTML inline incompatibles avec la CSP, analyse syntaxique CSS et `git diff --check` vérifiés.
- Aucun navigateur ou autre application graphique n’a été ouvert, piloté ou inspecté pendant cette refonte, conformément à la demande. Le rendu effectif reste à vérifier par l’utilisateur.
- Les tests de migration restent hors de cette validation : `upgrade.sh` est absent de ce checkout, comme constaté avant cette refonte.

Révision audio précédente :

Révision X2222USB du 9 octobre 2026, sans Raspberry Pi connecté :

- Référence du panneau XENYX X2222USB consultée : châssis graphite, commandes de gain noires, faders de source gris clair, faders stéréo MASTER noirs liés, MUTE à icône au-dessus des faders avec LED rouge séparée à droite.
- Gains indépendants de 0 à +4 dB par source, contrôles ALSA softvol distincts et lecteurs filtrés par adresse Bluetooth. Persistance et retour arrière testés.
- Vumètre stéréo de 12 segments par côté : vert, orange et rouge, crêtes et CLIP maintenus brièvement. API authentifiée à 10 Hz, extinction des données périmées. La production mesure les mêmes échantillons S16_LE transmis depuis ALSA Loopback vers la sortie ; l’aperçu reste explicitement simulé.
- 55 tests de contrôles et 33 tests audio réussis. Les nouveaux tests couvrent gain indépendant et persistant, entrées invalides, rollback des lecteurs, mesure gauche/droite, silence, clipping, transfert des mêmes octets audio et données périmées.
- Vérification DOM avec l’API de démonstration réelle : indépendance gain/fader, MUTE, déconnexion/reconnexion, gain général, dialogues, sortie sans gain général positif, LED de niveau, CLIP et extinction en cas de perte de mesure.
- Logique des banques vérifiée avec géométrie simulée à 1280 × 900, 390 × 844, 320 × 568 et 844 × 390, y compris conservation du focus. Ces géométries ne constituent pas une inspection visuelle du rendu CSS.
- Syntaxes Python et JavaScript, scripts Bash existants et `git diff --check` valides.
- L’inspection visuelle de cette révision n’a pas pu être terminée : le navigateur disponible changeait de page pendant les actions d’automatisation. L’URL locale est laissée active pour revue.
- Les neuf tests de migration restent bloqués par `upgrade.sh`, absent de ce checkout avant cette révision.

L’activation et le débit réel de snd-aloop, la latence supplémentaire du transfert, l’écoute simultanée de plusieurs sources et les gains ALSA restent à vérifier sur le Pi. Les anciennes routes personnalisées ne sont pas converties automatiquement et affichent le gain par source et le vumètre comme indisponibles.

Présentation précédente, inspectée sur ordinateur et à 390 × 844 :

- La version précédente utilisait un bouton LEVEL lié au fader, sans mesure audio. Elle a été remplacée par la révision ci-dessus.

Vérification antérieure :

Vérification locale du 8 octobre 2026, sans Raspberry Pi connecté.

- 87 tests Python réussis : 25 pour le moteur ALSA et le lecteur, 53 pour les contrôles Bluetooth et web, 9 pour l’installation, la migration et la désinstallation simulées.
- Syntaxe JavaScript et tous les scripts Bash vérifiés.
- Aperçu dans le navigateur : cinq sources, noms courts et longs, format ordinateur 1440 × 1000 et mobile 390 × 844.
- Bandeaux de 78 px sur ordinateur et 56 px sur mobile. Dans chaque rangée, faders et boutons MUTE se trouvent aux mêmes hauteurs, y compris MASTER.
- Choix d’une carte USB et de son contrôle sans gain positif, application et rechargement : destination et limite de gain conservées.
- Renommage d’un récepteur : nom visible et sélections mis à jour.
- Première configuration sur mobile : panneau ouvert automatiquement, sortie HDMI choisie, MASTER rendu disponible, volume et MUTE vérifiés.
- Aucun débordement horizontal observé au format mobile ; commandes du formulaire de 44 px de hauteur.

Les tests utilisent des objets D-Bus, cartes ALSA et processus simulés. Ils ne valident ni la qualité sonore, ni la latence, ni le nombre maximal de flux simultanés sur une radio physique. La configuration ALSA générée et la migration restent à tester sur chaque matériel cible.
