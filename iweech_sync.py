#!/usr/bin/env python3
"""
iweech_sync.py — Synchronisation autonome des trajets iweech
============================================================
Récupère tous les trajets disponibles sur le vélo via BLE,
les stocke en JSON dans ~/iweech_trips/, et génère une carte
HTML interactive avec tous les trajets.

Usage:
    python3 iweech_sync.py              # sync + génère la carte
    python3 iweech_sync.py --map-only  # régénère la carte sans sync BLE
    python3 iweech_sync.py --list      # liste les trajets stockés

Dépendances:
    pip3 install bleak pycryptodome --break-system-packages
"""

import asyncio
import os
import sys
import json
import gzip
import time
import base64
import argparse
from pathlib import Path
from datetime import datetime
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from bleak import BleakClient, BleakScanner

# ── Config ────────────────────────────────────────────────────────────────────

from config import BIKE_MAC, OWNER_KEY, SUB, BIKE_ID
TRIPS_DIR = Path.home() / "iweech_trips"

CHAR_AUTH        = "10000000-0000-1000-8000-00805f9b34fb"
CHAR_USER_ID     = "10001000-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_ST    = "10000001-0000-1000-8000-00805f9b34fb"
CHAR_LAST_TRIP   = "20000001-0000-1000-8000-00805f9b34fb"
CHAR_TRIP_MD5    = "40000001-0000-1000-8000-00805f9b34fb"
CHAR_TRIPS_AVAIL = "20000002-0000-1000-8000-00805f9b34fb"
CHAR_FLUSH_TRIP  = "20000003-0000-1000-8000-00805f9b34fb"
CHAR_READ_ST     = "10000002-0000-1000-8000-00805f9b34fb"

# ── Auth BLE ──────────────────────────────────────────────────────────────────

def build_payload(sub, key):
    aes_key = os.urandom(16)
    ct = AES.new(key.encode("ascii"), AES.MODE_CBC, iv=aes_key).encrypt(
        pad(f"{int(time.time()*1000)}|{sub}".encode(), 16))
    return f"{sub}|{base64.b64encode(aes_key).decode()}|{base64.b64encode(ct).decode()}".encode()

async def authenticate(client):
    await client.write_gatt_char(CHAR_USER_ID, build_payload(SUB, OWNER_KEY), response=True)
    await asyncio.sleep(2)
    auth = bytes(await client.read_gatt_char(CHAR_AUTH))[0]
    return auth == 1

# ── Lecture d'un trajet complet (multi-chunk) ─────────────────────────────────

async def read_full_trip(client):
    chunks = []
    for attempt in range(200):
        try:
            data = bytes(await client.read_gatt_char(CHAR_LAST_TRIP))
        except Exception as e:
            print(f"  [!] Erreur lecture chunk {attempt+1}: {e}")
            if attempt == 0:
                # Premier chunk échoue → trajet probablement vide ou Pi pas prêt
                return None
            # Chunk suivant échoue → on décompresse ce qu'on a
            break
        if not data:
            break
        chunks.append(data)
        try:
            status = bytes(await client.read_gatt_char(CHAR_READ_ST)).decode("utf-8", errors="replace").strip('\x00')
        except Exception:
            status = ""
        if ":2:" not in status:
            break
    raw = b"".join(chunks)
    if not raw:
        return None
    try:
        return json.loads(gzip.decompress(raw).decode("utf-8"))
    except Exception as e:
        print(f"  [!] Décompression échouée : {e}")
        return None

# ── Sync BLE ──────────────────────────────────────────────────────────────────

async def sync_trips():
    TRIPS_DIR.mkdir(exist_ok=True)

    print(f"[*] Recherche du vélo {BIKE_MAC}...")
    dev = await BleakScanner.find_device_by_address(BIKE_MAC, timeout=15.0)
    if not dev:
        print("[-] Vélo non trouvé. Allumez-le et réessayez.")
        return []

    print(f"[+] Vélo trouvé : {dev.name}")
    new_trips = []

    async with BleakClient(BIKE_MAC, timeout=120.0) as client:
        print("[*] Authentification BLE...")
        if not await authenticate(client):
            print("[-] Authentification échouée.")
            return []
        print("[+] Authentifié (Auth=1)")

        trips_avail = int.from_bytes(bytes(await client.read_gatt_char(CHAR_TRIPS_AVAIL)), "little")
        print(f"[*] {trips_avail} trajet(s) signalé(s), lecture en boucle jusqu'à épuisement...")

        seen_md5s = set()  # éviter les boucles infinies
        i = 0
        while True:
            md5 = bytes(await client.read_gatt_char(CHAR_TRIP_MD5)).decode("utf-8", errors="replace").strip('\x00')
            if not md5 or md5 == "0" * 32:
                print("  [*] Aucun trajet disponible (MD5 vide).")
                break
            if md5 in seen_md5s:
                print(f"  [*] MD5 {md5[:8]}... déjà vu cette session — fin de la file.")
                break
            seen_md5s.add(md5)

            # Vérifier si déjà stocké (par nom de fichier ou contenu)
            existing = list(TRIPS_DIR.glob(f"*_{md5[:8]}*.json"))
            if not existing:
                # Chercher aussi dans le contenu des fichiers (cas multi-ordi)
                for f in TRIPS_DIR.glob("*.json"):
                    if f.name == "health_metrics.json":
                        continue
                    try:
                        import json as _json2
                        with open(f) as _f2:
                            _d = _json2.load(_f2)
                        if _d.get("trip_key", "") == md5 or f.stem.endswith(md5[:8]):
                            existing = [f]
                            break
                    except Exception:
                        pass
            if existing:
                # Lire le trip_id depuis le fichier existant pour pouvoir flusher correctement
                import json as _json
                with open(existing[0]) as _f:
                    _stored = _json.load(_f)
                _trip_id = _stored.get("trip_id", "").encode()
                if not _trip_id:
                    print(f"  [!] trip_id manquant dans {existing[0].name}, flush impossible")
                    print(f"  [!] Arrêt pour éviter boucle infinie")
                    break
                print(f"  [=] Trajet {md5[:8]}... déjà stocké, flush avec trip_id et suivant")
                await client.write_gatt_char(CHAR_FLUSH_TRIP, _trip_id, response=False)
                await asyncio.sleep(2)
                # Vérifier que le MD5 a changé après le flush
                new_md5 = bytes(await client.read_gatt_char(CHAR_TRIP_MD5)).decode("utf-8", errors="replace").strip('\x00')
                if new_md5 == md5:
                    print(f"  [!] MD5 inchangé après flush — le Pi n'a pas accepté le trip_id")
                    print(f"  [!] Arrêt pour éviter boucle infinie")
                    break
                continue

            print(f"\n  [>] Lecture trajet {i+1} (MD5: {md5[:8]}...)...")
            trip = await read_full_trip(client)

            if trip:
                trip_id = trip.get("trip_id", f"trip_{md5[:8]}")
                # Nom de fichier basé sur la date du trajet
                start = trip.get("start_time", "unknown")[:19].replace(":", "-").replace("T", "_")
                filename = TRIPS_DIR / f"{start}_{md5[:8]}.json"
                with open(filename, "w") as f:
                    json.dump(trip, f, indent=2)
                print(f"  [+] Sauvegardé : {filename.name}")
                print(f"      Distance: {trip.get('distance', 0)/1000:.2f} km  "
                      f"Durée: {trip.get('duration', 0)/60:.0f} min  "
                      f"Legs: {len(trip.get('legs', []))}")
                new_trips.append(trip)
            else:
                print(f"  [!] Trajet vide ou illisible")

            # Flush pour passer au suivant
            i += 1
            if True:
                if trip is None:
                    print(f"  [!] Trajet illisible, pas de flush (on réessaiera plus tard)")
                    break
                trip_id_bytes = trip.get("trip_id", "").encode()
                if not trip_id_bytes:
                    print(f"  [!] trip_id manquant, pas de flush")
                    break
                print(f"  [*] Flush trajet ({trip.get('trip_id','?')})...")
                try:
                    await client.write_gatt_char(CHAR_FLUSH_TRIP, trip_id_bytes, response=False)
                except Exception as e:
                    print(f"  [!] Erreur flush : {e} — on réessaiera plus tard")
                    break
                await asyncio.sleep(2)

                # Continuer la boucle — le prochain MD5 nous dira s'il reste des trajets

    print(f"\n[+] Sync terminée. {len(new_trips)} nouveau(x) trajet(s) récupéré(s).")
    return new_trips

# ── Chargement des trajets stockés ───────────────────────────────────────────

def load_all_trips():
    if not TRIPS_DIR.exists():
        return []
    trips = []
    skip = {"health_metrics.json"}
    for f in sorted(TRIPS_DIR.glob("*.json")):
        if f.name in skip:
            continue
        try:
            with open(f) as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                print(f"[!] Ignoré (format inattendu) : {f.name}")
                continue
            if "legs" not in data:
                print(f"[!] Ignoré (pas de legs) : {f.name}")
                continue
            trips.append(data)
        except Exception as e:
            print(f"[!] Erreur lecture {f.name}: {e}")
    return trips

# ── Génération de la carte HTML ───────────────────────────────────────────────

COLORS = [
    "#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6",
    "#1abc9c","#e67e22","#34495e","#e91e63","#00bcd4",
]

def speed_color(speed):
    stops = [(33,150,243),(76,175,80),(255,152,0),(244,67,54)]
    t = max(0, min(1, speed / 28))
    si = min(int(t * (len(stops)-1)), len(stops)-2)
    st = t * (len(stops)-1) - si
    a, b = stops[si], stops[si+1]
    return f"#{int(a[0]+(b[0]-a[0])*st):02x}{int(a[1]+(b[1]-a[1])*st):02x}{int(a[2]+(b[2]-a[2])*st):02x}"

def generate_map(trips, output_path):
    bike_id = BIKE_ID or "iweech"
    if not trips:
        print("[!] Aucun trajet à afficher.")
        return

    from datetime import datetime, timedelta
    UTC_OFFSET = 2  # Paris été (CEST = UTC+2)

    def local_time(iso):
        """Convertit un timestamp UTC iweech en heure locale Paris."""
        try:
            dt = datetime.fromisoformat(iso)
            dt = dt + timedelta(hours=UTC_OFFSET)
            return dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            return iso[:16].replace("T", " ")

    def local_hhmm(iso):
        try:
            dt = datetime.fromisoformat(iso)
            dt = dt + timedelta(hours=UTC_OFFSET)
            return dt.strftime("%H:%M")
        except Exception:
            return iso[11:16]

    # Trier par date décroissante (plus récent en premier)
    def trip_sort_key(t):
        try:
            return datetime.fromisoformat(t.get("start_time", "1970-01-01"))
        except Exception:
            return datetime(1970, 1, 1)
    trips_sorted = sorted(trips, key=trip_sort_key, reverse=True)

    # Préparer les données pour JS
    trips_js = []
    for i, trip in enumerate(trips_sorted):
        legs = trip.get("legs", [])
        if not legs:
            continue
        color = COLORS[i % len(COLORS)]
        start_local = local_time(trip.get("start_time", ""))
        dist_km = trip.get("distance", 0) / 1000
        dur_min = trip.get("duration", 0) / 60
        # Extraire année/mois/jour pour le groupement
        try:
            dt = datetime.fromisoformat(trip.get("start_time", "")) + timedelta(hours=UTC_OFFSET)
            year  = dt.strftime("%Y")
            month = dt.strftime("%Y-%m")
            month_label = dt.strftime("%B %Y")
            day   = dt.strftime("%Y-%m-%d")
            day_label = dt.strftime("%d/%m/%Y")
            time_label = dt.strftime("%H:%M")
        except Exception:
            year = month = month_label = day = day_label = time_label = "?"
        trips_js.append({
            "id": trip.get("trip_id", f"trip_{i}"),
            "label": f"{start_local} · {dist_km:.1f} km · {dur_min:.0f} min",
            "time_label": time_label,
            "day_label": day_label,
            "month_label": month_label,
            "year": year,
            "month": month,
            "day": day,
            "color": color,
            "dist": round(dist_km, 2),
            "dur": round(dur_min),
            "max_speed": round(trip.get("max_speed", 0) * 3.6, 1),
            "avg_cyclist_power": round(trip.get("avg_cyclist_power", 0)),
            "ascent": round(trip.get("ascent", 0)),
            "legs": [{"lat": l.get("start_lat", l.get("lat")),
                      "lon": l.get("start_lon", l.get("lon")),
                      "speed": round(l.get("avg_speed", 0) * 3.6, 1),
                      "cp": round(l.get("avg_cyclist_power", l.get("cyclist_power", 0))),
                      "bp": round(l.get("avg_battery_power", l.get("battery_power", 0))),
                      "slope": round(l.get("slope", 0), 1),
                      "t": local_hhmm(l.get("start_time", ""))}
                     for l in legs
                     if abs(l.get("start_lat", l.get("lat", 0))) > 1
                     and abs(l.get("start_lon", l.get("lon", 0))) > 0.01]
        })

    trips_json = json.dumps(trips_js)

    total_km = sum(t.get("distance", 0) for t in trips) / 1000
    total_min = sum(t.get("duration", 0) for t in trips) / 60

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>iweech {bike_id} — Mes trajets</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f0efe9; color: #222; display: flex; flex-direction: column; height: 100vh; }}
#header {{ padding: 10px 16px; background: #fff; border-bottom: 1px solid #ddd; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
#header h1 {{ font-size: 14px; font-weight: 500; }}
.stat {{ font-size: 12px; color: #888; }}
.stat b {{ color: #222; font-weight: 500; }}
#controls {{ display: flex; align-items: center; gap: 6px; margin-left: auto; }}
.ctrl-label {{ font-size: 11px; color: #888; }}
.btn {{ font-size: 11px; padding: 3px 9px; border: 1px solid #ccc; border-radius: 5px; background: #fff; cursor: pointer; color: #444; }}
.btn.active {{ background: #222; color: #fff; border-color: #222; }}
#legend {{ display: flex; align-items: center; gap: 6px; }}
.legend-bar {{ height: 8px; width: 100px; border-radius: 3px; background: linear-gradient(to right, #2196F3, #4CAF50, #FF9800, #f44336); }}
.legend-labels {{ display: flex; justify-content: space-between; width: 100px; font-size: 9px; color: #888; margin-top: 1px; }}
#main {{ display: flex; flex: 1; overflow: hidden; }}
#sidebar {{ width: 270px; min-width: 270px; background: #fff; border-right: 1px solid #ddd; overflow-y: auto; }}
#sidebar-header {{ padding: 8px 14px; border-bottom: 1px solid #eee; font-size: 11px; color: #888; }}
.group-year {{ border-bottom: 1px solid #e8e8e0; }}
.group-year-header {{ padding: 9px 14px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; user-select: none; background: #f5f4ee; }}
.group-year-header:hover {{ background: #eeeee8; }}
.group-year-label {{ font-size: 13px; font-weight: 500; color: #222; }}
.group-year-meta {{ font-size: 11px; color: #888; }}
.group-year-chevron {{ font-size: 10px; color: #aaa; transition: transform .2s; }}
.group-year.open .group-year-chevron {{ transform: rotate(90deg); }}
.group-year-body {{ display: none; }}
.group-year.open .group-year-body {{ display: block; }}
.group-month {{ border-bottom: 1px solid #f0f0f0; }}
.group-month-header {{ padding: 7px 14px 7px 22px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; user-select: none; }}
.group-month-header:hover {{ background: #f8f8f6; }}
.group-month-label {{ font-size: 12px; font-weight: 500; color: #444; }}
.group-month-meta {{ font-size: 11px; color: #aaa; }}
.group-month-chevron {{ font-size: 10px; color: #ccc; transition: transform .2s; }}
.group-month.open .group-month-chevron {{ transform: rotate(90deg); }}
.group-month-body {{ display: none; }}
.group-month.open .group-month-body {{ display: block; }}
.group-day {{ border-bottom: 1px solid #f5f5f5; }}
.group-day-header {{ padding: 5px 14px 5px 30px; cursor: pointer; display: flex; align-items: center; justify-content: space-between; user-select: none; color: #666; font-size: 11px; }}
.group-day-header:hover {{ background: #fafaf8; }}
.group-day-chevron {{ font-size: 9px; color: #ccc; transition: transform .2s; }}
.group-day.open .group-day-chevron {{ transform: rotate(90deg); }}
.group-day-body {{ display: none; }}
.group-day.open .group-day-body {{ display: block; }}
.trip-item {{ padding: 7px 14px 7px 38px; border-bottom: 1px solid #f5f5f5; cursor: pointer; transition: background .1s; display: flex; align-items: center; justify-content: space-between; }}
.trip-item:hover {{ background: #f8f8f6; }}
.trip-item.active {{ background: #eef3ff; }}
.trip-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; flex-shrink: 0; }}
.trip-time {{ font-size: 12px; font-weight: 500; color: #222; }}
.trip-meta {{ font-size: 11px; color: #888; text-align: right; }}
#map {{ flex: 1; position: relative; }}
#detail {{ position: absolute; bottom: 12px; right: 12px; background: rgba(255,255,255,.96); border-radius: 10px; border: 1px solid #ddd; padding: 12px 16px; font-size: 12px; min-width: 175px; z-index: 500; display: none; }}
#detail h3 {{ font-size: 13px; font-weight: 500; margin-bottom: 8px; }}
.drow {{ display: flex; justify-content: space-between; gap: 16px; margin: 3px 0; color: #444; }}
.drow span:first-child {{ color: #888; }}
#tooltip {{ position: fixed; background: rgba(0,0,0,.8); color: #fff; padding: 6px 10px; border-radius: 6px; font-size: 11px; pointer-events: none; display: none; z-index: 9999; line-height: 1.6; }}
</style>
</head>
<body>
<div id="header">
  <h1>🚲 iweech {bike_id}</h1>
  <span class="stat"><b>{len(trips_js)}</b> trajets</span>
  <span class="stat"><b>{total_km:.1f} km</b> total</span>
  <span class="stat"><b>{total_min:.0f} min</b></span>
  <div id="controls">
    <span class="ctrl-label">Couleur :</span>
    <button class="btn active" id="btn-speed">Vitesse</button>
    <button class="btn" id="btn-cyclist_power">Effort</button>
    <button class="btn" id="btn-slope">Pente</button>
    <div id="legend">
      <div>
        <div class="legend-bar" id="legend-bar"></div>
        <div class="legend-labels"><span id="leg-min">0</span><span id="leg-max">28 km/h</span></div>
      </div>
    </div>
  </div>
</div>
<div id="main">
  <div id="sidebar">
    <div id="sidebar-header">↓ Cliquez pour déplier · Échap pour fermer</div>
    <div id="trip-list"></div>
  </div>
  <div id="map"></div>
  <div id="detail"></div>
</div>
<div id="tooltip"></div>

<script>
const TRIPS = {trips_json};
</script>
<script>
const MODES = {{
  speed:          {{ label: 'Vitesse',  min: 0,   max: 28,  unit: 'km/h', get: l => l.speed }},
  cyclist_power:  {{ label: 'Effort',   min: 0,   max: 220, unit: 'W',    get: l => l.cp }},
  slope:          {{ label: 'Pente',    min: -8,  max: 9,   unit: '%',    get: l => l.slope }},
}};
let colorMode = 'speed';

function getColor(val, mode) {{
  const m = MODES[mode];
  if (mode === 'slope') {{
    if (val < 0) {{
      const s = Math.min(1, Math.abs(val) / 8);
      return `rgb(${{Math.round(33+s*144)}},${{Math.round(150-s*117)}},${{Math.round(243-s*210)}})`;
    }}
    const s = Math.min(1, val / 9);
    return `rgb(${{Math.round(33+s*211)}},${{Math.round(150-s*106)}},33)`;
  }}
  const stops = [[33,150,243],[76,175,80],[255,152,0],[244,67,54]];
  const t = Math.max(0, Math.min(1, (val - m.min) / (m.max - m.min)));
  const si = Math.min(Math.floor(t*(stops.length-1)), stops.length-2);
  const st = t*(stops.length-1)-si;
  const a=stops[si], b=stops[si+1];
  return `rgb(${{Math.round(a[0]+(b[0]-a[0])*st)}},${{Math.round(a[1]+(b[1]-a[1])*st)}},${{Math.round(a[2]+(b[2]-a[2])*st)}})`;
}}

function updateLegend(mode) {{
  const m = MODES[mode];
  document.getElementById('leg-min').textContent = m.min + ' ' + m.unit;
  document.getElementById('leg-max').textContent = m.max + ' ' + m.unit;
  if (mode === 'slope') {{
    document.getElementById('legend-bar').style.background =
      'linear-gradient(to right, #2196F3, #4CAF50, #e65100)';
  }} else {{
    document.getElementById('legend-bar').style.background =
      'linear-gradient(to right, #2196F3, #4CAF50, #FF9800, #f44336)';
  }}
}}

let map, currentPolylines=[], currentIdx=null;
const tooltip = document.getElementById('tooltip');
const detail = document.getElementById('detail');

function drawTrip(idx) {{
  // Supprimer le tracé précédent
  currentPolylines.forEach(pl => map.removeLayer(pl));
  currentPolylines = [];

  const trip = TRIPS[idx];
  const pls = [];
  for (let i = 0; i < trip.legs.length - 1; i++) {{
    const a = trip.legs[i], b = trip.legs[i+1];
    const val = MODES[colorMode].get(a);
    const pl = L.polyline([[a.lat,a.lon],[b.lat,b.lon]], {{
      color: getColor(val, colorMode), weight: 5, opacity: 0.95
    }}).addTo(map);
    pl.on('mouseover', () => {{
      tooltip.style.display = 'block';
      tooltip.innerHTML = `${{a.t}} · ${{a.speed}} km/h · ${{a.cp}}W · pente ${{a.slope}}%`;
    }});
    pl.on('mousemove', e => {{
      tooltip.style.left = (e.originalEvent.clientX+12)+'px';
      tooltip.style.top = (e.originalEvent.clientY-8)+'px';
    }});
    pl.on('mouseout', () => {{ tooltip.style.display='none'; }});
    pls.push(pl);
  }}
  currentPolylines = pls;
}}

function recolorCurrent() {{
  if (currentIdx === null) return;
  const trip = TRIPS[currentIdx];
  currentPolylines.forEach((pl, i) => {{
    const a = trip.legs[i];
    if (!a) return;
    pl.setStyle({{ color: getColor(MODES[colorMode].get(a), colorMode) }});
  }});
}}

function setMode(mode) {{
  colorMode = mode;
  document.querySelectorAll('.btn').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-' + mode).classList.add('active');
  updateLegend(mode);
  recolorCurrent();
}}

function initMap() {{
  map = L.map('map').setView([48.864, 2.253], 13);
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '© OpenStreetMap © CARTO', maxZoom: 19, subdomains: 'abcd'
  }}).addTo(map);

  const list = document.getElementById('trip-list');

  // ── Construire la sidebar arborescente ──────────────────────────────────
  // Grouper : année → mois → jour → trajets
  const tree = {{}};
  TRIPS.forEach((trip, idx) => {{
    const y = trip.year, m = trip.month, d = trip.day;
    if (!tree[y]) tree[y] = {{}};
    if (!tree[y][m]) tree[y][m] = {{}};
    if (!tree[y][m][d]) tree[y][m][d] = [];
    tree[y][m][d].push(idx);
  }});

  function toggle(el, bodyEl) {{
    el.classList.toggle('open');
  }}

  const years = Object.keys(tree).sort((a,b) => b-a);
  years.forEach(y => {{
    const months = Object.keys(tree[y]).sort((a,b) => b.localeCompare(a));
    const totalY = months.reduce((s,m) => s + Object.values(tree[y][m]).reduce((s2,d)=>s2+d.length,0), 0);

    const yDiv = document.createElement('div');
    yDiv.className = 'group-year';
    yDiv.innerHTML = `
      <div class="group-year-header">
        <span class="group-year-label">${{y}}</span>
        <span style="display:flex;align-items:center;gap:8px">
          <span class="group-year-meta">${{totalY}} trajet${{totalY>1?'s':''}}</span>
          <span class="group-year-chevron">▶</span>
        </span>
      </div>
      <div class="group-year-body"></div>`;
    const yHeader = yDiv.querySelector('.group-year-header');
    const yBody   = yDiv.querySelector('.group-year-body');
    yHeader.addEventListener('click', () => yDiv.classList.toggle('open'));

    months.forEach(m => {{
      const days = Object.keys(tree[y][m]).sort((a,b) => b.localeCompare(a));
      const totalM = days.reduce((s,d) => s+tree[y][m][d].length, 0);
      const mLabel = TRIPS[tree[y][m][days[0]][0]].month_label;

      const mDiv = document.createElement('div');
      mDiv.className = 'group-month';
      mDiv.innerHTML = `
        <div class="group-month-header">
          <span class="group-month-label">${{mLabel}}</span>
          <span style="display:flex;align-items:center;gap:6px">
            <span class="group-month-meta">${{totalM}} trajet${{totalM>1?'s':''}}</span>
            <span class="group-month-chevron">▶</span>
          </span>
        </div>
        <div class="group-month-body"></div>`;
      const mHeader = mDiv.querySelector('.group-month-header');
      const mBody   = mDiv.querySelector('.group-month-body');
      mHeader.addEventListener('click', () => mDiv.classList.toggle('open'));

      days.forEach(d => {{
        const idxs = tree[y][m][d];
        const dLabel = TRIPS[idxs[0]].day_label;

        const dDiv = document.createElement('div');
        dDiv.className = 'group-day';
        dDiv.innerHTML = `
          <div class="group-day-header">
            <span>${{dLabel}} — ${{idxs.length}} trajet${{idxs.length>1?'s':''}}</span>
            <span class="group-day-chevron">▶</span>
          </div>
          <div class="group-day-body"></div>`;
        const dHeader = dDiv.querySelector('.group-day-header');
        const dBody   = dDiv.querySelector('.group-day-body');
        dHeader.addEventListener('click', () => dDiv.classList.toggle('open'));

        idxs.forEach(idx => {{
          const trip = TRIPS[idx];
          const item = document.createElement('div');
          item.className = 'trip-item';
          item.id = 'trip-item-' + idx;
          item.innerHTML = `
            <span><span class="trip-dot" style="background:${{trip.color}}"></span>
            <span class="trip-time">${{trip.time_label}}</span></span>
            <span class="trip-meta">${{trip.dist}} km · ${{trip.dur}} min</span>`;
          item.addEventListener('click', () => focusTrip(idx));
          dBody.appendChild(item);
        }});
        mBody.appendChild(dDiv);
      }});
      yBody.appendChild(mDiv);
    }});
    list.appendChild(yDiv);
  }});

  // Ouvrir automatiquement l'année et le mois les plus récents
  const firstYear = list.querySelector('.group-year');
  if (firstYear) {{
    firstYear.classList.add('open');
    const firstMonth = firstYear.querySelector('.group-month');
    if (firstMonth) firstMonth.classList.add('open');
  }}

  // Marqueurs départ/arrivée sur la carte
  TRIPS.forEach((trip, idx) => {{
    const first = trip.legs[0], last = trip.legs[trip.legs.length-1];
    if (first) L.circleMarker([first.lat,first.lon],
      {{radius:4, color:'#fff', fillColor:trip.color, fillOpacity:1, weight:2,
        title: trip.label}}).addTo(map)
      .on('click', () => focusTrip(idx));
    if (last) L.circleMarker([last.lat,last.lon],
      {{radius:3, color:'#fff', fillColor:'#555', fillOpacity:1, weight:2}}).addTo(map)
      .on('click', () => focusTrip(idx));
  }});

  updateLegend(colorMode);
  ['speed','cyclist_power','slope'].forEach(m => {{
    document.getElementById('btn-'+m).addEventListener('click', () => setMode(m));
  }});

  // Afficher le trajet le plus récent par défaut
  if (TRIPS.length > 0) focusTrip(0);
}}

function focusTrip(idx) {{
  currentIdx = idx;
  drawTrip(idx);
  document.querySelectorAll('.trip-item').forEach(el => el.classList.remove('active'));
  const activeItem = document.getElementById('trip-item-' + idx);
  if (activeItem) {{
    activeItem.classList.add('active');
    // Ouvrir les groupes parents si repliés
    let p = activeItem.parentElement;
    while (p && p.id !== 'trip-list') {{
      if (p.classList.contains('group-day') || p.classList.contains('group-month') || p.classList.contains('group-year')) {{
        p.classList.add('open');
      }}
      p = p.parentElement;
    }}
    activeItem.scrollIntoView({{block: 'nearest'}});
  }}
  const trip = TRIPS[idx];
  detail.style.display = 'block';
  detail.innerHTML = `<h3>${{trip.label.split('·')[0].trim()}}</h3>
    <div class="drow"><span>Distance</span><span>${{trip.dist}} km</span></div>
    <div class="drow"><span>Durée</span><span>${{trip.dur}} min</span></div>
    <div class="drow"><span>Vitesse max</span><span>${{trip.max_speed}} km/h</span></div>
    <div class="drow"><span>Puissance cycliste</span><span>${{trip.avg_cyclist_power}} W moy.</span></div>
    <div class="drow"><span>Dénivelé +</span><span>${{trip.ascent}} m</span></div>`;
  const latlngs = trip.legs.map(l => [l.lat,l.lon]);
  if (latlngs.length) map.fitBounds(latlngs, {{padding:[40,40]}});
}}

document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{
    currentPolylines.forEach(pl => map.removeLayer(pl));
    currentPolylines = [];
    currentIdx = null;
    document.querySelectorAll('.trip-item').forEach(el => el.classList.remove('active'));
    detail.style.display = 'none';
  }}
}});
</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" onload="initMap()"></script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)
    print(f"[+] Carte générée : {output_path}")

# ── Main ──────────────────────────────────────────────────────────────────────

def list_trips():
    trips = load_all_trips()
    if not trips:
        print(f"Aucun trajet dans {TRIPS_DIR}")
        return
    print(f"\n{len(trips)} trajet(s) dans {TRIPS_DIR}:\n")
    for t in trips:
        start = t.get("start_time","?")[:16].replace("T"," ")
        dist = t.get("distance",0)/1000
        dur = t.get("duration",0)/60
        legs = len(t.get("legs",[]))
        print(f"  {start}  {dist:5.1f} km  {dur:4.0f} min  ({legs} segments)")
    print(f"\nTotal: {sum(t.get('distance',0) for t in trips)/1000:.1f} km")

async def main():
    parser = argparse.ArgumentParser(description="iweech trip sync")
    parser.add_argument("--map-only", action="store_true", help="Régénérer la carte sans sync BLE")
    parser.add_argument("--list", action="store_true", help="Lister les trajets stockés")
    args = parser.parse_args()

    if args.list:
        list_trips()
        return

    map_path = TRIPS_DIR / "carte.html"
    TRIPS_DIR.mkdir(exist_ok=True)

    if not args.map_only:
        await sync_trips()

    trips = load_all_trips()
    print(f"\n[*] {len(trips)} trajet(s) total en base locale")
    generate_map(trips, map_path)
    print(f"\nOuvrez la carte : file://{map_path}")

if __name__ == "__main__":
    asyncio.run(main())
