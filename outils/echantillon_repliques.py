#!/usr/bin/env python3
"""Tire un échantillon stratifié de répliques pour le banc XTTS (ADR-0004).

À lancer À LA MAIN, comme « generer_table_genre.py » : jamais en CI, jamais
au runtime. Même source (API communautaire DofusDB), même cadence polie,
même provenance datée.

Ce que ça produit — et ce que ça N'EST PAS :
- un fichier LOCAL « id → texte français », destiné au seul banc d'essai.
  Contrairement à la table de genre (qui n'écrit que des crc32), ce fichier
  contient les textes EN CLAIR : c'est indispensable pour synthétiser, mais
  cela en fait un artefact strictement local, JAMAIS redistribué ni versionné
  — règle héritée de l'ADR-0003 (« redistribuer les dialogues du jeu ≠
  redistribuer des empreintes »).
- l'échantillon est STRATIFIÉ en longueur, pas pris en tête de liste. Le
  ratio 0,24× de l'ADR a été mesuré sur trois répliques de 12 à 171
  caractères, alors que le corpus a une médiane de 181 et un p90 de 409 :
  un échantillon non stratifié re-mesurerait le cas facile et validerait
  l'extrapolation par construction.

Usage :
    python3 outils/echantillon_repliques.py [--sortie CHEMIN] [--taille N]

Un Python NU suffit (stdlib seulement).
"""

import argparse
import datetime
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request

API = "https://api.dofusdb.fr"
PAGE = 50  # taille de page Feathers ($limit)
PAUSE = 0.25  # politesse : quatre requêtes par seconde au plus
ENTETES = {"User-Agent": "keraconte/echantillon-repliques (outil hors ligne)"}

# Bornes de strates, en caractères, adossées à la distribution mesurée le
# 2026-08-03 (moyenne 205,6 ; médiane 181 ; p90 409). Cinq strates : les deux
# dernières portent la queue longue, celle que le banc de l'ADR n'a jamais
# touchée et qui décide de l'extrapolation à 55 037 répliques.
STRATES = [(0, 80), (80, 180), (180, 300), (300, 450), (450, 10_000)]

# Graine fixe : deux exécutions tirent le MÊME échantillon. Un banc dont
# l'échantillon bouge ne compare rien d'une mesure à l'autre.
GRAINE = 20260810


def _requete(chemin, parametres):
    url = f"{API}{chemin}?{urllib.parse.urlencode(parametres, doseq=True)}"
    demande = urllib.request.Request(url, headers=ENTETES)
    with urllib.request.urlopen(demande, timeout=30) as reponse:
        return json.load(reponse)


def _total():
    """Nombre de répliques annoncé par l'API (55 037 le 2026-08-03)."""
    return int(_requete("/npc-messages", {"$limit": 1}).get("total") or 0)


def _moissonner(cible_par_strate, total):
    """Tire des pages AU HASARD dans la plage jusqu'à remplir les strates.

    Parcourir les 55 037 répliques pour en garder 300 serait impoli et
    inutile : on saute directement à des offsets tirés au sort. Chaque page
    ramène 50 répliques dont on classe chacune dans sa strate ; on s'arrête
    quand toutes les strates sont pleines (ou quand les tirages s'épuisent).
    """
    alea = random.Random(GRAINE)
    paniers = {borne: [] for borne in STRATES}
    vus = set()
    offsets = list(range(0, max(total - PAGE, 1), PAGE))
    alea.shuffle(offsets)

    for tentative, saut in enumerate(offsets, start=1):
        if all(len(paniers[b]) >= cible_par_strate for b in STRATES):
            break
        temps = time.monotonic()
        try:
            resultat = _requete("/npc-messages", {"$limit": PAGE, "$skip": saut})
        except OSError as erreur:
            print(f"page {saut} : {erreur}, ignorée", file=sys.stderr)
            continue
        for enregistrement in resultat.get("data") or []:
            identifiant = enregistrement.get("id")
            texte = (enregistrement.get("message") or {}).get("fr")
            if not texte or identifiant is None or identifiant in vus:
                continue
            texte = texte.strip()
            if not texte:
                continue
            vus.add(identifiant)
            for borne in STRATES:
                bas, haut = borne
                if bas <= len(texte) < haut and len(paniers[borne]) < cible_par_strate:
                    paniers[borne].append({"id": identifiant, "texte": texte})
                    break
        if tentative % 10 == 0:
            etat = " ".join(
                f"{bas}-{haut}:{len(paniers[(bas, haut)])}" for bas, haut in STRATES
            )
            print(f"  {tentative} pages — {etat}", file=sys.stderr)
        time.sleep(max(0.0, PAUSE - (time.monotonic() - temps)))
    return paniers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sortie",
        default="echantillon-repliques.json",
        help="fichier JSON à écrire (LOCAL, jamais versionné)",
    )
    parser.add_argument(
        "--taille", type=int, default=300, help="taille visée de l'échantillon"
    )
    args = parser.parse_args()

    total = _total()
    print(f"{total} répliques annoncées par l'API", file=sys.stderr)
    cible = max(1, args.taille // len(STRATES))
    paniers = _moissonner(cible, total)

    repliques = [item for borne in STRATES for item in paniers[borne]]
    strates = [
        {
            "min": bas,
            "max": haut,
            "n": len(paniers[(bas, haut)]),
            "car_moyen": round(
                sum(len(i["texte"]) for i in paniers[(bas, haut)])
                / max(len(paniers[(bas, haut)]), 1),
                1,
            ),
        }
        for bas, haut in STRATES
    ]
    sortie = {
        "version": 1,
        "source": API,
        "date": datetime.date.today().isoformat(),
        "total_api": total,
        "graine": GRAINE,
        "strates": strates,
        "repliques": repliques,
    }
    os.makedirs(os.path.dirname(args.sortie) or ".", exist_ok=True)
    with open(args.sortie, "w", encoding="utf-8") as fichier:
        json.dump(sortie, fichier, ensure_ascii=False)

    caracteres = sum(len(i["texte"]) for i in repliques)
    print(f"Échantillon écrit : {args.sortie}")
    print(f"  {len(repliques)} répliques, {caracteres} caractères")
    for strate in strates:
        print(f"  [{strate['min']}-{strate['max']}[ : {strate['n']} "
              f"(moy. {strate['car_moyen']} car.)")


if __name__ == "__main__":
    main()
