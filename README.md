# iweech-bluetooth

> **L'application mobile Iweech n'est plus maintenue et plus disponible. La société Bellatrix, fabricante des vélos, est en redressement judiciaire.
>
> Ce dépôt fournit des outils Python pour continuer à utiliser votre vélo iweech de façon autonome via Bluetooth LE, sans dépendre de l'application ni des serveurs iweech.

## Fonctionnalités

- 🔒 **Verrouiller / déverrouiller** le vélo via BLE
- 🗺️ **Synchroniser et visualiser les trajets** sur une carte interactive (code couleur par vitesse, effort ou pente)
- 📊 **Dashboard de santé** (charge batterie, consommation Wh/km, cycles estimés, erreurs moteur...)
- 📥 **Récupérer l'historique des trajets** depuis l'API iweech (tant que les serveurs répondent)
- 🗓️ Trajets organisés par année → mois → jour dans la barre latérale

## Prérequis

```bash
pip3 install bleak pycryptodome
```

Python 3.9+ requis. Testé sur Linux. Devrait fonctionner sur macOS et Windows.

## Configuration

Vous avez besoin de deux valeurs spécifiques à votre vélo :

- **`owner_key`** — une clé AES de 16 caractères hexadécimaux, associée à votre vélo
- **`sub`** — l'UUID de votre compte iweech

### Comment les trouver (tant que les serveurs sont en ligne)

> **Note :** Le `client_secret` ci-dessous est codé en dur dans l'APK Android iweech (`com.bellatrix.iweech`) et peut être retrouvé par n'importe qui en décompilant l'application avec jadx. Ce n'est pas un secret personnel.

```bash
# 1. Obtenir un token d'accès
ACCESS_TOKEN=$(curl -s -X POST \
  "https://accounts.iweech.com/auth/realms/iweech/protocol/openid-connect/token" \
  -d "client_id=mobile-app" \
  -d "client_secret=adf52c9f-a4ed-41bf-9cf7-ec6cd5c65693" \
  -d "grant_type=password" \
  -d "username=VOTRE_EMAIL" \
  -d "password=VOTRE_MOT_DE_PASSE" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 2. Récupérer l'ownerKey (remplacez IW-XXXX par l'ID de votre vélo, inscrit sur le cadre)
curl -s "https://api.iweech.com/bam/bike/IW-XXXX" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('owner_key:', d['ownerKey'])"

# 3. Récupérer votre sub (UUID utilisateur, aussi lisible dans le JWT)
echo $ACCESS_TOKEN | cut -d. -f2 | base64 -d 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('sub:', d['sub'])"
```

### Créer le fichier de configuration

Copiez `config.json.example` en `config.json` et renseignez vos valeurs :

```bash
cp config.json.example config.json
```

```json
{
  "bike_mac":  "B8:27:EB:XX:XX:XX",
  "bike_id":   "IW-XXXX",
  "owner_key": "votre_clé_16_car",
  "sub":       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

> ⚠️ **Ne commitez jamais `config.json`** dans un dépôt public. Il est listé dans `.gitignore`.

L'adresse MAC Bluetooth de votre vélo commence par `B8:27:EB:` (préfixe Raspberry Pi Foundation) et est visible depuis n'importe quel outil de scan BLE (`bluetoothctl scan on`).

## Scripts

### `iweech_lock.py` — Verrouiller / déverrouiller

```bash
python3 iweech_lock.py status    # état actuel du verrou
python3 iweech_lock.py lock      # verrouiller
python3 iweech_lock.py unlock    # déverrouiller
```

### `iweech_sync.py` — Synchroniser les trajets et générer la carte

```bash
python3 iweech_sync.py              # sync via BLE + génération carte
python3 iweech_sync.py --map-only   # régénérer la carte sans sync BLE
python3 iweech_sync.py --list       # lister les trajets stockés
```

Les trajets sont stockés en JSON dans `~/iweech_trips/` et affichés sur une carte Leaflet interactive (`~/iweech_trips/carte.html`).

La carte permet de :
- Naviguer dans les trajets via une sidebar année → mois → jour
- Colorier les segments par vitesse, effort cycliste ou pente
- Cliquer sur un trajet pour l'afficher ; Échap pour effacer

### `iweech_monitor.py` — Dashboard de santé

```bash
python3 iweech_monitor.py              # collecte métriques + génère dashboard
python3 iweech_monitor.py --dash-only  # régénère le dashboard sans BLE
python3 iweech_monitor.py --once       # collecte uniquement, sans dashboard
```

Ouvre `~/iweech_trips/dashboard.html`. Suit dans le temps :
- Charge batterie %
- Consommation (Wh/km) — une hausse progressive indique une dégradation de la batterie
- Cycles de charge estimés
- Codes d'erreur moteur et BMS
- Versions firmware, statut WiFi, état du verrou

**Automatisation via cron** (chaque jour à 8h, le vélo doit être allumé et à portée) :
```
0 8 * * * cd ~/iweech && python3 iweech_monitor.py >> ~/iweech_trips/monitor.log 2>&1
```

### `iweech_fetch_history.py` — Récupérer l'historique depuis le serveur

> ⚠️ **À faire maintenant**, tant que les serveurs sont en ligne.

Nécessite un cookie de session récupéré depuis Firefox (voir la docstring du script pour les instructions détaillées).

```bash
python3 iweech_fetch_history.py --cookie "cf_clearance=...; kc-access=..."
python3 iweech_fetch_history.py --cookie-file cookie.txt --dry-run  # aperçu
python3 iweech_fetch_history.py --cookie-file cookie.txt            # télécharger tout
```

## Fonctionnement de l'authentification BLE

*Obtenu par rétro-ingénierie de l'APK Android iweech (`com.bellatrix.iweech`) avec jadx.*

Le vélo expose le service UUID `40000000-0000-1000-8000-00805f9b34fb` (firmware v5+).

Le payload d'authentification à écrire sur `UserID` (`10001000-...`) :

```
sub + "|" + base64(clé_AES_aléatoire) + "|" + base64(AES_CBC(timestamp_ms|sub, iv=clé_AES, key=owner_key))
```

Si accepté, `Auth` (`10000000-...`) passe de `0` à `1`.

## Caractéristiques BLE principales

Les UUIDs complets sont de la forme `XXXXXXXX-0000-1000-8000-00805f9b34fb`.

| Nom | Suffixe UUID | Mode | Format | Notes |
|-----|-------------|------|--------|-------|
| Auth | 10000000 | READ/NOTIFY | uint8 | 0=non auth, 1=auth ok |
| UserID | 10001000 | WRITE | utf8 | payload d'authentification |
| WriteStatus | 10000001 | READ/NOTIFY | utf8 | `uuid:code:message` |
| Lock | 10002000 | READ/NOTIFY | uint8 | 1=verrouillé, 2=déverrouillé |
| SetLock | 10002100 | WRITE | uint8 | 1=lock, 2=unlock |
| BatteryPercent | 10002003 | READ/NOTIFY | uint8 | 0–100 |
| Speed | 10002005 | READ/NOTIFY | int32 | m/s |
| WifiStatus | 10070000 | READ/NOTIFY | utf8 | `connecté:WAN:ssid` |
| WifiList | 10070005 | READ | utf8 | JSON `[{ssid, psk}]` |
| LastTrip | 20000001 | READ | binaire | gzip JSON, multi-chunk |
| TripsAvailable | 20000002 | READ/NOTIFY | uint32 | nombre de trajets en attente |
| FlushTrip | 20000003 | WRITE | utf8 | trip_id pour valider la lecture |
| LastTripMD5 | 40000001 | READ | utf8 | MD5 du trajet courant |

## Format des données de trajet

Chaque trajet est un JSON compressé gzip contenant une liste de `legs` (segments de ~5–8 secondes) :

```json
{
  "seq_id": 0,
  "start_lat": 48.12345,
  "start_lon": 2.12345,
  "avg_speed": 7.1,
  "avg_cyclist_power": 34.0,
  "avg_battery_power": 228.0,
  "slope": -1.77,
  "distance": 33.3,
  "duration": 7.2,
  "start_time": "2024-01-15T09:00:00"
}
```

La lecture d'un trajet est multi-chunk : continuer à lire `LastTrip` tant que `ReadStatus` contient le code `2` (buffer plein). Valider avec le `trip_id` exact écrit sur `FlushTrip`.

## Compatibilité

Testé sur iweech 24S avec firmware `brain-v6.2.9.1-prod` / `gatt-v1.5.0-prod`.

Les autres modèles iweech utilisent vraisemblablement le même protocole. Les versions antérieures du firmware utilisaient un UUID de service différent (`89e0ffda-5a56-47f4-b475-dff3423fac08`) avec des UUIDs de caractéristiques différents — le mécanisme d'authentification est identique.

## Licence

MIT

## Avertissement

Ce projet n'est pas affilié à iweech ni à Bellatrix. Il a été développé de façon indépendante pour un usage personnel et pour permettre aux propriétaires de continuer à utiliser un matériel qu'ils ont légitimement acheté. Utilisez-le à vos propres risques.
