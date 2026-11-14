"""Relevés publiés dans des salons accessibles au compte bot."""
import os
import re
import requests

from instore import magasin_local
from veille import selectionne, simple


def releves(message, config, preferences):
    for embed in message.get("embeds", []):
        champs = {simple(f["name"]): f["value"] for f in embed.get("fields", [])}
        titre = embed.get("title", "")
        if not selectionne(preferences, titre):
            continue
        nom = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", champs.get("magasin", "")).strip()
        cp = champs.get("code postal", "").strip()
        if not nom or not magasin_local(config["zone"], nom, cp):
            continue
        magasin = simple(nom + " " + champs.get("site", ""))
        if not any(simple(enseigne) in magasin for enseigne in config["magasins"]):
            continue
        quantites = re.fullmatch(r"\s*(\d{1,8})\s*(?:→|->|➜)\s*(\d{1,8})\s*", champs.get("quantite", ""))
        if quantites:
            avant, apres = map(int, quantites.groups())
            if avant != apres:
                yield titre, nom, cp, avant, apres, embed.get("url"), message.get("timestamp")


def lire_salons(config, etat, notifier, diagnostic=False):
    salons = os.getenv("DISCORD_INSTORE_CHANNEL_IDS", "")
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not salons.strip():
        return
    if not token:
        print("Lecture instore inactive : DISCORD_BOT_TOKEN manquant")
        return
    positions = etat.setdefault("salons_stock", {})
    for salon in dict.fromkeys(s.strip() for s in salons.split(",")):
        if not salon.isascii() or not salon.isdecimal():
            continue
        position = positions.get(salon)
        params = {"limit": 1} if position is None or diagnostic else {"after": position, "limit": 100}
        try:
            r = requests.get(f"https://discord.com/api/v10/channels/{salon}/messages",
                             headers={"Authorization": "Bot " + token}, params=params, timeout=12)
            if not r.ok:
                print(f"Salon instore {salon} : HTTP {r.status_code}")
                if r.status_code == 429:
                    break
                continue
            messages = sorted(r.json(), key=lambda m: int(m["id"]))
            if diagnostic:
                print(f"Salon {salon} : lecture autorisée")
                continue
            if position is None:
                positions[salon] = messages[-1]["id"] if messages else "0"
                continue
            for m in messages:
                if int(m["id"]) <= int(positions[salon]):
                    continue
                for observation in releves(m, config, etat.get("preferences", {})):
                    notifier(*observation)
                positions[salon] = m["id"]
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"Lecture instore {salon} interrompue : {type(exc).__name__}")
