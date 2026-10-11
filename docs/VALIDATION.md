# Modules audio — 11 octobre 2026

- 189 tests Python réussis avec Python 3.9.6, dont 38 tests de modules et un test supplémentaire des MUTE/SOLO BlueALSA en capture brute. Somme flottante, limiteur, compresseur lié, EQ d’entrée/bus, gate, automix, ducking, PFL indépendant, mix-minus et MUTE sans résidu de filtres vérifiés sur des signaux numériques.
- Tests des vraies boucles asynchrones de capture/lecture avec des pipes simulés : trames S32 multicanal, négociation sans conversion de canaux, coupure USB, blocage de sortie, annulation d’un bloc DSP et récupération des enfants. Dérive fractionnaire et bornes des tampons contrôlées. Aucun périphérique ALSA réel n’a été ouvert sur le Mac.
- FBQ : suggestion sur une tonalité persistante, absence de pose live sur une note stable, pose sur une tonalité croissante, expiration des filtres live, filtres fixes et banque de balance distincte. Ces essais synthétiques ne valident pas l’efficacité acoustique ni le taux de faux positifs.
- Scènes : sauvegarde/rappel/import/export/suppression, restauration Master/source, gains live étendus, révisions périmées, validation, erreur de disque et retour au mode classique. Échec du démarrage live avec restauration du moteur et de l’EQ précédents. Préparation du PCM brut après l’arrêt des anciens lecteurs ; bits MUTE BlueALSA retirés pour le PFL et restaurés au retour au mode classique.
- Enregistrement : fichier WAV réel avec en-tête finalisé et échantillons attendus, refus faute d’espace, file bornée avec arrêt explicite en cas de saturation. L’aperçu refuse d’inventer un enregistrement. API de modules, exports et téléchargements authentifiés.
- 26 tests JavaScript réussis avec Node 24. En plus de l’EQ et des gestes tactiles : brouillon conservé pendant le polling et après erreur, saisie du nom d’une scène, protection contre la perte d’un brouillon, numérotation des canaux USB, validation des nombres, enregistrement indépendant du brouillon, gain négatif et rotation du bouton.
- Inspection réelle dans Safari sur ordinateur : panneaux Master/FBQ, matrice, interface USB et enregistrement lisibles et défilants ; sauvegarde de la scène « Validation locale », apparition de Rappeler/Exporter et état sauvegardé vérifiés. La tentative d’inspection mobile en conception adaptative a été interrompue par un changement de fenêtre ; aucune validation visuelle mobile complète n’est revendiquée.
- Syntaxes Python/JavaScript/shell et `git diff --check` réussis. Installation, bootstrap et mise à jour incluent les nouveaux modules et sont couverts par la suite simulée. Workflow CI complété pour le JavaScript du panneau.
- Mesure indicative sur le Mac : environ 0,58 ms par bloc de 10 ms, avec quatre micros, EQ/dynamique, automix et FBQ assisté. Cette mesure ne prédit pas les performances du Raspberry Pi.

Le Pi **4B** est la cible communiquée. Les micros et l’interface USB de type Focusrite ne sont pas encore disponibles. Restent à vérifier sur le matériel choisi : exposition ALSA des canaux, duplex 48 kHz, correspondance des sorties/casque, latence réelle, stabilité et charge sur une session prolongée, ainsi que FBQ avec enceintes, parole et musique. Le mode live est désactivé par défaut et le FBQ live est présenté comme expérimental. Le guide figure dans le README.

# EQ MASTER initial — 11 octobre 2026

- 150 tests Python réussis, dont 17 tests EQ : réponses mesurées sur des sinusoïdes, filtres de coupure, plateaux, stabilité, indépendance des canaux, continuité entre blocs, compensation de bandes superposées, saturation sans débordement entier et retour exact des octets en bypass après transition.
- Intégration : les octets filtrés traversent le transfert Loopback et alimentent le vumètre ; la configuration ne relance pas les lecteurs. Activation refusée sur une route indisponible, sauvegarde et restauration après erreur de disque, persistance après rechargement et API authentifiée vérifiées sans matériel.
- 19 tests JavaScript réussis, dont 7 pour l’EQ : cohérence des coefficients Python/JavaScript, courbe logarithmique, écritures successives pendant une requête, rejet des anciennes réponses, glissement tactile à un seul doigt, annulation et réessai après erreur réseau.
- Aperçu réel dans Brave : fenêtre EQ, activation et préréglage Clarté voix, courbe compensée, mention SIMULÉ et absence de spectre matériel inventé. Rendu inspecté sur ordinateur et avec un viewport mobile de 390 × 844 ; la fenêtre défile et les bandes passent sur deux rangées.
- Tests locaux avec Python 3.9.6 et Node 24. Syntaxes Python/JavaScript/shell et `git diff --check` vérifiés. Scripts d’installation, téléchargement et mise à jour testés dans des environnements simulés ; NumPy/SciPy et les nouveaux modules sont inclus.
- Mesure indicative sur le Mac : environ 0,30 ms par bloc audio de 40 ms, six bandes actives et analyse spectrale. Cette mesure ne constitue pas un résultat sur Raspberry Pi.

À ce stade initial de l’EQ, l’écoute, la charge CPU, la stabilité ALSA et les gestes sur une tablette physique restaient à valider sur le Pi cible. La route classique conserve ses périodes de 40 ms et ses tampons de 200 ms. Les modules décrits plus haut ajoutent désormais la capture micro et le FBQ expérimental.

# Vérification de la version 3.0

Ajout de SOLO et voyants MUTE rouges du 10 octobre 2026 :

- SOLO réel par source via les bits mute logiciels BlueALSA, sélection additive et MUTE explicite prioritaire. Volumes et bits mute gauche/droite conservés ; les états de restauration sont enregistrés avant l’exclusion pour survivre à un redémarrage.
- Réapplication aux flux reconnectés et aux nouvelles sources, levée de SOLO sans flux, retrait du dernier canal SOLO et restauration après un échec D-Bus vérifiés. L’API valide strictement le booléen et exige l’authentification. Le mode démo et son vumètre suivent les mêmes exclusions.
- Voyants MUTE rouges sur les sources et MASTER, voyant SOLO ambré distinct, indication « Exclu par SOLO », boutons MUTE/SOLO de 44 × 44 px sur les appareils tactiles et bande de commandes commune pour conserver l’alignement des faders.
- 133 tests Python et 12 tests JavaScript tactiles réussis. Vérification DOM avec les vrais HTML/JavaScript et l’API démo locale : clics SOLO, sélection multiple, volumes inchangés, MUTE indépendant, conservation du focus et canal déconnecté. Vérification DOM existante des commandes, vumètre et banques aux cinq géométries simulées également réussie.
- La vérification DOM ne constitue pas une inspection visuelle ni une validation audio sur Raspberry Pi. L’inspection dans le navigateur n’a pas été menée à terme : l’application était utilisée simultanément et a changé de page. Aucun réglage du matériel réel n’a été exécuté.

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
