#!/usr/bin/env python3
"""
config.py — Chargement de la configuration iweech
Lit config.json dans le répertoire du script ou le répertoire courant.
"""

import json
from pathlib import Path

def load_config():
    """Charge config.json depuis le répertoire du script ou le répertoire courant."""
    candidates = [
        Path(__file__).parent / "config.json",
        Path.cwd() / "config.json",
    ]
    for path in candidates:
        if path.exists():
            with open(path) as f:
                cfg = json.load(f)
            required = ["bike_mac", "owner_key", "sub"]
            missing = [k for k in required if k not in cfg]
            if missing:
                raise ValueError(f"config.json manque les clés : {missing}")
            return cfg

    raise FileNotFoundError(
        "config.json introuvable.\n"
        "Créez-le avec :\n"
        '''{
  "bike_mac":  "B8:27:EB:XX:XX:XX",
  "bike_id":   "IW-XXXX",
  "owner_key": "your16charhexkey",
  "sub":       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}'''
    )

# Valeurs chargées au module level pour import direct
try:
    _cfg = load_config()
    BIKE_MAC  = _cfg["bike_mac"]
    BIKE_ID   = _cfg.get("bike_id", "IW-0000")
    OWNER_KEY = _cfg["owner_key"]
    SUB       = _cfg["sub"]
except FileNotFoundError:
    # Permettre l'import même sans config (les scripts afficheront une erreur utile au runtime)
    BIKE_MAC = BIKE_ID = OWNER_KEY = SUB = None
