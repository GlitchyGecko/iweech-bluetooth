#!/usr/bin/env python3
"""
iweech_fetch_history.py — Récupération des trajets historiques depuis l'API iweech
===================================================================================
Récupère tous les trajets stockés sur le serveur iweech et les convertit
au format JSON compatible avec iweech_sync.py.

Usage:
    python3 iweech_fetch_history.py --cookie "cf_clearance=...; kc-access=..."
    python3 iweech_fetch_history.py --cookie-file cookie.txt

Le cookie s'obtient depuis Firefox DevTools (F12) → Réseau → requête
synthesis → En-têtes → Cookie (valeur complète).
"""

import sys
import json
import time
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timedelta
from config import BIKE_ID, SUB

TRIPS_DIR  = Path.home() / "iweech_trips"
BASE_URL   = "https://api.iweech.com"
UTC_OFFSET = 2  # Paris été (CEST)

def api_get(path, cookie):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def local_dt(iso_str):
    """Convertit un timestamp UTC iweech en datetime local Paris."""
    iso_str = iso_str.replace("+00:00", "").strip()
    try:
        dt = datetime.fromisoformat(iso_str) + timedelta(hours=UTC_OFFSET)
    except Exception:
        dt = datetime.now()
    return dt

def convert_to_ble_format(details, summary):
    """
    Convertit le format API (colonnes parallèles) en format BLE (liste d'objets),
    compatible avec iweech_sync.py et la carte HTML.
    """
    legs_raw = details["legs"]
    n = len(legs_raw["seq_id"])

    legs = []
    for i in range(n):
        coord = legs_raw["coords"][i] if i < len(legs_raw.get("coords", [])) else [0, 0]
        lat, lon = (coord[0], coord[1]) if len(coord) >= 2 else (0, 0)

        # Distance du segment (différence entre points cumulés)
        cum = legs_raw.get("cum_dist", [])
        seg_dist = (cum[i] - cum[i-1]) * 1000 if i > 0 and i < len(cum) else (cum[0] * 1000 if cum else 0)

        # Durée du segment
        dur = legs_raw.get("cum_duration", [])
        seg_dur = (dur[i] - dur[i-1]) if i > 0 and i < len(dur) else (dur[0] if dur else 0)

        # Vitesse en m/s (API donne km/h)
        spd_kmh = legs_raw.get("avg_speed", [])[i] if i < len(legs_raw.get("avg_speed", [])) else 0
        spd_ms  = spd_kmh / 3.6

        # Altitude → pente approx (diff alt / dist)
        alt = legs_raw.get("alt", [])
        if i > 0 and i < len(alt) and seg_dist > 0:
            slope = round((alt[i] - alt[i-1]) / seg_dist * 100, 2)
        else:
            slope = 0.0

        legs.append({
            "seq_id":              int(legs_raw["seq_id"][i]),
            "start_lat":           round(lat, 6),
            "start_lon":           round(lon, 6),
            "avg_speed":           round(spd_ms, 3),
            "avg_cyclist_power":   legs_raw.get("avg_cyclist_power", [])[i] if i < len(legs_raw.get("avg_cyclist_power", [])) else 0,
            "avg_battery_power":   legs_raw.get("avg_battery_power", [])[i] if i < len(legs_raw.get("avg_battery_power", [])) else 0,
            "slope":               slope,
            "distance":            round(seg_dist, 1),
            "duration":            round(seg_dur, 1),
            "alt":                 alt[i] if i < len(alt) else 0,
            "start_time":          "",  # pas disponible par segment dans l'API
        })

    # Reconstruire le trip_id au format iweech
    dt = local_dt(summary["start_time"])
    trip_id = f"{BIKE_ID}_{dt.strftime('%Y-%m-%d_%H%M%S')}.000000"

    return {
        "trip_id":           trip_id,
        "user_id":           SUB,
        "bike_id":           summary.get("bike_id", BIKE_ID),
        "start_time":        (local_dt(summary["start_time"]) - timedelta(hours=UTC_OFFSET)).isoformat(),
        "start_lat":         legs[0]["start_lat"] if legs else 0,
        "start_lon":         legs[0]["start_lon"] if legs else 0,
        "end_lat":           legs[-1]["start_lat"] if legs else 0,
        "end_lon":           legs[-1]["start_lon"] if legs else 0,
        "distance":          summary["distance"] * 1000,      # km → m
        "duration":          summary["duration"],
        "ascent":            summary.get("ascent", 0),
        "descent":           0,
        "max_speed":         summary.get("avg_speed", 0) / 3.6,  # approx
        "avg_speed":         summary.get("avg_speed", 0) / 3.6,
        "avg_cyclist_power": summary.get("cyclist_energy", 0) / max(summary.get("duration", 1), 1),
        "avg_battery_power": summary.get("battery_energy", 0) / max(summary.get("duration", 1), 1),
        "cyclist_energy":    summary.get("cyclist_energy", 0) * 1000,
        "battery_energy":    summary.get("battery_energy", 0),
        "source":            "api",   # marqueur pour distinguer des trajets BLE
        "trip_key":          summary.get("trip_key", ""),
        "legs":              legs,
    }

def fetch_all(cookie, limit=None, dry_run=False):
    TRIPS_DIR.mkdir(exist_ok=True)

    print("[*] Récupération de la liste des trajets...")
    data = api_get("/history/trips/synthesis", cookie)
    trips_list = data.get("trips", [])
    print(f"[+] {len(trips_list)} trajet(s) trouvé(s) sur le serveur")

    if limit:
        trips_list = trips_list[:limit]
        print(f"[*] Limité aux {limit} premiers")

    new_count = 0
    skip_count = 0
    error_count = 0

    for i, summary in enumerate(trips_list):
        trip_key = summary.get("trip_key", "")
        dt       = local_dt(summary["start_time"])
        label    = dt.strftime("%d/%m/%Y %H:%M")
        dist_km  = summary.get("distance", 0)
        dur_min  = summary.get("duration", 0) / 60

        # Vérifier si déjà téléchargé (par trip_key dans les fichiers existants)
        existing = list(TRIPS_DIR.glob(f"*{trip_key[:8]}*.json"))
        if not existing:
            # Chercher aussi par date approx
            date_str = dt.strftime("%Y-%m-%d_%H-%M")
            existing = list(TRIPS_DIR.glob(f"{date_str}*.json"))

        if existing:
            print(f"  [{i+1:3d}/{len(trips_list)}] {label} {dist_km:.1f}km — déjà présent, ignoré")
            skip_count += 1
            continue

        if dry_run:
            print(f"  [{i+1:3d}/{len(trips_list)}] {label} {dist_km:.1f}km {dur_min:.0f}min — serait téléchargé")
            continue

        print(f"  [{i+1:3d}/{len(trips_list)}] {label} {dist_km:.1f}km {dur_min:.0f}min — téléchargement...", end=" ", flush=True)

        try:
            details = api_get(f"/history/trips/details?trip_key={trip_key}", cookie)
            trip    = convert_to_ble_format(details, summary)

            # Nom de fichier : date locale + 8 premiers chars du trip_key
            fname = TRIPS_DIR / f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}_{trip_key[:8]}.json"
            with open(fname, "w") as f:
                json.dump(trip, f, indent=2)

            n_legs = len(trip["legs"])
            print(f"OK ({n_legs} segments → {fname.name})")
            new_count += 1

            # Pause courtoise entre requêtes
            time.sleep(0.5)

        except Exception as e:
            print(f"ERREUR: {e}")
            error_count += 1

    print(f"\n── Résumé ─────────────────────────────")
    print(f"  Nouveaux téléchargés : {new_count}")
    print(f"  Déjà présents        : {skip_count}")
    print(f"  Erreurs              : {error_count}")
    print(f"  Total sur serveur    : {len(trips_list)}")
    return new_count

def main():
    parser = argparse.ArgumentParser(description="Récupère l'historique des trajets iweech depuis l'API")
    parser.add_argument("--cookie",      help="Valeur complète du header Cookie")
    parser.add_argument("--cookie-file", help="Fichier contenant le cookie (une ligne)")
    parser.add_argument("--limit",       type=int, help="Limiter au N premiers trajets")
    parser.add_argument("--dry-run",     action="store_true", help="Lister sans télécharger")
    args = parser.parse_args()

    if args.cookie_file:
        cookie = Path(args.cookie_file).read_text().strip()
    elif args.cookie:
        cookie = args.cookie
    else:
        print("Usage: python3 iweech_fetch_history.py --cookie \"cf_clearance=...; kc-access=...\"")
        print("  ou : python3 iweech_fetch_history.py --cookie-file cookie.txt")
        sys.exit(1)

    # Test rapide
    print("[*] Test de connexion...")
    try:
        data = api_get("/history/trips/synthesis", cookie)
        n = len(data.get("trips", []))
        print(f"[+] Connexion OK — {n} trajets disponibles sur le serveur")
    except Exception as e:
        print(f"[-] Connexion échouée : {e}")
        print("    → Vérifiez que le cookie est complet et frais (Firefox ouvert sur api.iweech.com)")
        sys.exit(1)

    new = fetch_all(cookie, limit=args.limit, dry_run=args.dry_run)

    if new > 0 and not args.dry_run:
        print(f"\n[*] Régénération de la carte avec {new} nouveau(x) trajet(s)...")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from iweech_sync import load_all_trips, generate_map
            trips = load_all_trips()
            map_path = TRIPS_DIR / "carte.html"
            generate_map(trips, map_path)
            print(f"[+] Carte mise à jour : file://{map_path}")
        except ImportError:
            print("[!] iweech_sync.py non trouvé — lancez manuellement :")
            print("    python3 iweech_sync.py --map-only")

if __name__ == "__main__":
    main()
