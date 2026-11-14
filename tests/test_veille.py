"""Régressions sur les campagnes, la rotation, les préférences et le stock local."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor as m
from veille import appliquer_commandes, dans_zone, etat_campagne, correspond

cfg = m.charger_config()
zone = cfg["zone"]
assert dans_zone(zone, {"ville": "Fresnes"})
assert dans_zone(zone, {"coordonnees": zone["centre"]})
assert not dans_zone(zone, {"coordonnees": [45, 2]})
assert etat_campagne('<form>Inscrivez-vous<input type=email></form>', 'formulaire') == 'ouverte'
assert etat_campagne('<h1>Inscriptions closes</h1><form><input type=email></form>', 'formulaire') == 'fermee'
assert etat_campagne('<button>Demander une invitation</button>', 'invitation') == 'ouverte'
assert not etat_campagne('<button disabled>Demander une invitation</button>', 'invitation')
prefs = {}
appliquer_commandes([{"id":"1","author":{"id":"moi"},"content":"!suivre Étincelles"},
                     {"id":"2","author":{"id":"autre"},"content":"!ignorer Étincelles"}], "moi", prefs)
assert prefs == {"etincelles": True} and correspond("etincelles", "ETB Étincelles")
ctx = {"etat": {}, "nom": "Fnac", "mag": {}, "doctor": False, "mention": "", "webhook":"test",
       "zone":zone, "local":[{"enseigne":"Fnac","magasin":"Fnac Créteil","ville":"Créteil",
        "produit":"ETB", "url":"https://example.org/produit", "ligne_css":"li","magasin_css":"b",
        "stock_css":"i","disponible":["en rayon"],"indisponible":["indisponible"]}]}
def ligne(nom, stock):
    return f"<li><b>{nom}</b><i>{stock}</i></li>"
with patch.object(m, "_pause"), patch.object(m, "envoyer_discord") as alerte:
    for statut in ["indisponible","en rayon","inconnu","en rayon"]:
        with patch.object(m, "telecharger", return_value=(200, ligne("Fnac Paris","en rayon") + ligne("Fnac Créteil",statut))):
            m.source_locale(ctx)
    assert alerte.call_count == 1
    ctx["mag"] = {"produits":[f"https://example.org/{i}" for i in range(300)]}
    with patch.object(m,"statut_produit", return_value=(404,None)) as verif:
        for i in range(25):
            m.source_produits(ctx)
        assert len({appel.args[0] for appel in verif.call_args_list}) == 300
with patch.object(m,"telecharger",return_value=(500,"<button>Ajouter au panier</button>")):
    assert m.statut_produit("https://example.org/test", {}) == (500,None)
print("Campagnes, zone, préférences, magasin et rotation de 300 fiches : OK")
