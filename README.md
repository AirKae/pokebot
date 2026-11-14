# PokeBot : alertes de stock Pokémon sur Discord

## 1. Créer le webhook Discord (2 min)
1. Crée un serveur Discord perso, puis un salon `#alertes`.
2. Paramètres du salon → Intégrations → Webhooks → Nouveau webhook → **Copier l'URL**.

## 2. Tester sur ton PC
```bash
pip install -r requirements.txt
set DISCORD_WEBHOOK=https://discord.com/api/webhooks/...      # Windows
# export DISCORD_WEBHOOK=https://discord.com/api/webhooks/... # Mac/Linux
python monitor.py --test-discord     # un message doit arriver sur Discord
```

## 3. Ajouter les liens à surveiller (`config.yaml`)
- **produits** : colle le lien d'une page produit (ex. un ETB en rupture chez Cultura).
- **recherches** : fais une recherche « pokemon » sur le site, copie l'URL de la
  page de résultats, et indique dans `motif_lien` un morceau d'URL commun aux
  pages produits (ouvre 2-3 produits pour voir ce qui se répète).

Puis lance le diagnostic :
```bash
python monitor.py --doctor
```
- 🟢 / 🔴 : le bot lit bien le stock de la page.
- ❓ : la page est lue mais le stock n'est pas détecté → ajoute des mots dans
  `mots_rupture` / `mots_stock` pour ce magasin.
- ⛔ : le site bloque le bot (fréquent chez Carrefour, Leclerc, Auchan).
- 🔎 0 produit : le `motif_lien` est faux ou la page est chargée en JavaScript.

## 4. Le faire tourner 24h/24 gratuitement (GitHub Actions)
1. Crée un compte GitHub, puis un dépôt **public** et envoie-y ces fichiers.
   (Public = minutes GitHub Actions illimitées et gratuites. Un dépôt privé n'a
   que 2 000 min/mois, insuffisant pour une vérification toutes les 5 min.
   Ton webhook reste secret car il est dans les « Secrets », pas dans les fichiers.)
2. Settings → Secrets and variables → Actions → New secret
   `DISCORD_WEBHOOK` = l'URL du webhook.
3. Onglet Actions → active les workflows → « PokeBot » → **Run workflow**.

Le bot vérifie ensuite toutes les ~5 à 15 min. Au premier passage il mémorise
l'existant sans alerter ; ensuite il prévient quand un produit passe en stock
ou quand un nouveau produit Pokémon apparaît dans une recherche.

**Alternative plus rapide** : sur un PC allumé en permanence,
`python monitor.py --loop 180` (toutes les ~3 min). Ne descends pas beaucoup plus
bas : tu risques de te faire bloquer.

## 5. Les sources « avancées »
- **Sitemaps** (`sitemaps: ["auto"]`) : les sites publient la liste de toutes
  leurs pages pour Google. Le bot la relit et te prévient dès qu'une fiche
  Pokémon y est ajoutée, souvent avant que le produit soit achetable. Les
  produits découverts sont ensuite suivis automatiquement pour le stock.
  Certains sites ne mettent leur sitemap à jour qu'une fois par jour.
- **Shopify** : si une boutique tourne sous Shopify (essaie
  `https://lesite.com/products.json` dans ton navigateur : si tu vois du texte
  JSON, c'est Shopify), mets son adresse dans `shopify:`. C'est la source la plus
  rapide et la plus fiable.
- **API interne** : sur le site, ouvre les outils développeur (F12) → onglet
  *Réseau* → filtre *Fetch/XHR*, puis fais une recherche « pokemon ». Cherche une
  requête qui renvoie la liste des produits en JSON. Copie son URL dans `api:`
  et indique où sont le nom, le lien et le stock (envoie-moi un extrait si tu
  bloques).

Lance toujours `python monitor.py --doctor` après une modification.

## Configuration Maisons-Alfort et ventes réservées

Décompresse le ZIP. Depuis le dossier qui contient monitor.py, installe les dépendances avec
`python -m pip install -r requirements.txt`. Lance `python tests/test_bot.py` puis
`python tests/test_veille.py` pour les tests hors ligne. Lance `python monitor.py --doctor`
pour contrôler la lecture depuis ton réseau. Pour le service en continu sur Windows,
utilise lancer_bot.bat. Pour GitHub Actions, mets **le contenu** de pokebot/ à la racine du dépôt,
y compris .github/. Configure DISCORD_WEBHOOK dans Settings → Secrets and variables → Actions.
Lance PokeBot dans Actions → Run workflow. Utilise un seul déploiement par historique.
Pour un dépôt existant, conserve state.json. En dépôt public, l'historique des produits et
les mots de suivi inscrits dans state.json seront visibles.

### Sorties et préférences dans Discord
Le calendrier public Radar Stock propose les fiches produit liées depuis son calendrier Pokémon.
Au premier passage les fiches identifiables sont proposées, puis seulement les nouvelles URL.
La couverture dépend des liens publiés. Une annonce ne garantit aucune disponibilité.
Pour répondre dans le salon, crée une application bot sur le portail développeur Discord,
active Message Content Intent dans Bot, invite le bot avec Voir le salon et Voir les anciens messages.
Renseigne trois secrets GitHub supplémentaires (variables d'environnement sous Windows) :
DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID et DISCORD_OWNER_ID. Les deux ID se copient
dans Discord après activation du mode développeur. Utilise le même salon que le webhook.
Seuls les messages de ce compte servent aux préférences, lus au prochain passage.
Exemple : `!suivre mentali ultra premium` ou `!ignorer calendrier avent`.
Choisis des mots présents dans les titres marchands ou leurs URL. Tous les mots doivent
correspondre. Les produits sélectionnés sont épinglés et contrôlés par lots de quatre
en plus de la rotation normale (douze fiches par enseigne par défaut).
La veille générale continue. Les filtres ignorer portent sur les fiches et les alertes
de stock en ligne. Un webhook seul permet uniquement de recevoir des notifications.

### Campagnes
Fnac et Micromania : l'index Radar Stock sert à repérer des URL officielles. Le bot ouvre
ces pages et recherche les indices d'un formulaire disponible. Un formulaire fermé,
illisible ou chargé uniquement en JavaScript peut échapper à la détection. Tu peux aussi
ajouter des liens officiels dans campagnes.pages. Amazon : le catalogue public MapTCG
fournit des ASIN candidats. Pour annoncer une invitation, le bot cherche le contrôle
« Demander une invitation » sur la fiche Amazon elle-même. Un statut visible seulement
après connexion ne sera pas détecté. Aucun compte marchand ou e-mail n'est connecté.
Un lien d'inscription est différent d'une invitation personnelle qui autorise un achat.
Les pages d'une même enseigne sont espacées ; un HTTP 429 suspend les nouvelles lectures
de cet hôte selon Retry-After. La fréquence réelle dépend de la durée des passages.

### Stock local
Le centre approximatif de Maisons-Alfort et un rayon de 10 km à vol d'oiseau sont
configurés ; Créteil, Thiais, Fresnes et Maisons-Alfort sont aussi acceptées par nom.
Les sources locales doivent être des pages publiques propres au produit affichant
un nom de magasin et un statut sur une même ligne. Aucune source locale opérationnelle
n'est fournie dans le ZIP. Pour chaque page vérifiée dans le HTML reçu, ajouter une
entrée dans stocks_locaux avec enseigne, produit, url, ville, magasin, ligne_css,
magasin_css, stock_css, disponible et indisponible (deux listes de libellés exacts).
Hors des quatre villes, fournir coordonnees: [latitude, longitude].
Le premier constat positif et le retour de disponibilité déclenchent une alerte.
Les enseignes qui affichent les stocks après sélection interactive exigent un adaptateur
spécifique. Un retrait sous plusieurs jours ne prouve pas la présence en rayon.

### Alerte sur chaque variation quantitative observée

Réponds directement à l’annonce d’une sortie avec `!suivre`. Le choix reste enregistré
avant la publication des fiches marchandes. Pour les variantes de nom, tu peux aussi
choisir des mots distinctifs avec `!suivre mentali ultra premium`. Le bot recherche ces
mots sur les fiches découvertes, identifie leur SKU ou EAN et vérifie les quantités locales
publiées pour les articles sélectionnés. Le premier relevé positif déclenche une alerte.
Une page sans quantité locale laisse le stock inconnu.

* **Flux d'inventaire fourni par une enseigne ou un partenaire.** Renseigne
  INSTORE_FEED_URL et, si nécessaire, INSTORE_FEED_TOKEN (Bearer), dans les secrets
  Actions ou les variables d'environnement locales. Le contrat JSON attendu est une
  liste de relevés horodatés. Exemple fictif de ligne :
  `{"enseigne":"Fnac","magasin_id":"creteil","reference":"9342125","magasin":"Fnac Créteil","produit":"Coffret Pokémon","code_postal":"94012","date":"2026-11-10T18:34:00+01:00","quantite":4}`.
  Facultatifs : url produit et coordonnees [latitude,longitude]. Pour un historique
  de mouvements, fournir les relevés en ordre chronologique. Le premier initialise le
  compteur et déclenche une alerte si la quantité est positive. Une quantité inconnue, absente ou un relevé périmé conserve le dernier chiffre.
  Pour une page HTML publique déjà configurée, quantite_css désigne une cellule contenant
  uniquement le nombre exact, dans la ligne du magasin.

Une baisse
peut aussi correspondre à une réservation ou à un ajustement. Le mode GitHub traite
les données au prochain passage. Un relevé périodique montre le changement net depuis
le précédent. Test hors réseau : python tests/test_instore.py.

### Plusieurs salons sources pour les stocks locaux

Dans ton serveur, crée un salon de réception distinct du salon où le bot envoie ses
alertes. Sur chaque salon d’annonces accessible proposant « Suivre », choisis ton
salon de réception. Seuls les messages que l'éditeur publie pour ses abonnés y arrivent.
Tu peux aussi utiliser directement des salons où le compte bot est invité et peut
voir le salon et lire l’historique. Active Message Content Intent. Dans les secrets
Actions, configure DISCORD_INSTORE_CHANNEL_IDS avec les identifiants de ces salons,
séparés par des virgules. DISCORD_BOT_TOKEN sert à la lecture, DISCORD_WEBHOOK aux alertes.
Le premier passage mémorise la position actuelle. Chaque passage traite ensuite
jusqu'à 100 nouveaux messages par salon. Le lecteur reconnaît les embeds comportant
un titre correspondant à une sélection, un magasin de la zone, une enseigne configurée
et une quantité sous la forme « 5 → 4 ». Les formats différents nécessitent un adaptateur.
