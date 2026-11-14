"""Lecture des inventaires publiés par les enseignes."""
import os
import re
from urllib.parse import urlsplit

import requests

from veille import dans_zone, simple, selectionne


def quantite(valeur):
    texte = str(valeur).strip()
    return int(texte) if re.fullmatch(r"[0-9]{1,8}", texte) else None


def magasin_local(zone, magasin, cp, coordonnees=None):
    if cp in zone.get("codes_postaux", []):
        return True
    mots = " " + re.sub(r"[^a-z0-9]+", " ", simple(magasin)).strip() + " "
    for ville in zone["villes_incluses"]:
        nom = re.sub(r"[^a-z0-9]+", " ", simple(ville))
        if " " + nom + " " in mots:
            return True
    return coordonnees is not None and dans_zone(zone, {"coordonnees": coordonnees})


def lire_flux(config, etat, notifier, diagnostic=False):
    url = os.environ.get("INSTORE_FEED_URL")
    if not url:
        return
    if urlsplit(url).scheme != "https":
        raise ValueError("Le flux instore doit utiliser HTTPS")
    token = os.environ.get("INSTORE_FEED_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    lignes = r.json()
    if not isinstance(lignes, list):
        raise ValueError("Le flux doit contenir une liste de relevés")
    from datetime import datetime
    historiques = etat.setdefault("instore_flux", {})
    for ligne in lignes:
        if not isinstance(ligne, dict):
            continue
        requis = ["enseigne","magasin_id","reference","magasin","produit","date"]
        if not all(isinstance(ligne.get(k), str) and ligne[k].strip() for k in requis):
            continue
        if not selectionne(etat.get("preferences", {}), ligne["produit"] + " " + ligne["reference"]):
            continue
        cp = str(ligne.get("code_postal", ""))
        if not magasin_local(config["zone"], ligne["magasin"], cp, ligne.get("coordonnees")):
            continue
        try:
            date = datetime.fromisoformat(ligne["date"].replace("Z", "+00:00"))
        except ValueError:
            continue
        n = quantite(ligne.get("quantite"))
        if n is None or date.tzinfo is None:
            continue
        cle = "|".join(ligne[k] for k in requis[:3])
        ancien = historiques.get(cle)
        if diagnostic:
            print(f"{ligne['magasin']} · {ligne['produit']} · {n} · {date.isoformat()}")
            continue
        if ancien and ancien["temps"] >= date.timestamp():
            continue
        if (ancien and ancien["quantite"] != n) or (ancien is None and n > 0):
            notifier(ligne["produit"], ligne["magasin"], cp, ancien["quantite"] if ancien else None, n, ligne.get("url"), date.isoformat())
        historiques[cle] = {"quantite": n, "temps": date.timestamp()}


def quantites_fiche(html, reference, zone, preferences=None):
    """Offre du produit désigné, quantité explicite et magasin unique identifié."""
    import json
    from bs4 import BeautifulSoup

    def produits(noeud):
        if isinstance(noeud, list):
            for item in noeud:
                yield from produits(item)
        if isinstance(noeud, dict):
            types = noeud.get("@type", [])
            if "Product" in (types if isinstance(types, list) else [types]):
                yield noeud
            yield from produits(noeud.get("@graph", []))

    for script in BeautifulSoup(html, "html.parser").select('script[type="application/ld+json"]'):
        try:
            donnees = json.loads(script.get_text())
        except ValueError:
            continue
        for produit in produits(donnees):
            identifiant = str(produit.get("sku") or produit.get("gtin13") or "").strip()
            if preferences is not None:
                if not identifiant or not selectionne(preferences, str(produit.get("name", "")) + " " + identifiant):
                    continue
            elif identifiant != reference:
                continue
            offres = produit.get("offers", [])
            for offre in offres if isinstance(offres, list) else [offres]:
                if not isinstance(offre, dict):
                    continue
                magasin = offre.get("availableAtOrFrom")
                niveau = offre.get("inventoryLevel")
                if not isinstance(magasin, dict) or not isinstance(niveau, dict):
                    continue
                adresse = magasin.get("address")
                if not isinstance(adresse, dict):
                    continue
                nom = magasin.get("name")
                cp = str(adresse.get("postalCode", ""))
                ville = adresse.get("addressLocality", "")
                n = quantite(niveau.get("value"))
                if nom and n is not None and magasin_local(zone, f"{nom} {ville}", cp):
                    if preferences is None:
                        yield str(nom), cp, n
                    else:
                        yield identifiant, produit.get("name") or identifiant, str(nom), cp, n
