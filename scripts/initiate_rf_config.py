#!/usr/bin/env python3
import os
import sys
import json

# ── Parse Command-Line Arguments ──────────────────────────────────────────────
# Expected order from PHP:
#   python3 initiate_rf_config.py <username> <password> <login_url> <clientToken> <clientType> <base_server>

if len(sys.argv) < 7:
    print("Usage: python3 initiate_rf_config.py <username> <password> <login_url> <clientToken> <clientType> <base_server>")
    sys.exit(1)

username    = sys.argv[1]
password    = sys.argv[2]
login_url   = sys.argv[3]
clientToken = sys.argv[4]
clientType  = sys.argv[5]
base_server = sys.argv[6]

# ── Config File Path ──────────────────────────────────────────────────────────
rf_config_file = "/root/Desktop/awesome-tradescopier/source_code/client_rf_trader/rf_config.json"

# ── Remove Existing Config File ───────────────────────────────────────────────
os.system("rm -rf " + rf_config_file)

# ── Create New Empty Config File ──────────────────────────────────────────────
os.system("touch " + rf_config_file)

# ── Build Config Dictionary ───────────────────────────────────────────────────
config = {
    "username":    username,
    "password":    password,
    "login_url":   login_url,
    "clientToken": clientToken,
    "clientType":  clientType,
    "base_server": base_server
}

# ── Write Config to File as JSON ──────────────────────────────────────────────
with open(rf_config_file, "w") as f:
    json.dump(config, f, indent=4)

print(f"RF Config successfully written to: {rf_config_file}")