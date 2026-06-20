#!/usr/bin/env python3
"""
iweech_lock.py — Verrouillage / déverrouillage du vélo iweech via BLE
======================================================================
Usage:
    python3 iweech_lock.py lock      # verrouiller
    python3 iweech_lock.py unlock    # déverrouiller
    python3 iweech_lock.py status    # état actuel

Dépendances:
    pip3 install bleak pycryptodome --break-system-packages
"""

import asyncio, os, sys, time, base64
from bleak import BleakClient, BleakScanner
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from config import BIKE_MAC, OWNER_KEY, SUB, BIKE_ID

CHAR_AUTH     = "10000000-0000-1000-8000-00805f9b34fb"  # READ — 1=auth ok
CHAR_USER_ID  = "10001000-0000-1000-8000-00805f9b34fb"  # WRITE — payload auth
CHAR_LOCK     = "10002000-0000-1000-8000-00805f9b34fb"  # READ/NOTIFY — état verrou
CHAR_SET_LOCK = "10002100-0000-1000-8000-00805f9b34fb"  # WRITE — commande verrou
CHAR_WRITE_ST = "10000001-0000-1000-8000-00805f9b34fb"  # READ — résultat écriture

LOCK_STATES = {0: "Inconnu", 1: "🔒 Verrouillé", 2: "🔓 Déverrouillé", 3: "⚠️  Volé"}
LOCK_CMD    = {"lock": 1, "unlock": 2}

def build_payload(sub, key):
    aes_key = os.urandom(16)
    ct = AES.new(key.encode("ascii"), AES.MODE_CBC, iv=aes_key).encrypt(
        pad(f"{int(time.time()*1000)}|{sub}".encode(), 16))
    return f"{sub}|{base64.b64encode(aes_key).decode()}|{base64.b64encode(ct).decode()}".encode()

async def main(action):
    print(f"[*] Recherche du vélo...")
    dev = await BleakScanner.find_device_by_address(BIKE_MAC, timeout=10.0)
    if not dev:
        print("[-] Vélo non trouvé. Allumez-le et réessayez.")
        sys.exit(1)
    print(f"[+] Vélo trouvé : {dev.name}")

    async with BleakClient(BIKE_MAC, timeout=20.0) as c:
        # Auth
        await c.write_gatt_char(CHAR_USER_ID, build_payload(SUB, OWNER_KEY), response=True)
        await asyncio.sleep(2)
        if bytes(await c.read_gatt_char(CHAR_AUTH))[0] != 1:
            print("[-] Authentification échouée.")
            sys.exit(1)
        print("[+] Authentifié")

        # Lire l'état actuel
        raw = bytes(await c.read_gatt_char(CHAR_LOCK))
        state = raw[0] if raw else 0
        print(f"\n    État actuel : {LOCK_STATES.get(state, f'Inconnu ({state})')}")

        if action == "status":
            return

        # Vérifier qu'on ne fait pas une action inutile
        if action == "lock" and state == 1:
            print("    Le vélo est déjà verrouillé.")
            return
        if action == "unlock" and state == 2:
            print("    Le vélo est déjà déverrouillé.")
            return

        # Envoyer la commande
        cmd = LOCK_CMD[action]
        label = "Verrouillage" if action == "lock" else "Déverrouillage"
        print(f"\n[*] {label} en cours...")

        lock_changed = asyncio.Event()
        new_state = [state]

        def on_lock_notify(sender, data):
            s = bytes(data)[0] if data else 0
            new_state[0] = s
            print(f"    Notification verrou : {LOCK_STATES.get(s, s)}")
            lock_changed.set()

        await c.start_notify(CHAR_LOCK, on_lock_notify)
        await c.write_gatt_char(CHAR_SET_LOCK, bytes([cmd]), response=True)

        # Lire le WriteStatus
        await asyncio.sleep(0.5)
        ws = bytes(await c.read_gatt_char(CHAR_WRITE_ST)).decode("utf-8", errors="replace").strip('\x00')
        print(f"    WriteStatus : {ws}")

        # Attendre la notification de changement d'état (max 5s)
        try:
            await asyncio.wait_for(lock_changed.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        # Lire l'état final
        await asyncio.sleep(0.5)
        raw2 = bytes(await c.read_gatt_char(CHAR_LOCK))
        final = raw2[0] if raw2 else 0

        if action == "lock" and final == 1:
            print(f"\n✅ Vélo verrouillé avec succès.")
        elif action == "unlock" and final == 2:
            print(f"\n✅ Vélo déverrouillé avec succès.")
        else:
            print(f"\n⚠️  État final : {LOCK_STATES.get(final, final)}")
            if ws and ":0:" not in ws:
                print(f"    La commande a peut-être échoué (WriteStatus={ws})")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("lock", "unlock", "status"):
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
