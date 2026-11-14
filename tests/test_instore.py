import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor
import instore

config = monitor.charger_config()
alerte = Mock()
etat = {"preferences":{"coffret":True}}
base = dict(enseigne="Fnac", magasin_id="creteil", reference="etb", magasin="Fnac Créteil",
            produit="Coffret Pokémon", code_postal="94012")
with patch.dict("os.environ", {"INSTORE_FEED_URL":"https://example.org/flux"}), patch.object(instore.requests, "get") as get:
    for i, n in enumerate([5,4,None,4,0,3]):
        get.return_value.json.return_value = [dict(base, date=f"2026-11-10T10:0{i}:00Z", quantite=n)]
        instore.lire_flux(config, etat, alerte)
assert [(a.args[3], a.args[4]) for a in alerte.call_args_list] == [(None,5),(5,4),(4,0),(0,3)]
import json
offre = {"inventoryLevel":{"value":4},"availableAtOrFrom":{"name":"Fnac Créteil","address":{"postalCode":"94012"}}}
produit = {"@type":"Product","sku":"9342125","offers":[offre]}
def observer():
    html = '<script type="application/ld+json">' + json.dumps(produit) + '</script>'
    return list(instore.quantites_fiche(html,"9342125",config["zone"]))
assert observer() == [("Fnac Créteil","94012",4)]
offre["inventoryLevel"] = {"maxValue":8}
assert observer() == []
offre["inventoryLevel"] = {"value":0}
assert observer()[0][2] == 0
del offre["availableAtOrFrom"]
assert observer() == []
print("OK : relevés directs, quantités absentes et rupture")

from veille import appliquer_commandes
etat = {"preferences": {}}
annonce = {"embeds":[{"title":"📅 ANNONCE JCC · Coffret Étoile"}]}
message = {"id":"123","author":{"id":"moi"},"content":"!suivre","referenced_message":annonce}
appliquer_commandes([message], "moi", etat["preferences"])
ctx = {"etat":etat,"nom":"Fnac","mag":{},"zone":config["zone"],"doctor":False,
       "mention":"","webhook":"test"}
url = "https://example.org/p/coffret-etoile"
monitor._alerte_nouveaux(ctx, "catalogue", {url:"Pokémon Coffret Étoile"})
assert url in etat["prioritaires"]["Fnac"]
offre["availableAtOrFrom"] = {"name":"Fnac Créteil","address":{"postalCode":"94012"}}
produit.update(sku="NOUVEAU", name="Coffret Pokémon Étoile")
with patch.object(monitor, "_pause"), patch.object(monitor, "notification_quantite") as envoyer:
    for n in [0,3,3,2]:
        offre["inventoryLevel"] = {"value":n}
        html = '<script type="application/ld+json">' + json.dumps(produit) + '</script>'
        with patch.object(monitor,"telecharger",return_value=(200,html)):
            monitor.source_produits(ctx)
    assert [(a.args[5],a.args[6]) for a in envoyer.call_args_list] == [(0,3),(3,2)]
    message["content"] = "!ignorer"
    appliquer_commandes([message],"moi",etat["preferences"])
    assert not list(instore.quantites_fiche(html,None,config["zone"],etat["preferences"]))
print("OK : sélection avant référencement, arrivée en magasin et baisse")
