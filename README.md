# MyGES Calendar Fetcher

Récupère vos données de planning MyGES via une connexion CAS automatisée et les exporte en JSON et ICS.

## Installation

1. Copiez `.env.example` vers `.env`.
2. Renseignez `MYGES_USERNAME` et `MYGES_PASSWORD`.
3. Lancez :

```bash
python3 myges_calendar.py
```

Ceci génère deux fichiers nommés d'après le lundi de la semaine cible :

- `output/semaine-du-24-03.json`
- `output/semaine-du-24-03.ics`

## Options utiles

Récupérer 4 semaines à partir d'une date appartenant à la première semaine :

```bash
python3 myges_calendar.py --week-of 2026-03-23 --weeks 4
```

Choisir des chemins de sortie personnalisés :

```bash
python3 myges_calendar.py \
  --json-out output/april.json \
  --ics-out output/april.ics
```

Ignorer la récupération des détails par événement (plus rapide, mais les noms de matières restent tronqués) :

```bash
python3 myges_calendar.py --no-details
```

## Remarques

- Les identifiants restent dans `.env`, qui est ignoré par git.
- Par défaut, le script récupère les détails complets de chaque événement (nom de la matière, enseignant, salle, type, modalité) via un second appel AJAX (`eventSelect`). Cela ajoute environ 150 ms par événement.
- Utilisez `--no-details` si vous avez uniquement besoin des horaires de début et de fin et que les libellés tronqués vous conviennent.
- Le champ `SUMMARY` du fichier ICS utilise le nom complet de la matière lorsqu'il est disponible, avec l'enseignant, la salle, le type et la modalité dans le champ `DESCRIPTION`.
