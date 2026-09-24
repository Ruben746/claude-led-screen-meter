# Claude meter

[Français](#français) · [English](#english)

Your Claude plan usage (5-hour window and weekly limit) on a 96×16 Bluetooth LED matrix, driven by a Raspberry Pi, with a small web panel on your local network.

---

## Français

### Ce que ça fait

L'écran affiche en permanence :
- la barre du **5H**, avec son pourcentage, et toutes les 30 s l'heure de réinitialisation ;
- la barre de la **semaine** (WEEK).

Deux animations sont activables séparément : un flash quand la fenêtre de 5 heures repart à zéro, et la barre qui grimpe quand l'usage augmente. En cas de problème persistant, les dernières valeurs restent affichées et un code rouge (`AUTH`, `NET` ou `ERR`) remplace le pourcentage.

Le panneau web, accessible sur `http://claude-meter.local:8080` ou sur l'IP du Pi, permet de :
- voir un aperçu de l'écran en direct ;
- choisir la méthode de connexion au compte Claude ;
- trouver l'écran en Bluetooth ;
- régler la luminosité, l'orientation et l'alimentation ;
- régler les intervalles et les animations.

L'écran choisi et tous les réglages sont enregistrés dans `.env`, y compris l'alimentation. Le compteur se reconnecte au démarrage et après une coupure Bluetooth, puis réapplique luminosité, orientation et alimentation. Après un échec, les tentatives automatiques sont espacées de 10 secondes. Seules les orientations 0° et 180° sont proposées ; les anciennes valeurs 90°/270° reviennent à 0° au chargement.

Le panneau est organisé en trois onglets : **Compteur** (quotas et compte Claude), **Spotify** et **Paramètres** (écran, animations et visibilité de Spotify). L'aperçu LED reste visible dans les trois onglets. Les messages locaux, les pages d'envoi à distance et les relais externes ne sont pas inclus.

### Spotify (facultatif)

Dans **Spotify**, connecte ton compte pour afficher la pochette, le titre et éventuellement l'artiste à chaque nouveau morceau. Après la durée choisie (10 secondes par défaut), l'écran revient aux quotas. La durée, la vitesse et l'affichage de l'artiste sont sauvegardés. Les couleurs unies des barres Claude restent inchangées.

1. Crée ou ouvre ton application dans le [tableau de bord Spotify Developers](https://developer.spotify.com/dashboard).
2. Enregistre l'adresse de retour affichée dans l'onglet, par défaut `http://127.0.0.1:8080/spotify/callback`.
3. Renseigne son **Client ID**, clique sur **Connecter Spotify**, puis ouvre le lien proposé et autorise la connexion. Effectue cette étape depuis l'ordinateur qui exécute LED Meter. Pour un Pi sans navigateur, utilise un tunnel SSH vers son port web depuis ton ordinateur.

Le parcours [OAuth PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) n'exige aucun Client Secret. Les identifiants renouvelables restent dans `.spotify-oauth.json`, exclu de Git. Spotify impose une [adresse de retour loopback explicite](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri), pas `localhost`. L'accès dépend aussi des restrictions du compte et de l'application Spotify ; le panneau indique les refus et respecte les délais HTTP 429.

Dans **Paramètres**, décoche **Afficher l'onglet Spotify** pour le masquer et arrêter son suivi et son affichage LED, sans effacer la connexion. Pour laisser l'onglet visible mais suspendre l'affichage musical, décoche **Afficher les nouveaux morceaux sur l'écran** dans Spotify. Le bouton **Déconnecter** efface uniquement les identifiants Spotify du compteur.

### Matériel

- Un Raspberry Pi avec Bluetooth (testé sur Raspberry Pi OS, base Debian).
- Une matrice LED iPixel 96×16 pilotée en BLE via [pypixelcolor](https://pypi.org/project/pypixelcolor/).

### Installation

```bash
git clone https://github.com/Ruben746/claude-led-screen-meter.git
cd claude-led-screen-meter
./install.sh
```

L'installeur :
- installe Bluetooth et avahi ;
- crée l'environnement Python ;
- copie `.env.example` en `.env` ;
- crée deux services : `claude-meter` pour l'application, `claude-meter-mdns` pour l'adresse `claude-meter.local`.

Le Pi garde son propre nom d'hôte. Pour un autre nom : `LED_MDNS_NAME=bureau-meter ./install.sh`.

Ouvre ensuite le panneau, clique sur **Find displays**, choisis ton écran, puis connecte le compte Claude.

### Lancement rapide sous Windows

Pour une installation durable, double-clique sur **Installer-Windows.bat**. L'installateur demande si LED Meter doit démarrer automatiquement à l'ouverture de ta session Windows (**O/N**). Entrée conserve le choix existant ; lors d'une première installation, le choix par défaut est **non**. L'application est installée dans `Documents/LED Meter`, avec un raccourci sur le Bureau dans les deux cas. Aucun droit administrateur n'est nécessaire. Connecte ton compte depuis cette installation une première fois ; ses identifiants restent séparés de ceux d'autres copies.

Le raccourci du Bureau ouvre le panneau sans lancer une seconde instance. Les erreurs sont enregistrées dans `meter.log` dans le dossier d'installation. Pour modifier le démarrage automatique, relance l'installateur et choisis **O** ou **N** ; le choix non retire le raccourci de démarrage existant. Tu peux aussi le retirer depuis `shell:startup` (**Win+R**). Une réinstallation conserve les réglages et les connexions Claude et Spotify. En ligne de commande, `install_windows.ps1 -AutoStart Yes` ou `-AutoStart No` permet de fournir le choix explicitement.

Double-clique sur **Lancer.bat**. Python 3 doit être installé. Au premier lancement, le script prépare l'environnement et installe les dépendances ; les lancements suivants les réutilisent. Il conserve le fichier `.env` existant et ouvre le panneau dans le navigateur quand le serveur est prêt.

Garde la fenêtre ouverte pendant l'utilisation. Ferme-la ou utilise **Ctrl+C** pour arrêter le compteur. Aucun écran LED n'est nécessaire pour tester le panneau.

### Connexion au compte Claude

Deux méthodes, à choisir dans le panneau.

**Claude OAuth** (connexion par code ; des erreurs HTTP 429 persistantes restent possibles).

1. Dans le panneau, choisis **Claude OAuth**, puis **Connect with Claude**.
2. Clique sur **Open Claude sign-in**, connecte-toi sur Claude et autorise la connexion.
3. Copie le code affiché par Claude, reviens dans le panneau et colle-le dans **Authorization code**.
4. Clique sur **Complete connection**.

Garde le panneau ouvert pendant la connexion. Le lien expire après 10 minutes ; après une erreur ou un redémarrage, recommence avec **Connect with Claude**. En cas de HTTP 429, respecte le délai affiché avant de demander un nouveau code. Le compteur utilise `Retry-After` (secondes ou date HTTP), ou une pause locale de 120 secondes si le serveur ne donne aucun délai exploitable. Cette pause ne garantit pas la réussite suivante et n'est pas conservée après un redémarrage. Un échec conserve la méthode de connexion existante ; garde le mode session s'il fonctionne.

Le compteur enregistre ses propres identifiants dans `.meter-oauth.json` (exclu de Git, permissions privées) et renouvelle automatiquement le jeton. Il conserve aussi le nouveau jeton de renouvellement quand Claude le remplace. Aucune installation de Claude Code ni copie de cookie n'est nécessaire. Les permissions demandées sont `org:create_api_key user:profile`, sans `user:inference`.

Pour une installation existante, fais `git pull`, puis `sudo systemctl restart claude-meter`, et connecte-toi une fois avec le nouveau bouton. Les anciens identifiants Claude Code ne sont pas écrasés. Une révocation côté Claude peut toujours nécessiter une nouvelle connexion.

**Claude Code (ancien parcours, avancé)**. Tu peux conserver une connexion sur le Pi lui-même :

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude        # se connecter avec le compte Claude, puis /exit
```

Ce parcours exige de définir explicitement `LED_OAUTH_FILE=~/.claude/.credentials.json` dans `.env`, puis de redémarrer le compteur. Par défaut, seul `.meter-oauth.json` est utilisé : aucun ancien identifiant n'est lu ni renouvelé automatiquement. Deux précautions :
- Ne copie pas ce fichier depuis un ordinateur qui utilise aussi Claude Code : les deux se disputeraient le même jeton.
- Ne fais pas `claude logout` sur le Pi.

Dans ce mode, l'usage est lu au plus une fois par minute.

**Session claude.ai**. Colle la valeur du cookie `sessionKey` de claude.ai dans le panneau. Si Cloudflare bloque, ajoute aussi `cf_clearance` et le User-Agent exact du navigateur d'où vient ce cookie. L'organisation est détectée automatiquement. Une session expire : il faudra la recoller de temps en temps.

### Avertissement

Projet personnel, **non affilié à Anthropic et non approuvé par Anthropic**. Il repose sur des endpoints non documentés, qui peuvent changer ou disparaître à tout moment. Les conditions d'Anthropic réservent les jetons OAuth des abonnements Claude à Claude Code et Claude.ai, et encadrent l'accès automatisé à leurs services. Les deux méthodes de connexion sortent donc de ce cadre. Tu les utilises sous ta propre responsabilité, avec ton propre compte.

Le panneau n'a pas d'authentification par défaut : garde-le sur ton réseau local. `LED_ADMIN_TOKEN` exige un code pour modifier le compte ou l'écran.

### Dépannage

```bash
journalctl -u claude-meter -f              # logs en direct
sudo systemctl restart claude-meter
```

- **Find displays ne trouve rien** : l'écran est peut-être encore connecté à l'app du téléphone. Ferme-la, puis relance la recherche.
- **`claude-meter.local` ne répond pas** : utilise l'IP du Pi. Certains réseaux ou appareils Android anciens ne résolvent pas le mDNS.

---

## English

### What it does

The display shows at all times:
- the **5H** bar with its percentage, plus the reset time every 30 s;
- the **weekly** bar (WEEK).

Two animations can be switched on separately: a flash when the 5-hour window resets, and the bar climbing when usage goes up. If a problem persists, the last values stay on screen and a red code (`AUTH`, `NET` or `ERR`) replaces the percentage.

The web panel, at `http://claude-meter.local:8080` or the Pi's IP, lets you:
- see a live preview of the display;
- choose how to sign in to Claude;
- find the display over Bluetooth;
- set brightness, orientation and power;
- set the intervals and animations.

The selected display and every setting are saved to `.env`, including power. The meter reconnects at startup and after Bluetooth disconnections, then restores brightness, orientation and power. Failed automatic attempts are spaced 10 seconds apart. Only 0° and 180° orientations are supported; legacy 90°/270° values load as 0°.

Bluetooth connection detection uses the private `AsyncClient._session.is_connected` property because pypixelcolor 0.5.0 can retain a stale client connection flag after a remote disconnect. Recheck this integration when upgrading the dependency.

The panel has three tabs: **Compteur** (meter and Claude account), **Spotify**, and **Paramètres** (settings, display and Spotify visibility). The LED preview remains visible in all tabs. Local messages, remote message pages and relay polling are not included.

### Optional Spotify display

The Spotify tab displays cover art, track title and optionally the artist when a new song starts. It returns to the Claude meter after the selected duration (10 seconds by default). Duration, scrolling speed and artist visibility are saved. Claude's solid bar colors are unchanged.

Create or open an app in the [Spotify developer dashboard](https://developer.spotify.com/dashboard), register the redirect URI shown in the tab (default `http://127.0.0.1:8080/spotify/callback`), then enter the Client ID and follow **Connecter Spotify**. Sign in using a browser on the computer running LED Meter; a headless Pi requires an SSH tunnel to its web port. [PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow) requires no client secret. Spotify credentials are stored separately in the Git-ignored `.spotify-oauth.json`. Spotify account and app access restrictions still apply; failures and rate limits are shown in the panel.

Uncheck **Afficher l'onglet Spotify** in Paramètres to hide the tab and stop both Spotify polling and LED playback, preserving credentials. Uncheck **Afficher les nouveaux morceaux sur l'écran** to pause the feature while keeping its tab visible. **Déconnecter** removes only this meter's Spotify credentials.

### Hardware

- A Raspberry Pi with Bluetooth (tested on Raspberry Pi OS, Debian based).
- A 96×16 iPixel LED matrix, driven over BLE through [pypixelcolor](https://pypi.org/project/pypixelcolor/).

### Install

```bash
git clone https://github.com/Ruben746/claude-led-screen-meter.git
cd claude-led-screen-meter
./install.sh
```

The installer:
- installs Bluetooth and avahi;
- creates the Python environment;
- copies `.env.example` to `.env`;
- creates two services: `claude-meter` for the app, `claude-meter-mdns` for the `claude-meter.local` address.

The Pi keeps its own hostname. For another name: `LED_MDNS_NAME=desk-meter ./install.sh`.

Then open the panel, click **Find displays**, pick your display and connect your Claude account.

### Quick start on Windows

For a permanent installation, double-click **Installer-Windows.bat**. The installer asks whether to start automatically at Windows sign-in (**O** for yes, **N** for no). Pressing Enter preserves the existing choice; a new installation defaults to no. It installs into `Documents/LED Meter` and creates a desktop shortcut in either case, without administrator rights. Connect your account once in this installation. Reinstalling preserves its settings and Claude/Spotify credentials.

The desktop shortcut opens the panel without a second instance. Background errors go to `meter.log` in the installation folder. Rerun the installer to change the startup choice; no removes an existing startup shortcut. You can also remove **LED Meter** from `shell:startup` (**Win+R**). For command-line installation use `install_windows.ps1 -AutoStart Yes` or `-AutoStart No`.

Double-click **Lancer.bat** with Python 3 installed. The launcher prepares the environment and installs dependencies when needed, preserves an existing `.env`, and opens the panel once the server is ready. Keep its window open; close it or press **Ctrl+C** to stop. You can test the panel without an LED display.

### Signing in to Claude

Two methods, chosen in the panel.

**Claude OAuth** (copy-and-paste code flow; persistent HTTP 429 errors remain possible).

1. Select **Claude OAuth**, then **Connect with Claude** in the panel.
2. Follow **Open Claude sign-in**, sign in to Claude and authorize the connection.
3. Copy the code Claude displays and paste it into **Authorization code** in the panel.
4. Click **Complete connection**.

Keep the panel open. The link expires after 10 minutes; after an error or restart, use **Connect with Claude** again. For HTTP 429, wait for the displayed delay before requesting a fresh code. The meter respects `Retry-After` (seconds or HTTP date), falling back to a local 120-second pause if the server supplies no usable delay. This does not guarantee success and is not retained across restarts. A failed login preserves the existing sign-in method; keep session mode if it works.

The meter stores its own credentials in `.meter-oauth.json` (Git-ignored, private permissions), automatically refreshes the access token and saves rotated refresh tokens. No Claude Code installation or cookie copying is needed. Requested scopes are `org:create_api_key user:profile`, without `user:inference`.

For an existing installation, run `git pull` and `sudo systemctl restart claude-meter`, then connect once using the new button. Existing Claude Code credentials are not overwritten. Revocation by Claude can still require signing in again.

**Claude Code (legacy, advanced)**. You can keep using a login on the Pi itself:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude        # sign in with your Claude account, then /exit
```

This legacy method requires explicitly setting `LED_OAUTH_FILE=~/.claude/.credentials.json` in `.env` and restarting the meter. By default, only `.meter-oauth.json` is used: other applications' credentials are never automatically read or refreshed. Two precautions:
- Don't copy that file from a computer that also runs Claude Code: both would fight over the same token.
- Don't run `claude logout` on the Pi.

In this mode usage is read at most once a minute.

**claude.ai session**. Paste the value of the claude.ai `sessionKey` cookie into the panel. If Cloudflare blocks the requests, also add `cf_clearance` and the exact User-Agent of the browser that cookie comes from. The organization is detected automatically. Sessions expire, so you will need to paste a fresh one now and then.

### Disclaimer

Personal project, **not affiliated with or endorsed by Anthropic**. It relies on undocumented endpoints that can change or disappear at any time. Anthropic's terms restrict OAuth tokens from Claude subscriptions to Claude Code and Claude.ai, and limit automated access to their services, so both sign-in methods fall outside what Anthropic permits. Use them at your own risk, with your own account.

The panel has no authentication by default: keep it on your local network. `LED_ADMIN_TOKEN` requires a code to change the account or the display.

### Troubleshooting

```bash
journalctl -u claude-meter -f              # live logs
sudo systemctl restart claude-meter
```

- **Find displays finds nothing**: the display may still be connected to the phone app. Close the app and search again.
- **`claude-meter.local` does not respond**: use the Pi's IP. Some networks and older Android devices don't resolve mDNS.
