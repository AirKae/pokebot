"""
PokeBot - surveille le stock de produits Pokémon et envoie des alertes Discord.

Deux types de surveillance :
  1. "produits" : une page produit précise -> alerte quand elle passe de
     "rupture" à "en stock".
  2. "recherches" : une page de recherche/catégorie d'un magasin -> alerte
     quand un NOUVEAU produit Pokémon apparaît (souvent avant l'ouverture
     de la vente : c'est comme ça qu'on repère les sorties en avance).

Utilisation :
  python monitor.py              -> une vérification (pour GitHub Actions)
  python monitor.py --loop 180   -> tourne en continu, toutes les 180 s
  python monitor.py --doctor     -> teste chaque lien et dit s'il est lisible
  python monitor.py --test-discord -> envoie un message de test sur Discord
"""

import argparse
import json
import os
import random
import re
import sys
import time
import threading
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from veille import correspond, appliquer_commandes, dans_zone, simple, etat_campagne, liens_campagnes

import requests
from salons import lire_salons
from instore import lire_flux, quantites_fiche, quantite
import yaml
from bs4 import BeautifulSoup

ICI = Path(__file__).parent
FICHIER_CONFIG = ICI / "config.yaml"
FICHIER_ETAT = ICI / "state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# Mots utilisés si la page ne donne pas l'info de façon structurée.
MOTS_RUPTURE = [
    "rupture de stock", "en rupture", "indisponible", "épuisé", "epuise",
    "plus disponible", "bientôt disponible", "bientot disponible",
    "m'alerter", "me prévenir", "prevenez-moi", "victime de son succès",
]
MOTS_EN_STOCK = [
    "ajouter au panier", "ajouter au caddie", "add to cart", "acheter",
    "en stock", "disponible en ligne", "livraison à domicile",
]
MOTS_BLOCAGE = ["captcha", "datadome", "access denied", "accès refusé",
                "are you a robot", "êtes-vous un robot", "cf-challenge", "_incapsula_resource", "challenge-platform"]

DISPO_EN_STOCK = ("instock", "preorder", "presale", "limitedavailability",
                  "onlineonly", "backorder")
DISPO_RUPTURE = ("outofstock", "soldout", "discontinued", "instoreonly")


# --------------------------------------------------------------------------
# Lecture des pages
# --------------------------------------------------------------------------

_verrou_reseau = threading.Lock()
_prochaine_requete = {}
_suspensions = {}


def attente_http(valeur):
    try:
        return max(1, int(valeur))
    except (TypeError, ValueError):
        try:
            return max(1, parsedate_to_datetime(valeur).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 300


def telecharger(url, timeout=20):
    """Renvoie (code_http, texte). code 0 = erreur réseau."""
    domaine = urlsplit(url).netloc
    with _verrou_reseau:
        if time.time() < _suspensions.get(domaine, 0):
            return 429, ""
        depart = max(time.monotonic(), _prochaine_requete.get(domaine, 0))
        _prochaine_requete[domaine] = depart + 1.5
    time.sleep(max(0, depart - time.monotonic()))
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code in (403, 429, 503):
            with _verrou_reseau:
                _suspensions[domaine] = time.time() + attente_http(r.headers.get("Retry-After"))
        return r.status_code, r.text
    except requests.RequestException as e:
        return 0, str(e)


def page_bloquee(code, html):
    if code in (401, 403, 429, 503):
        return True
    debut = html[:5000].lower()
    return any(m in debut for m in MOTS_BLOCAGE) and len(html) < 60000


def _parcourir_jsonld(objet):
    """Parcourt récursivement le JSON-LD et renvoie les blocs 'Product'."""
    if isinstance(objet, list):
        for x in objet:
            yield from _parcourir_jsonld(x)
    elif isinstance(objet, dict):
        types = objet.get("@type", "")
        types = types if isinstance(types, list) else [types]
        if "Product" in types or "ProductGroup" in types:
            yield objet
        for v in objet.values():
            if isinstance(v, (dict, list)):
                yield from _parcourir_jsonld(v)


def _normaliser_dispo(valeur):
    v = str(valeur).lower().replace("https://schema.org/", "").replace(
        "http://schema.org/", "").replace(" ", "")
    if any(v.startswith(x) for x in DISPO_EN_STOCK):
        return True
    if any(v.startswith(x) for x in DISPO_RUPTURE):
        return False
    return None


def analyser_produit(html, mots_rupture=None, mots_stock=None):
    """
    Analyse une page produit.
    Renvoie un dict : {en_stock: True/False/None, titre, prix, methode}
    """
    soup = BeautifulSoup(html, "html.parser")
    titre, prix = None, None

    # 1) Données structurées JSON-LD (présentes sur la plupart des e-shops)
    for bloc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(bloc.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for produit in _parcourir_jsonld(data):
            titre = titre or produit.get("name")
            offres = produit.get("offers") or []
            if isinstance(offres, dict):
                offres = offres.get("offers", [offres]) if offres.get(
                    "@type") == "AggregateOffer" and offres.get("offers") else [offres]
            for o in offres:
                if not isinstance(o, dict):
                    continue
                prix = prix or o.get("price") or o.get("lowPrice")
                dispo = _normaliser_dispo(o.get("availability", ""))
                if dispo is not None:
                    return {"en_stock": dispo, "titre": titre, "prix": prix,
                            "methode": "json-ld"}

    # 2) Balises meta (ex. <meta property="product:availability" content="in stock">)
    for m in soup.find_all("meta"):
        prop = (m.get("property") or m.get("itemprop") or m.get("name") or "").lower()
        if "availability" in prop:
            dispo = _normaliser_dispo(m.get("content") or m.get("href") or "")
            if dispo is not None:
                return {"en_stock": dispo, "titre": titre or _titre_page(soup),
                        "prix": prix, "methode": "meta"}
    for lien in soup.find_all(attrs={"itemprop": "availability"}):
        dispo = _normaliser_dispo(lien.get("href") or lien.get("content") or "")
        if dispo is not None:
            return {"en_stock": dispo, "titre": titre or _titre_page(soup),
                    "prix": prix, "methode": "microdata"}

    # 3) Recherche de mots dans le texte visible
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    texte = soup.get_text(" ", strip=True).lower()
    rupture = any(m in texte for m in (mots_rupture or MOTS_RUPTURE))
    stock = any(m in texte for m in (mots_stock or MOTS_EN_STOCK))
    en_stock = False if rupture else (True if stock else None)
    return {"en_stock": en_stock, "titre": titre or _titre_page(soup),
            "prix": prix, "methode": "mots-cles"}


def _titre_page(soup):
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    return soup.title.get_text(strip=True) if soup.title else None


def verifier_shopify(url):
    """Les boutiques Shopify exposent /products/xxx.js avec le stock exact."""
    if "/products/" not in url:
        return None
    code, txt = telecharger(url.split("?")[0].rstrip("/") + ".js")
    if code != 200:
        return None
    try:
        d = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("available"), bool):
        return None
    prix = d.get("price")
    return {"en_stock": bool(d.get("available")), "titre": d.get("title"),
            "prix": prix / 100 if isinstance(prix, int) else prix,
            "methode": "shopify"}


def extraire_liens(html, url_base, motif):
    """Liens de la page de recherche dont l'URL correspond au motif (regex)."""
    soup = BeautifulSoup(html, "html.parser")
    regex = re.compile(motif, re.I)
    liens = {}
    for a in soup.find_all("a", href=True):
        complet = urljoin(url_base, a["href"]).split("#")[0]
        if urlsplit(complet).netloc != urlsplit(url_base).netloc:
            continue
        if urlsplit(complet).hostname == "www.amazon.fr":
            asin = re.search(r"/dp/([A-Z0-9]{10})(?:[/?]|$)", complet)
            if asin:
                complet = "https://www.amazon.fr/dp/" + asin[1]
        if regex.search(complet):
            texte = a.get_text(" ", strip=True) or a.get("title") or ""
            if complet not in liens or len(texte) > len(liens[complet]):
                liens[complet] = texte
    return liens


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def envoyer_discord(webhook, titre, url, magasin, prix=None, type_alerte="stock",
                    mention="", champs_plus=None, date=None):
    if not webhook:
        print(f"   (pas de webhook) ALERTE : {titre} -> {url}")
        return
    couleur = 0x2ECC71 if type_alerte == "stock" else 0xF1C40F
    entete = {"stock": "✅ EN STOCK", "nouveau": "🆕 NOUVEAU PRODUIT",
              "variation": "📍 MOUVEMENT DE STOCK", "local": "📍 DISPONIBLE EN MAGASIN", "invitation": "✉️ INVITATION DISPONIBLE",
              "formulaire": "📝 INSCRIPTION OUVERTE", "annonce": "📅 ANNONCE JCC" }[type_alerte]
    champs = [{"name": "Magasin", "value": magasin, "inline": True}]
    champs.extend(champs_plus or [])
    if prix:
        champs.append({"name": "Prix", "value": f"{prix} €", "inline": True})
    message = {
        "content": mention or None,
        "embeds": [{
            "title": f"{entete} · {titre or 'Produit'}"[:250],
            "url": url,
            "color": couleur,
            "fields": champs,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    if not url:
        message["embeds"][0].pop("url")
    if date:
        message["embeds"][0]["timestamp"] = date
    if type_alerte == "annonce":
        message["embeds"][0]["description"] = "Veux-tu suivre cette sortie en magasin ? Réponds à ce message avec !suivre ou !ignorer. Tu peux aussi écrire !suivre suivi de mots distinctifs."
    try:
        r = requests.post(webhook, json=message, timeout=15)
        if r.status_code == 429:  # Discord demande d'attendre
            time.sleep(float(r.json().get("retry_after", 2)))
            r = requests.post(webhook, json=message, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError("Échec de livraison Discord") from None


# --------------------------------------------------------------------------
# Boucle principale
# --------------------------------------------------------------------------

def charger_config():
    with open(FICHIER_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def charger_etat():
    if FICHIER_ETAT.exists():
        return json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    return {"produits": {}, "vus": {}, "suivis": {}}


def sauver_etat(etat):
    temp = FICHIER_ETAT.with_suffix(".tmp")
    temp.write_text(json.dumps(etat, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    temp.replace(FICHIER_ETAT)


def statut_produit(url, magasin_conf):
    res = verifier_shopify(url)
    if res:
        return 200, res
    code, html = telecharger(url)
    if code == 0 or page_bloquee(code, html):
        return code, None
    if code != 200:
        return code, None
    if magasin_conf.get("campagnes", {}).get("mode") == "invitation" and etat_campagne(html, "invitation") == "ouverte":
        return code, {"en_stock": None, "titre": _titre_page(BeautifulSoup(html, "html.parser")), "prix": None, "methode": "invitation"}
    resultat = analyser_produit(html, magasin_conf.get("mots_rupture"), magasin_conf.get("mots_stock"))
    resultat["html"] = html
    return code, resultat


def _alerte_nouveaux(ctx, cle, trouves):
    """trouves = {url: titre}. Alerte les nouveaux (sauf au tout 1er passage)."""
    etat, nom = ctx["etat"], ctx["nom"]
    connus = set(etat.setdefault("vus", {}).get(cle, []))
    preferences = etat.get("preferences", {})
    suivis_prioritaires = etat.setdefault("prioritaires", {}).setdefault(nom, [])
    for u, titre in trouves.items():
        choix = [v for k, v in preferences.items() if correspond(k, str(titre) + " " + u)]
        if choix and all(choix) and u not in suivis_prioritaires:
            suivis_prioritaires.append(u)
        elif not all(choix) and u in suivis_prioritaires:
            suivis_prioritaires.remove(u)
    nouveaux = [u for u in trouves if u not in connus]
    if cle in etat["vus"] and not ctx["doctor"]:
        for u in nouveaux:
            if any(not v and correspond(k, str(trouves[u]) + " " + u) for k, v in preferences.items()):
                continue
            envoyer_discord(ctx["webhook"], trouves[u], u, nom, None,
                            "nouveau", ctx["mention"])
    if not ctx["doctor"]:
        etat["vus"][cle] = sorted(connus | set(trouves))
        # suivi automatique du stock des produits découverts
        if ctx["mag"].get("suivi_auto", True):
            suivis = etat.setdefault("suivis", {}).setdefault(nom, [])
            for u in nouveaux:
                if u not in suivis:
                    suivis.append(u)
            del suivis[:-int(ctx["mag"].get("max_suivi", 60))]


def _alerte_stock(ctx, url, en_stock, titre, prix):
    if ctx["doctor"] or en_stock is None:
        return
    if any(not v and correspond(k, str(titre) + " " + url) for k, v in ctx["etat"].get("preferences", {}).items()):
        return
    produits = ctx["etat"].setdefault("produits", {})
    avant = produits.get(url, {}).get("en_stock")
    # alerte uniquement au passage rupture -> en stock
    if en_stock is True and avant is False:
        envoyer_discord(ctx["webhook"], titre, url, ctx["nom"], prix,
                        "stock", ctx["mention"])
    produits[url] = {"en_stock": en_stock, "vu": datetime.now().isoformat()}


def _pause(mag):
    time.sleep(random.uniform(*mag.get("pause", [1.5, 3.5])))


# ---------- sources de surveillance ----------

def source_produits(ctx):
    mag, nom, etat = ctx["mag"], ctx["nom"], ctx["etat"]
    urls = list(dict.fromkeys((mag.get("produits") or []) +
                              etat.get("suivis", {}).get(nom, [])))
    curseurs = etat.setdefault("curseurs", {})
    if urls:
        debut = curseurs.get(nom, 0) % len(urls)
        urls = (urls[debut:] + urls[:debut])[:max(1, mag.get("fiches_par_passage", 12))]
    lot_standard = set(urls)
    prioritaires = etat.get("prioritaires", {}).get(nom, [])
    if prioritaires:
        index = curseurs.get("priorite:" + nom, 0) % len(prioritaires)
        lot = (prioritaires[index:] + prioritaires[:index])[:4]
        if not ctx["doctor"]:
            curseurs["priorite:" + nom] = index + len(lot)
        urls = list(dict.fromkeys(urls + lot))
    for url in urls:
        if not ctx["doctor"] and url in lot_standard:
            curseurs[nom] = curseurs.get(nom, 0) + 1
        code, res = statut_produit(url, mag)
        if res is None:
            print(f"  ⛔ bloqué/erreur (HTTP {code}) : {url}")
            if code in (403, 429):  # le site nous freine : on arrête ce tour
                print("     -> pause pour ce magasin jusqu'au prochain tour")
                return
            continue
        preferences = etat.get("preferences", {})
        if preferences and res.get("html") and ctx.get("zone"):
            mesures = {}
            for ref, titre, magasin, cp, n in quantites_fiche(res["html"], None, ctx["zone"], preferences):
                mesures.setdefault((ref, titre, magasin, cp), set()).add(n)
            for (ref, titre, magasin, cp), nombres in mesures.items():
                if len(nombres) != 1 or ctx["doctor"]:
                    continue
                n = next(iter(nombres))
                historique = etat.setdefault("quantites_selections", {})
                cle = json.dumps([nom, ref, magasin, cp], ensure_ascii=False)
                avant = historique.get(cle)
                if avant != n and (avant is not None or n > 0):
                    notification_quantite(ctx["webhook"], ctx["mention"], titre, magasin, cp,
                                          avant, n, url, None)
                historique[cle] = n
        if res["methode"] == "invitation":
            signal_campagne(ctx, url, "ouverte", res["titre"])
        icone = {True: "🟢", False: "🔴", None: "❓"}[res["en_stock"]]
        print(f"  {icone} [{res['methode']}] {res['titre']} : {url}")
        _alerte_stock(ctx, url, res["en_stock"], res["titre"], res["prix"])
        _pause(mag)


def source_recherches(ctx):
    mag = ctx["mag"]
    for rech in mag.get("recherches") or []:
        url, motif = rech["url"], rech.get("motif_lien", ".")
        code, html = telecharger(url)
        if code != 200 or page_bloquee(code, html):
            print(f"  ⛔ recherche bloquée/erreur (HTTP {code}) : {url}")
            continue
        liens = {u: t for u, t in extraire_liens(html, url, motif).items()
                 if ctx["filtre"].search(u) or ctx["filtre"].search(t)}
        print(f"  🔎 recherche : {len(liens)} produit(s) Pokémon")
        if ctx["doctor"]:
            for u, t in list(liens.items())[:5]:
                print(f"       - {t[:60]} -> {u}")
        _alerte_nouveaux(ctx, "recherche:" + url, liens)
        _pause(mag)


def lire_lot_sitemaps(ctx, racine):
    """Traite une file persistante, avec un budget partagé par enseigne."""
    files = ctx["etat"].setdefault("files_sitemaps", {})
    file = list(files.get(racine, [])) or [racine]
    visitees = set()
    while file and ctx["budget_sitemaps"] > 0:
        url = file.pop(0)
        if url in visitees:
            continue
        visitees.add(url)
        ctx["budget_sitemaps"] -= 1
        code, xml = telecharger(url, timeout=20)
        if code != 200 or page_bloquee(code, xml):
            print(f"  Sitemap illisible (HTTP {code}) : {url}")
            if code in (403, 429, 503):
                file.insert(0, url)
                break
            continue
        from xml.etree import ElementTree
        try:
            arbre = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            continue
        locs = [(x.text or "").strip() for x in arbre.findall("./*/{*}loc")]
        if arbre.tag.split("}")[-1] == "sitemapindex":
            deja = visitees | set(file)
            for lien in locs:
                if lien not in deja and lien.startswith(("http://", "https://")):
                    file.append(lien)
                    deja.add(lien)
        elif arbre.tag.split("}")[-1] == "urlset":
            yield url, locs
    if not ctx["doctor"]:
        files[racine] = file


def sitemaps_depuis_robots(base):
    code, txt = telecharger(base.rstrip("/") + "/robots.txt")
    if code != 200:
        return []
    return re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)


def source_sitemaps(ctx):
    mag = ctx["mag"]
    sources = list(mag.get("sitemaps") or [])
    if "auto" in sources:
        sources.remove("auto")
        trouves = sitemaps_depuis_robots(mag.get("site", ""))
        if not trouves:
            print("  ⛔ aucun sitemap trouvé dans robots.txt (site bloqué ou injoignable ?)")
        sources += trouves
    ctx["budget_sitemaps"] = max(1, int(mag.get("sitemaps_par_passage", 6)))
    if not sources:
        return
    positions = ctx["etat"].setdefault("positions_sitemaps", {})
    index = positions.get(ctx["nom"], 0) % len(sources)
    parcours = sources[index:] + sources[:index]
    # Privilégier les racines produits sans exclure un index générique.
    motif = mag.get("filtre_sitemap")
    if motif:
        favoris = [u for u in sources if re.search(motif, u, re.I)]
        if favoris:
            index = positions.get(ctx["nom"], 0) % len(favoris)
            parcours = favoris[index:] + favoris[:index]
    for sm in parcours:
        if ctx["budget_sitemaps"] <= 0:
            break
        for feuille, urls in lire_lot_sitemaps(ctx, sm):
            pokes = {u: u.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
                     for u in urls if ctx["filtre"].search(u)}
            print(f"  Sitemap : {len(pokes)} liens Pokémon dans {feuille}")
            _alerte_nouveaux(ctx, "sitemap:" + feuille, pokes)
        if not ctx["doctor"]:
            positions[ctx["nom"]] = positions.get(ctx["nom"], 0) + 1


def source_shopify(ctx):
    """Boutiques Shopify : tout le catalogue + stock en 1 à 3 requêtes."""
    base = ctx["mag"].get("shopify")
    if not base:
        return
    base = base.rstrip("/")
    trouves, stocks = {}, {}
    for page in range(1, 11):
        code, txt = telecharger(f"{base}/products.json?limit=250&page={page}")
        if code != 200:
            print(f"  ⛔ Shopify illisible (HTTP {code})")
            return
        produits = json.loads(txt).get("products", [])
        for p in produits:
            if not (ctx["filtre"].search(p.get("title", "")) or
                    ctx["filtre"].search(" ".join(p.get("tags", []))
                                         if isinstance(p.get("tags"), list)
                                         else str(p.get("tags", "")))):
                continue
            url = f"{base}/products/{p['handle']}"
            variantes = p.get("variants") or []
            trouves[url] = p.get("title")
            stocks[url] = (any(v.get("available") for v in variantes),
                           p.get("title"), variantes[0].get("price") if variantes else None)
        if len(produits) < 250:
            break
        _pause(ctx["mag"])
    nb = sum(1 for s in stocks.values() if s[0])
    print(f"  🛍️  Shopify : {len(trouves)} produits Pokémon, {nb} en stock")
    if ctx["doctor"]:
        for u, (dispo, t, prix) in list(stocks.items())[:5]:
            print(f"       {'🟢' if dispo else '🔴'} {t} ({prix} €)")
    ctx["mag"] = dict(ctx["mag"], suivi_auto=False)  # stock déjà connu ici
    _alerte_nouveaux(ctx, "shopify:" + base, trouves)
    for url, (dispo, t, prix) in stocks.items():
        _alerte_stock(ctx, url, dispo, t, prix)


def _chemin(obj, chemin):
    for morceau in [c for c in (chemin or "").split(".") if c]:
        if isinstance(obj, list):
            obj = obj[int(morceau)] if morceau.isdigit() else None
        elif isinstance(obj, dict):
            obj = obj.get(morceau)
        if obj is None:
            return None
    return obj


def source_api(ctx):
    """Endpoint JSON public utilisé par le site lui-même (repéré dans F12 > Réseau)."""
    for api in ctx["mag"].get("api") or []:
        code, txt = telecharger(api["url"])
        if code != 200:
            print(f"  ⛔ API bloquée/erreur (HTTP {code}) : {api['url']}")
            continue
        try:
            items = _chemin(json.loads(txt), api.get("liste", "")) or []
        except (json.JSONDecodeError, ValueError):
            print("  ⛔ réponse API pas en JSON")
            continue
        trouves = {}
        for it in items:
            titre = str(_chemin(it, api.get("champ_nom", "name")) or "")
            lien = _chemin(it, api.get("champ_url", "url"))
            if not lien or not ctx["filtre"].search(titre + " " + str(lien)):
                continue
            url = urljoin(api.get("base_url", api["url"]), str(lien))
            trouves[url] = titre
            dispo = _chemin(it, api.get("champ_dispo", "")) if api.get("champ_dispo") else None
            if dispo is not None:
                dispo = (str(dispo).lower() in [str(v).lower() for v in
                         api.get("valeurs_en_stock", [True, "true", "instock", "1"])])
                _alerte_stock(ctx, url, dispo, titre, _chemin(it, api.get("champ_prix", "")))
        print(f"  🔌 API : {len(trouves)} produits Pokémon")
        if ctx["doctor"]:
            for u, t in list(trouves.items())[:5]:
                print(f"       - {t[:60]} -> {u}")
        _alerte_nouveaux(ctx, "api:" + api["url"], trouves)
        _pause(ctx["mag"])


def signal_campagne(ctx, url, statut, titre):
    if not statut or ctx["doctor"]:
        return
    historique = ctx["etat"].setdefault("campagnes", {})
    if statut == "ouverte" and historique.get(url) != statut:
        envoyer_discord(ctx["webhook"], titre, url, ctx["nom"], None,
                        ctx["mag"]["campagnes"]["mode"], ctx["mention"])
    historique[url] = statut


def source_campagnes(ctx):
    conf = ctx["mag"].get("campagnes")
    if not conf:
        return
    urls = list(conf.get("pages", []))
    if conf.get("catalogue_amazon"):
        code, html = telecharger(conf["catalogue_amazon"])
        if code == 200 and not page_bloquee(code, html):
            fiches = {}
            for article in BeautifulSoup(html, "html.parser").select("article"):
                lien = article.select_one('a[href^="https://www.amazon.fr/dp/"]')
                if not lien:
                    continue
                asin = re.match(r"https://www\.amazon\.fr/dp/([A-Z0-9]{10})(?:[/?#]|$)", lien["href"])
                if asin:
                    section = article.find_parent("section")
                    serie = section.find(["h2", "h3"]) if section else None
                    titre = article.find(["h3", "h4"])
                    fiches["https://www.amazon.fr/dp/" + asin[1]] = " ".join(
                        x.get_text(" ", strip=True) for x in (serie, titre or article) if x)
            if fiches:
                _alerte_nouveaux(ctx, "catalogue:" + conf["catalogue_amazon"], fiches)
    if conf.get("index"):
        code, html = telecharger(conf["index"])
        if code == 200 and not page_bloquee(code, html):
            urls.extend(liens_campagnes(html, conf["index"], conf["motifs"]))
    for url in dict.fromkeys(urls):
        code, html = telecharger(url)
        if code != 200 or page_bloquee(code, html):
            print(f"  Campagne illisible (HTTP {code}) : {url}")
            continue
        statut = etat_campagne(html, conf["mode"])
        print(f"  Campagne {statut or 'non confirmée'} : {url}")
        signal_campagne(ctx, url, statut, _titre_page(BeautifulSoup(html, "html.parser")))
        _pause(ctx["mag"])


def source_locale(ctx):
    for source in ctx["local"]:
        if source["enseigne"] != ctx["nom"] or not dans_zone(ctx["zone"], source):
            continue
        code, html = telecharger(source["url"])
        if code != 200 or page_bloquee(code, html):
            continue
        for ligne in BeautifulSoup(html, "html.parser").select(source["ligne_css"]):
            nom = ligne.select_one(source["magasin_css"])
            if nom and simple(nom.get_text(" ", strip=True)) == simple(source["magasin"]) and source.get("quantite_css"):
                element = ligne.select_one(source["quantite_css"])
                nombre = quantite(element.get_text(" ", strip=True)) if element else None
                if nombre is not None and not ctx["doctor"]:
                    cle = "|".join([ctx["nom"], source["magasin"], source["url"]])
                    suivi = ctx["etat"].setdefault("quantites_locales", {})
                    if cle in suivi and suivi[cle] != nombre:
                        notification_quantite(ctx["webhook"], ctx["mention"], source["produit"],
                            source["magasin"], source.get("code_postal", ""), suivi[cle], nombre, source["url"], None)
                    suivi[cle] = nombre
                break
            stock = ligne.select_one(source.get("stock_css", "stock-inconnu"))
            if not nom or not stock or simple(nom.get_text(" ", strip=True)) != simple(source["magasin"]):
                continue
            statut = simple(stock.get_text(" ", strip=True))
            oui = statut in [simple(v) for v in source["disponible"]]
            if not oui and statut not in [simple(v) for v in source["indisponible"]]:
                continue
            cle = "|".join([ctx["nom"], source["magasin"], source["url"]])
            historique = ctx["etat"].setdefault("stocks_locaux", {})
            print(f"  {source['magasin']} : {statut}")
            if not ctx["doctor"]:
                if oui and historique.get(cle) is not True:
                    envoyer_discord(ctx["webhook"], source["produit"], source["url"],
                                    source["magasin"], None, "local", ctx["mention"])
                historique[cle] = oui
            break
        _pause(ctx["mag"])


def verifier_magasin(nom, mag, config, etat, doctor):
    ctx = {"zone": config.get("zone", {}), "local": config.get("stocks_locaux", []), "nom": nom, "mag": mag, "etat": etat, "doctor": doctor,
           "webhook": os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook"),
           "mention": config.get("mention", ""),
           "filtre": re.compile(config.get("filtre_nom", "pok[eé]mon"), re.I)}
    print(f"\n=== {nom} ===")
    for source in (source_campagnes, source_locale, source_shopify, source_api, source_sitemaps,
                   source_recherches, source_produits):
        try:
            source(dict(ctx))
        except Exception as e:  # un site cassé ne doit pas arrêter les autres
            print(f"  ⚠️  erreur {source.__name__} : {e}")


def veille_sorties(config, etat, doctor):
    webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook")
    token = os.environ.get("DISCORD_BOT_TOKEN")
    salon = os.environ.get("DISCORD_CHANNEL_ID")
    auteur = os.environ.get("DISCORD_OWNER_ID")
    if token and salon and auteur and not doctor:
        params = {"limit": 100}
        if etat.get("discord_apres"):
            params["after"] = etat["discord_apres"]
        r = requests.get(f"https://discord.com/api/v10/channels/{salon}/messages",
                         headers={"Authorization": f"Bot {token}"}, params=params, timeout=10)
        if r.ok:
            messages = r.json()
            appliquer_commandes(messages, auteur, etat.setdefault("preferences", {}))
            if messages:
                etat["discord_apres"] = max((m["id"] for m in messages), key=int)
        else:
            print(f"Commandes Discord illisibles : HTTP {r.status_code}")
    for source in config.get("calendriers", []):
        code, html = telecharger(source["url"])
        if code != 200 or page_bloquee(code, html):
            print(f"Calendrier illisible : {source['url']}")
            continue
        page = BeautifulSoup(html, "html.parser")
        trouves = {}
        for a in page.select(source["liens_css"]):
            lien = urljoin(source["url"], a["href"])
            titre = a.get_text(" ", strip=True)
            if titre and re.fullmatch(source["motif"], lien):
                trouves[lien] = titre
        if not trouves:
            print(f"Calendrier sans fiche identifiable : {source['url']}")
            continue
        if doctor:
            print(f"Calendrier : {len(trouves)} fiche(s)")
            continue
        vus = etat.setdefault("annonces", {}).setdefault(source["url"], [])
        for url, titre in trouves.items():
            if url not in vus:
                envoyer_discord(webhook, titre, url, "Calendrier Pokémon", None, "annonce",
                                config.get("mention", ""))
                vus.append(url)


def notification_quantite(webhook, mention, titre, magasin, cp, avant, apres, url, date):
    if not webhook:
        raise ValueError("Configurer DISCORD_WEBHOOK avant de suivre les quantités")
    statut = "Premier stock observé" if avant is None else "Rupture" if apres == 0 else "Réassort" if apres > avant else "Baisse"
    champs = [{"name": "Quantité", "value": str(apres) if avant is None else f"{avant} → {apres}", "inline": True},
              {"name": "Statut", "value": statut, "inline": True}]
    if cp:
        champs.append({"name": "Code postal", "value": str(cp), "inline": True})
    lien = url if isinstance(url, str) and url.startswith("https://") else None
    envoyer_discord(webhook, titre, lien, magasin, None, "variation", mention, champs, date)


def verifier_instore(config, etat, doctor=False):
    from functools import partial
    notifier = partial(notification_quantite, os.environ.get("DISCORD_WEBHOOK") or
                       config.get("discord_webhook"), config.get("mention", ""))
    lire_salons(config, etat, notifier, doctor)
    try:
        lire_flux(config, etat, notifier, doctor)
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        statut = getattr(getattr(exc, "response", None), "status_code", None)
        print(f"Lecture du flux de stock interrompue : {statut or type(exc).__name__}")
    for fiche in config.get("fiches_instore", []):
        code, html = telecharger(fiche["url"])
        if code != 200 or page_bloquee(code, html):
            print(f"Fiche instore inaccessible : {fiche['produit']} (HTTP {code})")
            continue
        mesures = {}
        for nom, cp, nombre in quantites_fiche(html, fiche["reference"], config["zone"]):
            mesures.setdefault((nom, cp), set()).add(nombre)
        if not mesures:
            print(f"{fiche['produit']} : aucune quantité locale publiée dans les données lues")
        for (nom, cp), valeurs in mesures.items():
            if len(valeurs) != 1:
                continue
            n = next(iter(valeurs))
            print(f"{nom} ({cp}) · {fiche['produit']} · {n}")
            if not doctor:
                cle = "|".join([fiche["url"], nom, cp])
                historique = etat.setdefault("quantites_fiches", {})
                if cle in historique and historique[cle] != n:
                    notifier(fiche["produit"], nom, cp, historique[cle], n, fiche["url"], None)
                historique[cle] = n


def une_passe(config, etat, doctor=False):
    from concurrent.futures import ThreadPoolExecutor
    _suspensions.update({h: t for h, t in etat.get("suspensions", {}).items() if t > time.time()})
    try:
        veille_sorties(config, etat, doctor)
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Veille sorties interrompue : {type(exc).__name__}")
    verifier_instore(config, etat, doctor)
    magasins = [(n, m) for n, m in (config.get("magasins") or {}).items()
                if m and m.get("actif") is not False]
    # Les magasins sont vérifiés en parallèle ; les pages d'un même
    # magasin restent espacées pour ne pas le surcharger.
    with ThreadPoolExecutor(max_workers=max(1, len(magasins))) as ex:
        list(ex.map(lambda nm: verifier_magasin(*nm, config, etat, doctor),
                    magasins))
    if not doctor:
        etat["suspensions"] = {h: t for h, t in _suspensions.items() if t > time.time()}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", type=int, help="intervalle en secondes")
    p.add_argument("--doctor", action="store_true")
    p.add_argument("--test-discord", action="store_true")
    a = p.parse_args()

    config = charger_config()
    if a.test_discord:
        webhook = os.environ.get("DISCORD_WEBHOOK") or config.get("discord_webhook")
        if not webhook:
            raise SystemExit("DISCORD_WEBHOOK manquant")
        envoyer_discord(webhook, "Test du bot",
                        "https://discord.com", "Test", None, "nouveau")
        print("Message de test envoyé.")
        return

    etat = charger_etat()
    while True:
        une_passe(config, etat, doctor=a.doctor)
        if not a.doctor:
            sauver_etat(etat)
        if not a.loop or a.doctor:
            break
        attente = a.loop + random.randint(0, 30)
        print(f"\nProchaine vérification dans {attente} s...")
        time.sleep(attente)


if __name__ == "__main__":
    sys.exit(main())
