"""Sources publiques de campagnes et de disponibilité locale."""
import math
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def simple(valeur):
    texte = unicodedata.normalize("NFKD", str(valeur).casefold())
    return " ".join("".join(c for c in texte if not unicodedata.combining(c)).split())


def dans_zone(zone, magasin):
    if simple(magasin.get("ville", "")) in {simple(v) for v in zone["villes_incluses"]}:
        return True
    if "coordonnees" not in magasin:
        return False
    lat, lon = map(math.radians, zone["centre"])
    autre_lat, autre_lon = map(math.radians, magasin["coordonnees"])
    a = math.sin((autre_lat-lat)/2)**2 + math.cos(lat)*math.cos(autre_lat)*math.sin((autre_lon-lon)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a))) <= zone["rayon_km"]


def etat_campagne(html, mode):
    page = BeautifulSoup(html, "html.parser")
    for bloc in page(["script", "style", "footer", "nav"]):
        bloc.decompose()
    texte = simple(page.get_text(" ", strip=True))
    if re.search(r"inscriptions? (?:sont (?:desormais )?)?(?:closes?|terminees?)|operation (?:est )?terminee", texte):
        return "fermee"
    for bouton in page.select('a[href],button,input, [role="button"]'):
        if bouton.has_attr("disabled") or bouton.get("aria-disabled") == "true":
            continue
        label = simple(" ".join([bouton.get_text(" ", strip=True), bouton.get("value", ""), bouton.get("aria-label", "")]))
        if mode == "invitation" and "demander une invitation" in label:
            return "ouverte"
    if mode == "formulaire" and ("inscrivez-vous" in texte or "inscription" in texte) and page.select_one(
            'input[type=email],input[placeholder*="@"],form input:not([type=hidden]):not([type=search])'):
        return "ouverte"
    return None


def liens_campagnes(html, base, motifs):
    for lien in BeautifulSoup(html, "html.parser").select("a[href]"):
        url = urljoin(base, lien["href"]).split("#")[0]
        if urlsplit(url).scheme == "https" and any(re.fullmatch(m, url, re.I) for m in motifs):
            yield url


def correspond(preference, titre):
    mots = set(re.findall(r"[a-z0-9]+", simple(titre)))
    requis = set(re.findall(r"[a-z0-9]+", simple(preference)))
    return bool(requis) and requis <= mots


def selectionne(preferences, titre):
    choix = [v for terme, v in preferences.items() if correspond(terme, titre)]
    return bool(choix) and all(choix)


def appliquer_commandes(messages, auteur, preferences):
    for message in sorted(messages, key=lambda m: int(m["id"])):
        if message.get("author", {}).get("id") != auteur:
            continue
        commande, _, terme = message.get("content", "").strip().partition(" ")
        if not terme:
            reponse = message.get("referenced_message") or {}
            for embed in reponse.get("embeds", []):
                titre = embed.get("title", "")
                if titre.startswith("📅 ANNONCE JCC · "):
                    terme = titre.split(" · ", 1)[1]
                    break
        terme = simple(terme)[:250]
        if terme and commande.lower() in ("!suivre", "!ignorer"):
            preferences[terme] = commande.lower() == "!suivre"
