#!/usr/bin/env python3
"""
iweech_monitor.py — Monitoring périodique de la santé du vélo
=============================================================
À lancer une fois par jour (ou via cron), collecte les métriques
de santé et les stocke en JSON. Génère un tableau de bord HTML.

Usage:
    python3 iweech_monitor.py              # collecte + dashboard
    python3 iweech_monitor.py --dash-only  # régénère le dashboard sans BLE
    python3 iweech_monitor.py --once       # collecte sans dashboard

Cron (tous les jours à 8h) :
    0 8 * * * cd ~/iweech && python3 iweech_monitor.py >> logs/monitor.log 2>&1

Dépendances:
    pip3 install bleak pycryptodome --break-system-packages
"""

import asyncio
import os
import sys
import json
import time
import base64
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from bleak import BleakClient, BleakScanner

# ── Config ────────────────────────────────────────────────────────────────────

from config import BIKE_MAC, OWNER_KEY, SUB, BIKE_ID
DATA_DIR    = Path.home() / "iweech_trips"
METRICS_FILE = DATA_DIR / "health_metrics.json"
DASH_FILE    = DATA_DIR / "dashboard.html"
UTC_OFFSET   = 2  # Paris été (CEST)

CHAR_AUTH        = "10000000-0000-1000-8000-00805f9b34fb"
CHAR_USER_ID     = "10001000-0000-1000-8000-00805f9b34fb"
CHAR_WRITE_ST    = "10000001-0000-1000-8000-00805f9b34fb"

METRICS_CHARS = {
    "battery_pct":      ("10002003-0000-1000-8000-00805f9b34fb", "uint8"),
    "motor_error":      ("10080002-0000-1000-8000-00805f9b34fb", "uint32"),
    "bms_error":        ("10080003-0000-1000-8000-00805f9b34fb", "uint32"),
    "total_distance":   ("10022007-0000-1000-8000-00805f9b34fb", "uint32"),

    "co2_savings":      ("10022009-0000-1000-8000-00805f9b34fb", "uint32"),
    "cyclist_energy":   ("1002200a-0000-1000-8000-00805f9b34fb", "uint32"),
    "bike_serial":      ("10030000-0000-1000-8000-00805f9b34fb", "utf8"),
    "mcu_version":      ("10030001-0000-1000-8000-00805f9b34fb", "utf8"),
    "brain_version":    ("10030002-0000-1000-8000-00805f9b34fb", "utf8"),
    "gatt_version":     ("10030003-0000-1000-8000-00805f9b34fb", "utf8"),
    "wifi_status":      ("10070000-0000-1000-8000-00805f9b34fb", "utf8"),
    "lock_state":       ("10002000-0000-1000-8000-00805f9b34fb", "uint8"),
    "sleep_mode":       ("10012001-0000-1000-8000-00805f9b34fb", "uint8"),
    "ride_mode":        ("10012000-0000-1000-8000-00805f9b34fb", "uint8"),
    "lights":           ("10002001-0000-1000-8000-00805f9b34fb", "uint8"),
    "trips_available":  ("20000002-0000-1000-8000-00805f9b34fb", "uint32"),
}

# ── Auth ──────────────────────────────────────────────────────────────────────

def build_payload(sub, key):
    aes_key = os.urandom(16)
    ct = AES.new(key.encode("ascii"), AES.MODE_CBC, iv=aes_key).encrypt(
        pad(f"{int(time.time()*1000)}|{sub}".encode(), 16))
    return f"{sub}|{base64.b64encode(aes_key).decode()}|{base64.b64encode(ct).decode()}".encode()

async def authenticate(client):
    await client.write_gatt_char(CHAR_USER_ID, build_payload(SUB, OWNER_KEY), response=True)
    await asyncio.sleep(2)
    return bytes(await client.read_gatt_char(CHAR_AUTH))[0] == 1

# ── Lecture des métriques ─────────────────────────────────────────────────────

async def read_metrics():
    print(f"[*] Recherche du vélo...")
    dev = await BleakScanner.find_device_by_address(BIKE_MAC, timeout=15.0)
    if not dev:
        print("[-] Vélo non trouvé.")
        return None

    print(f"[+] Connecté à {dev.name}")
    metrics = {}

    async with BleakClient(BIKE_MAC, timeout=30.0) as c:
        if not await authenticate(c):
            print("[-] Auth échouée.")
            return None
        print("[+] Authentifié")

        for name, (uuid, fmt) in METRICS_CHARS.items():
            try:
                raw = bytes(await c.read_gatt_char(uuid))
                if fmt == "utf8":
                    metrics[name] = raw.decode("utf-8", errors="replace").strip('\x00')
                elif fmt == "uint8":
                    metrics[name] = raw[0] if raw else 0
                elif fmt == "uint32":
                    metrics[name] = int.from_bytes(raw, "little") if raw else 0
            except Exception as e:
                metrics[name] = None
                print(f"  [!] {name}: {e}")

    now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_OFFSET)
    metrics["timestamp"] = now.isoformat()
    metrics["timestamp_local"] = now.strftime("%d/%m/%Y %H:%M")

    # Calculs dérivés
    if metrics.get("total_distance"):
        metrics["total_km"] = round(metrics["total_distance"] / 1000, 1)
    if metrics.get("co2_savings"):
        metrics["co2_kg"] = round(metrics["co2_savings"] / 1000, 2)
    if metrics.get("cyclist_energy"):
        metrics["cyclist_kwh"] = round(metrics["cyclist_energy"] / 3_600_000, 2)

    # Calculer Wh/km depuis les trajets stockés
    try:
        from pathlib import Path as _Path
        import json as _json
        _trips_dir = _Path.home() / "iweech_trips"
        _total_wh = 0
        _total_km = 0
        for _f in _trips_dir.glob("*.json"):
            if _f.name in ("health_metrics.json",):
                continue
            try:
                with open(_f) as _fp:
                    _t = _json.load(_fp)
                _total_wh += _t.get("battery_energy", 0)
                _total_km += _t.get("distance", 0) / 1000
            except Exception:
                pass
        if _total_km > 0:
            metrics["wh_per_km"] = round(_total_wh / _total_km, 1)
            metrics["total_wh_consumed"] = round(_total_wh)
            metrics["estimated_cycles"] = round(_total_wh / 360)
    except Exception:
        pass

    return metrics

# ── Persistance ───────────────────────────────────────────────────────────────

def load_history():
    if not METRICS_FILE.exists():
        return []
    try:
        with open(METRICS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def save_metrics(metrics):
    history = load_history()
    history.append(metrics)
    with open(METRICS_FILE, "w") as f:
        json.dump(history, f, indent=2)
    print(f"[+] Métriques sauvegardées ({len(history)} entrées)")

# ── Dashboard HTML ────────────────────────────────────────────────────────────

LOCK_LABELS  = {0: "Inconnu", 1: "Verrouillé 🔒", 2: "Déverrouillé 🔓", 3: "Volé ⚠️"}
RIDE_LABELS  = {0: "—", 1: "iRide", 2: "Freeride", 3: "Fitness"}
LIGHT_LABELS = {0: "Off manuel", 1: "On manuel", 2: "Off auto", 3: "On auto"}
SLEEP_LABELS = {0: "Veille", 1: "Éveillé"}

def generate_dashboard(history):
    bike_id = BIKE_ID or "iweech"
    if not history:
        print("[!] Aucune donnée.")
        return

    latest = history[-1]

    # Séries temporelles pour les graphiques
    timestamps  = [h.get("timestamp_local", h.get("timestamp","")[:16]) for h in history]
    km_series    = [h.get("total_km") for h in history]
    co2_series   = [h.get("co2_kg") for h in history]
    wh_series    = [h.get("wh_per_km") for h in history]
    cycle_series = [h.get("estimated_cycles") for h in history]

    ts_json    = json.dumps(timestamps)
    km_json    = json.dumps(km_series)
    co2_json   = json.dumps(co2_series)
    wh_json    = json.dumps(wh_series)
    cycle_json = json.dumps(cycle_series)

    # Valeurs courantes
    pct     = latest.get("battery_pct", "—")
    km      = latest.get("total_km", "—")
    co2     = latest.get("co2_kg", "—")
    ckwh    = latest.get("cyclist_kwh", "—")
    wh_km   = latest.get("wh_per_km", "—")
    est_cyc = latest.get("estimated_cycles", "—")
    motor_e = latest.get("motor_error", 0)
    bms_e   = latest.get("bms_error", 0)
    lock    = LOCK_LABELS.get(latest.get("lock_state"), "—")
    ride    = RIDE_LABELS.get(latest.get("ride_mode"), "—")
    lights  = LIGHT_LABELS.get(latest.get("lights"), "—")
    sleep   = SLEEP_LABELS.get(latest.get("sleep_mode"), "—")
    wifi    = latest.get("wifi_status", "—")
    brain   = latest.get("brain_version", "—")
    mcu     = latest.get("mcu_version", "—")
    updated = latest.get("timestamp_local", "—")
    trips_avail = latest.get("trips_available", 0)

    motor_color = "#e74c3c" if motor_e else "#2ecc71"
    bms_color   = "#e74c3c" if bms_e   else "#2ecc71"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="3600">
<title>iweech {bike_id} — Santé du vélo</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f0efe9; color: #222; padding: 16px; }}
h1 {{ font-size: 16px; font-weight: 500; margin-bottom: 4px; }}
.subtitle {{ font-size: 12px; color: #888; margin-bottom: 20px; }}
.section-title {{ font-size: 11px; font-weight: 500; color: #888; text-transform: uppercase; letter-spacing: .06em; margin: 20px 0 10px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 8px; }}
.card {{ background: #fff; border-radius: 10px; padding: 12px 14px; border: 0.5px solid #e0e0d8; }}
.card-label {{ font-size: 11px; color: #888; margin-bottom: 4px; }}
.card-value {{ font-size: 22px; font-weight: 500; }}
.card-unit {{ font-size: 12px; color: #888; margin-left: 3px; }}
.card-sub {{ font-size: 11px; color: #aaa; margin-top: 2px; }}
.status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }}
.status-card {{ background: #fff; border-radius: 10px; padding: 10px 14px; border: 0.5px solid #e0e0d8; font-size: 12px; }}
.status-label {{ color: #888; font-size: 11px; margin-bottom: 3px; }}
.status-value {{ font-weight: 500; }}
.error-ok {{ color: #2ecc71; font-weight: 500; }}
.error-err {{ color: #e74c3c; font-weight: 500; }}
.chart-wrap {{ background: #fff; border-radius: 10px; padding: 16px; border: 0.5px solid #e0e0d8; margin-bottom: 10px; }}
.chart-title {{ font-size: 12px; color: #888; margin-bottom: 10px; }}
.alert {{ background: #fdf3f3; border: 1px solid #f5c6c6; border-radius: 8px; padding: 10px 14px; font-size: 13px; color: #c0392b; margin-bottom: 12px; }}
.badge {{ display: inline-block; font-size: 10px; padding: 2px 7px; border-radius: 10px; font-weight: 500; margin-left: 6px; vertical-align: middle; }}
.badge-ok {{ background: #eafaf1; color: #27ae60; }}
.badge-warn {{ background: #fef9e7; color: #d68910; }}
.badge-err {{ background: #fdf3f3; color: #c0392b; }}
</style>
</head>
<body>

<h1>🚲 iweech {bike_id} — Tableau de bord santé</h1>
<p class="subtitle">Dernière mise à jour : {updated} · {len(history)} mesure(s) enregistrée(s){' · <b style="color:#e74c3c">Trajet(s) en attente de sync !</b>' if trips_avail else ''}</p>

{'<div class="alert">⚠️ Erreur moteur détectée (code ' + str(motor_e) + ') — consultez un technicien.</div>' if motor_e else ''}
{'<div class="alert">⚠️ Erreur BMS batterie détectée (code ' + str(bms_e) + ') — consultez un technicien.</div>' if bms_e else ''}

<p class="section-title">État de la batterie</p>
<div class="grid">
  <div class="card">
    <div class="card-label">Charge actuelle</div>
    <div class="card-value">{pct}<span class="card-unit">%</span></div>
  </div>
  <div class="card">
    <div class="card-label">Consommation</div>
    <div class="card-value">{wh_km}<span class="card-unit">Wh/km</span></div>
    <div class="card-sub">moyenne sur tous les trajets</div>
  </div>
  <div class="card">
    <div class="card-label">Cycles estimés</div>
    <div class="card-value">{est_cyc}</div>
    <div class="card-sub">sur ~500 cycles de vie</div>
  </div>
  <div class="card">
    <div class="card-label">Erreur moteur</div>
    <div class="card-value {'error-ok' if not motor_e else 'error-err'}">{motor_e if motor_e else '✓ OK'}</div>
  </div>
  <div class="card">
    <div class="card-label">Erreur BMS</div>
    <div class="card-value {'error-ok' if not bms_e else 'error-err'}">{bms_e if bms_e else '✓ OK'}</div>
  </div>
</div>

<p class="section-title">Kilométrage & statistiques</p>
<div class="grid">
  <div class="card">
    <div class="card-label">Distance totale</div>
    <div class="card-value">{km}<span class="card-unit">km</span></div>
  </div>
  <div class="card">
    <div class="card-label">CO₂ économisé</div>
    <div class="card-value">{co2}<span class="card-unit">kg</span></div>
  </div>
  <div class="card">
    <div class="card-label">Énergie cycliste</div>
    <div class="card-value">{ckwh}<span class="card-unit">kWh</span></div>
    <div class="card-sub">vous avez pédalé !</div>
  </div>
</div>

<p class="section-title">État actuel</p>
<div class="status-grid">
  <div class="status-card"><div class="status-label">Verrou</div><div class="status-value">{lock}</div></div>
  <div class="status-card"><div class="status-label">Mode de conduite</div><div class="status-value">{ride}</div></div>
  <div class="status-card"><div class="status-label">Lumières</div><div class="status-value">{lights}</div></div>
  <div class="status-card"><div class="status-label">État</div><div class="status-value">{sleep}</div></div>
  <div class="status-card"><div class="status-label">WiFi</div><div class="status-value">{wifi}</div></div>
  <div class="status-card"><div class="status-label">Firmware brain</div><div class="status-value">{brain}</div></div>
  <div class="status-card"><div class="status-label">Firmware MCU</div><div class="status-value">{mcu}</div></div>
  <div class="status-card"><div class="status-label">Trajets en attente</div><div class="status-value">{trips_avail} {'<span class="badge badge-warn">à synchroniser</span>' if trips_avail else '<span class="badge badge-ok">OK</span>'}</div></div>
</div>

{'<p class="section-title">Évolution dans le temps</p>' if len(history) > 1 else ''}

{'''<div class="chart-wrap">
  <div class="chart-title">État de santé batterie (SOH %)</div>
  <div style="position:relative;height:180px"><canvas id="sohChart" role="img" aria-label="Évolution SOH batterie">Évolution du SOH batterie dans le temps.</canvas></div>
</div>
<div class="chart-wrap">
  <div class="chart-title">Kilométrage total</div>
  <div style="position:relative;height:180px"><canvas id="kmChart" role="img" aria-label="Évolution kilométrage total">Évolution du kilométrage total dans le temps.</canvas></div>
</div>
<div class="chart-wrap">
  <div class="chart-title">Cycles de charge</div>
  <div style="position:relative;height:160px"><canvas id="chgChart" role="img" aria-label="Évolution cycles de charge">Évolution des cycles de charge dans le temps.</canvas></div>
</div>''' if len(history) > 1 else '<p style="font-size:12px;color:#aaa;margin-top:8px;">Les graphiques d\'évolution apparaîtront après la 2e mesure.</p>'}

<script>
const labels = {ts_json};
const kmData    = {km_json};
const chgData   = {cycle_json};
const co2Data   = {co2_json};
const whData    = {wh_json};

const chartOpts = (label, color) => ({{
  responsive: true, maintainAspectRatio: false,
  plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => ctx.parsed.y + ' ' + label }} }} }},
  scales: {{
    x: {{ ticks: {{ font: {{ size: 10 }}, maxRotation: 45 }} }},
    y: {{ ticks: {{ font: {{ size: 10 }} }}, beginAtZero: false }}
  }}
}});

if (document.getElementById('sohChart')) {{
  new Chart(document.getElementById('whChart'), {{
    type: 'line',
    data: {{ labels, datasets: [{{ data: whData, borderColor: '#e67e22', backgroundColor: 'rgba(230,126,34,.08)', tension: .3, pointRadius: 3, fill: true }}] }},
    options: {{ ...chartOpts('Wh/km') }}
  }});

  new Chart(document.getElementById('kmChart'), {{
    type: 'line',
    data: {{ labels, datasets: [{{ data: kmData, borderColor: '#3498db', backgroundColor: 'rgba(52,152,219,.08)', tension: .3, pointRadius: 3, fill: true }}] }},
    options: chartOpts('km')
  }});
  new Chart(document.getElementById('chgChart'), {{
    type: 'bar',
    data: {{ labels, datasets: [{{ data: chgData, backgroundColor: 'rgba(243,156,18,.7)', borderColor: '#f39c12', borderWidth: 1 }}] }},
    options: chartOpts('cycles')
  }});
}}
</script>
</body>
</html>"""

    with open(DASH_FILE, "w") as f:
        f.write(html)
    print(f"[+] Dashboard généré : {DASH_FILE}")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="iweech health monitor")
    parser.add_argument("--dash-only", action="store_true", help="Régénérer le dashboard sans BLE")
    parser.add_argument("--once",      action="store_true", help="Collecter sans générer le dashboard")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    if not args.dash_only:
        metrics = await read_metrics()
        if metrics:
            save_metrics(metrics)
            print(f"\n── Snapshot ──────────────────")
            print(f"  Charge         : {metrics.get('battery_pct')} %")
            print(f"  Kilométrage    : {metrics.get('total_km')} km")

            print(f"  CO₂ économisé  : {metrics.get('co2_kg')} kg")
            print(f"  Erreur moteur  : {metrics.get('motor_error') or 'OK'}")
            print(f"  Erreur BMS     : {metrics.get('bms_error') or 'OK'}")
            print(f"  Trajets en att.: {metrics.get('trips_available')}")
        else:
            print("[-] Collecte échouée, dashboard non mis à jour.")
            return

    if not args.once:
        history = load_history()
        generate_dashboard(history)
        print(f"\nOuvrez : file://{DASH_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
