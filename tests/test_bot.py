import json, sys, threading, http.server, os, shutil, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import monitor as m

# --- analyse de pages ---
jsonld = lambda a: f'<script type="application/ld+json">{json.dumps({"@context":"https://schema.org","@type":"Product","name":"ETB Pokémon EV10","offers":{"@type":"Offer","price":"54.99","availability":a}})}</script>'
r = m.analyser_produit(jsonld("https://schema.org/InStock")); assert r["en_stock"] is True and r["prix"]=="54.99" and r["methode"]=="json-ld", r
assert m.analyser_produit(jsonld("http://schema.org/OutOfStock"))["en_stock"] is False
assert m.analyser_produit(jsonld("PreOrder"))["en_stock"] is True
graph = '<script type="application/ld+json">{"@graph":[{"@type":"WebPage"},{"@type":["Product"],"name":"X","offers":[{"availability":"SoldOut"}]}]}</script>'
assert m.analyser_produit(graph)["en_stock"] is False
assert m.analyser_produit('<meta property="product:availability" content="in stock"><h1>Display</h1>')["en_stock"] is True
assert m.analyser_produit('<h1>Display</h1><button>Ajouter au panier</button>')["en_stock"] is True
assert m.analyser_produit('<h1>Display</h1><p>Produit en rupture de stock</p><button>Ajouter au panier</button>')["en_stock"] is False
assert m.analyser_produit('<h1>Display</h1>')["en_stock"] is None
assert m.page_bloquee(403, "") and m.page_bloquee(200, "<html>Please solve captcha datadome</html>")
liens = m.extraire_liens('<a href="/p/etb-pokemon-123">ETB Pokémon</a><a href="/compte">x</a>', "https://shop.fr/recherche", r"/p/")
assert liens == {"https://shop.fr/p/etb-pokemon-123": "ETB Pokémon"}
print("analyse OK")

# --- scénario complet avec un faux magasin + faux Discord ---
pages = {"/p/etb": jsonld("OutOfStock"), "/recherche": '<a href="/p/etb">ETB Pokémon</a>'}
recus = []
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(s):
        b = pages.get(s.path, "").encode(); s.send_response(200 if s.path in pages else 404); s.end_headers(); s.wfile.write(b)
    def do_POST(s):
        recus.append(json.loads(s.rfile.read(int(s.headers["Content-Length"])))); s.send_response(204); s.end_headers()
    def log_message(s,*a): pass
srv = http.server.HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
m.time.sleep = lambda *_: None
cfg = {"discord_webhook": base+"/hook", "magasins": {"Test": {"produits":[base+"/p/etb"], "recherches":[{"url":base+"/recherche","motif_lien":"/p/"}]}}}
etat = {"produits":{}, "vus":{}, "suivis":{}}
m.une_passe(cfg, etat); assert recus == [], recus                 # rupture + 1er passage recherche : pas d'alerte
pages["/p/etb"] = jsonld("InStock"); pages["/recherche"] += '<a href="/p/display">Display Pokémon JP</a><a href="/p/lego">Lego</a>'
m.une_passe(cfg, etat)
titres = sorted(e["embeds"][0]["title"] for e in recus); print(titres)
assert len(recus) == 2 and any("EN STOCK" in t for t in titres) and any("NOUVEAU" in t and "Display" in t for t in titres)
m.une_passe(cfg, etat); assert len(recus) == 2                     # pas de doublon
print("scénario OK")

# --- sitemap (via robots.txt), Shopify, API ---
recus.clear()
pages.update({
 "/robots.txt": "User-agent: *\nSitemap: %s/sm-index.xml" % base,
 "/sm-index.xml": "<sitemapindex><sitemap><loc>%s/sm-produits.xml</loc></sitemap></sitemapindex>" % base,
 "/sm-produits.xml": "<urlset><url><loc>%s/p/coffret-pokemon-a</loc></url><url><loc>%s/p/lego-b</loc></url></urlset>" % (base, base),
 "/products.json?limit=250&page=1": json.dumps({"products":[{"title":"ETB Pokémon Flammes","handle":"etb-flammes","tags":[],"variants":[{"available":False,"price":"54.90"}]},{"title":"Magic deck","handle":"magic","variants":[{"available":True}]}]}),
 "/api/search?q=pokemon": json.dumps({"data":{"items":[{"name":"Display Pokémon EV11","link":"/p/display-ev11","stock":"OUT"}]}}),
})
cfg2 = {"discord_webhook": base+"/hook", "magasins": {"Shop2": {"site": base, "sitemaps":["auto"], "shopify": base,
        "api":[{"url": base+"/api/search?q=pokemon","liste":"data.items","champ_nom":"name","champ_url":"link","champ_dispo":"stock","valeurs_en_stock":["IN"]}]}}}
e2 = {"produits":{}, "vus":{}, "suivis":{}}
m.une_passe(cfg2, e2); assert recus == [], recus
pages["/sm-produits.xml"] = pages["/sm-produits.xml"].replace("</urlset>", "<url><loc>%s/p/tripack-pokemon-c</loc></url></urlset>" % base)
pages["/products.json?limit=250&page=1"] = pages["/products.json?limit=250&page=1"].replace("false", "true")
pages["/api/search?q=pokemon"] = pages["/api/search?q=pokemon"].replace('"OUT"', '"IN"')
m.une_passe(cfg2, e2)
t = sorted(x["embeds"][0]["title"] for x in recus); print(t)
assert any("NOUVEAU" in x and "tripack" in x for x in t)
assert any("EN STOCK" in x and "Flammes" in x for x in t)
assert any("EN STOCK" in x and "EV11" in x for x in t)
assert not any("lego" in x.lower() or "magic" in x.lower() for x in t)
assert base+"/p/tripack-pokemon-c" in e2["suivis"]["Shop2"]
print("sitemap / shopify / api OK")
