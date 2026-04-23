# Car Alerts Bot

Bot qui lit **tes emails d'alerte officiels** Leboncoin / LaCentrale / AutoScout24,
score les annonces (bonne affaire ou pas), et pousse les meilleures sur Telegram.

**100% légal** : zéro scraping, zéro contournement anti-bot. Le bot lit uniquement
les emails que les sites t'envoient officiellement, dans ta propre boîte Gmail.

## Architecture

```
[Tu configures tes recherches sur les 3 sites via leur UI officielle]
       ↓
[Les sites t'envoient les alertes par email quand il y a une nouvelle annonce]
       ↓
[Le bot lit tes Gmail via IMAP toutes les 20 min (GitHub Actions)]
       ↓
[Parse les emails → extrait titre, prix, année, km, URL, image]
       ↓
[Filtre (price_max, year_min, mileage_max, mots-clés)]
       ↓
[Score (prix vs cote + keywords + km)]
       ↓
[Push les bonnes affaires sur Telegram (triées par score)]
```

## Setup (15 min)

### 1. Alertes email sur les 3 sites (déjà fait ✅)

- Leboncoin → recherche → cloche → "Recevoir par email" (reçu sur timdelmas123@gmail.com)
- LaCentrale → recherche → "Créer une alerte" (reçu sur chabodt@gmail.com)
- AutoScout24 → recherche → "Enregistrer la recherche" (reçu sur chabodt@gmail.com)

### 2. Mot de passe d'application Google (pour chaque Gmail)

Pour chaque Gmail (chabodt ET timdelmas123) :

1. Active la validation en 2 étapes : https://myaccount.google.com/security
2. Va sur : https://myaccount.google.com/apppasswords
3. Crée un mot de passe d'application (nom : "car-alerts-bot")
4. Note les 16 caractères générés (pas d'espace) — **tu ne les reverras pas**

### 3. Telegram bot (déjà fait ✅)


### 4. Secrets GitHub

Sur le repo GitHub → Settings → Secrets and variables → Actions → New repository secret

Ajoute ces 6 secrets :

| Nom | Valeur |
|-----|--------|
| `GMAIL_USER` | `chabodt@gmail.com` |
| `GMAIL_APP_PASSWORD` | le mot de passe d'app de chabodt |
| `GMAIL_USER_2` | `timdelmas123@gmail.com` |
| `GMAIL_APP_PASSWORD_2` | le mot de passe d'app de timdelmas123 |
| `TELEGRAM_BOT_TOKEN` | `8439322451:AAETNVYssZEJ4z54vWynwIPQokgN715nwzY` |
| `TELEGRAM_CHAT_ID` | `1457918572` |

### 5. Premier run

Actions → Car Alerts Bot → Run workflow → mode: **seed**

Le mode seed marque toutes les annonces actuelles comme "déjà vues" pour ne pas
spammer Telegram avec des anciennes alertes. À partir de là, seules les nouvelles
annonces te seront notifiées.

Ensuite le workflow tourne automatiquement toutes les 20 min.

## Modifier les recherches

Édite `config.yaml`. Exemple pour ajouter une Golf VII :

```yaml
searches:
  - name: "Golf VII TDI"
    keywords_must: ["golf"]
    keywords_nice: ["tdi", "bluemotion", "entretien", "distribution faite"]
    keywords_bad: ["accident", "moteur hs", "épave"]
    price_min: 4000
    price_max: 12000
    year_min: 2013
    year_max: 2019
    mileage_max: 220000
    market_price_ref: 8500
```

`market_price_ref` = ta référence de cote marché. Plus le prix de l'annonce est
sous cette valeur, plus le score sera élevé.

## Commandes locales

```bash
pip install -r requirements.txt

# Test sans envoyer (voit ce qui serait poussé)
GMAIL_USER=... GMAIL_APP_PASSWORD=... python main.py --dry-run

# Marque tout comme vu (pour démarrage propre)
python main.py --seed

# Run normal
python main.py
```

## Structure

```
car-alerts-bot/
├── config.yaml           # Tes recherches + scoring
├── main.py               # Orchestration
├── imap_client.py        # Lecture Gmail IMAP (multi-comptes)
├── parsers/
│   ├── leboncoin.py      # Parse emails Leboncoin
│   ├── lacentrale.py     # Parse emails LaCentrale
│   ├── autoscout24.py    # Parse emails AutoScout24
│   └── common.py         # Utilitaires
├── scoring.py            # Scoring des annonces
├── state.py              # Mémoire des annonces déjà vues
├── notifier.py           # Telegram
├── requirements.txt
└── .github/workflows/run.yml  # Cron GitHub Actions
```

## Troubleshooting

**"IMAP login failed"** → vérifie que tu as bien créé un *mot de passe d'application*
et pas tenté d'utiliser ton vrai mot de passe Gmail (Google bloque par défaut).

**"0 emails récupérés"** → les alertes ne sont peut-être pas encore arrivées.
Force une recherche manuelle sur les sites pour déclencher une alerte, ou vérifie
l'onglet "Tous les messages" Gmail (les alertes peuvent aller en "Promotions").

**"0 annonces extraites"** → les formats d'emails changent parfois. Regarde les
logs du workflow GitHub Actions pour voir ce qui a été parsé.
