#!/usr/bin/env python3
"""Génère le pack audio précompilé et son manifeste (ADR-0004).

À lancer À LA MAIN, jamais en CI, jamais au runtime — même discipline que
« generer_table_genre.py ». La passe complète coûte des dizaines d'heures de
GPU : elle se lance en connaissance de cause, après le banc
(« banc_precompilation.py ») qui vérifie l'extrapolation à ±20 %.

## Ce que produit cet outil

    <pack>/manifeste.json      index : id → empreinte, canal, fichier, durée
    <pack>/audio/<id>.wav      un fichier par réplique

Le manifeste NE CONTIENT PAS les textes en clair : seulement leur empreinte
(crc32 du texte français, même famille que « genre.jetons »). C'est la règle
de l'ADR-0003 reprise ici — un index de faits, pas une copie de contenu.
L'audio, lui, EST du contenu dérivé : le pack est un artefact local dont la
distribution est un geste séparé, décidé hors du dépôt (ADR-0004 §3).

## Mise à jour incrémentale (ADR-0004 §2)

Relancer l'outil ne régénère JAMAIS tout. Pour chaque id :

    absent du manifeste      → synthétiser (réplique nouvelle)
    empreinte différente     → re-synthétiser (Ankama a corrigé le texte)
    empreinte identique      → ne rien faire
    id disparu de la source  → conserver, marquer « orphelin »

C'est cette mécanique qui rend le pré-calcul soutenable : une mise à jour de
contenu touche quelques centaines de répliques, soit quelques minutes de GPU
au lieu de la passe entière.

Usage :
    python3 outils/generer_pack_audio.py --source FICHIER --pack DOSSIER
                                         [--canal pnj_masculin] [--limite N]
                                         [--sec-a-blanc]

Exige le venv XTTS (torch + coqui-tts), sauf en « --sec-a-blanc » qui
n'inventorie que le travail à faire, sans charger le modèle ni synthétiser.
"""

import argparse
import contextlib
import datetime
import json
import os
import sys
import tempfile
import time
import types
import wave
import zlib

# Le découpage en phrases doit être EXACTEMENT celui du runtime : le pack
# précompile ce que le moteur aurait produit en direct, sinon le repli
# « pack absent » ne sonne pas comme le pack. On importe donc le vrai
# « keraconte.text » — mais via un paquet SQUELETTE, comme le fait déjà
# « generer_table_genre.py » : l'__init__ du paquet tire cv2/PySide6, dont
# cet outil n'a que faire (et qui manquent dans le venv XTTS).
_RACINE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if "keraconte" not in sys.modules:
    _squelette = types.ModuleType("keraconte")
    _squelette.__path__ = [os.path.join(_RACINE, "keraconte")]
    sys.modules["keraconte"] = _squelette

from keraconte.text import pronounce, speakable, split_sentences  # noqa: E402

# Canaux de l'ADR-0001. Le pack déclare la voix employée PAR CANAL : une
# installation qui complète le pack doit pouvoir utiliser exactement les
# mêmes voix, sinon le joueur entend le timbre changer (ADR-0004 §4).
VOIX_PAR_CANAL = {
    "pnj_masculin": "Damien Black",
    "pnj_feminin": "Daisy Studious",
    "narration": "Royston Min",
}

MODELE = "tts_models/multilingual/multi-dataset/xtts_v2"

# Mesurés le 2026-08-10 sur RTX 3070 Ti libre, SUR LE CHEMIN DÉCOUPÉ — celui
# que « synthetiser » emprunte réellement, phrase par phrase. Le banc mesure
# le bloc entier et donne 0,229× / 16,1 car/s ; le découpage coûte +2,6 %
# (comparé sur 40 ids identiques, 148 appels pour 40 répliques). C'est cette
# valeur-ci qu'il faut pour estimer une passe, pas celle du banc.
RATIO_SYNTHESE = 0.233
DEBIT_CAR_PAR_S = 16.4


def empreinte(texte):
    """Empreinte stable du texte source, pour décider de la péremption.

    « zlib.crc32 » et non « hash() » : le hachage de Python est salé par
    processus — un manifeste écrit hier ne se relirait pas aujourd'hui.
    C'est le même choix, pour la même raison, que « genre.jetons ».

    Porte sur le texte ENTIER (pas sur son vocabulaire) : ici on ne cherche
    pas à rapprocher deux lectures d'un même texte, on veut détecter la
    moindre correction d'Ankama — une virgule change la prosodie.
    """
    return zlib.crc32(texte.encode("utf-8"))


def charger_manifeste(pack):
    """Lit le manifeste existant, ou en fabrique un vide."""
    chemin = os.path.join(pack, "manifeste.json")
    try:
        with open(chemin, encoding="utf-8") as fichier:
            return json.load(fichier)
    except FileNotFoundError:
        return {
            "version": 1,
            "modele": MODELE,
            "voix": {},
            "entrees": {},
        }


def ecrire_manifeste(pack, manifeste):
    chemin = os.path.join(pack, "manifeste.json")
    provisoire = chemin + ".tmp"
    with open(provisoire, "w", encoding="utf-8") as fichier:
        json.dump(manifeste, fichier, ensure_ascii=False, indent=1)
    # Remplacement atomique : une passe interrompue ne laisse jamais un
    # manifeste tronqué, qui ferait re-synthétiser tout le pack.
    os.replace(provisoire, chemin)


def plan_de_travail(repliques, manifeste, canal):
    """Trie les répliques en (à faire, inchangées) et repère les orphelins."""
    a_faire, inchangees = [], []
    entrees = manifeste["entrees"]
    for replique in repliques:
        cle = f"{replique['id']}:{canal}"
        marque = empreinte(replique["texte"])
        connue = entrees.get(cle)
        if connue and connue.get("empreinte") == marque:
            fichier = os.path.join("audio", connue["fichier"])
            if os.path.isfile(os.path.join(manifeste["_pack"], fichier)):
                inchangees.append(replique)
                continue
        a_faire.append(replique)
    vus = {f"{r['id']}:{canal}" for r in repliques}
    orphelins = [
        cle for cle, val in entrees.items()
        if cle not in vus and val.get("canal") == canal
    ]
    return a_faire, inchangees, orphelins


def _charger_moteur():
    """Charge XTTS sur CUDA (même rustine que keraconte/engines/xtts.py)."""
    import torch
    import transformers.pytorch_utils as pu

    if not hasattr(pu, "isin_mps_friendly"):
        pu.isin_mps_friendly = lambda elements, test_elements: torch.isin(
            elements, test_elements
        )
    if not torch.cuda.is_available():
        print("CUDA indisponible : le pack se génère sur GPU. Arrêt.", file=sys.stderr)
        raise SystemExit(1)

    from TTS.api import TTS

    return TTS(MODELE).to("cuda")


def _duree_wav(chemin):
    with contextlib.closing(wave.open(chemin, "rb")) as fichier:
        return fichier.getnframes() / float(fichier.getframerate())


def phrases_a_dire(texte):
    """Les segments que le runtime synthétiserait, dans le même ordre.

    Même chaîne que « XttsEngine.speak » : « pronounce » puis découpe, et on
    écarte les segments sans phonème — sans ce filtre le moteur concatène une
    liste vide et lève (bug déjà rencontré sur Kokoro).
    """
    return [
        phrase
        for phrase in split_sentences(pronounce(texte))
        if speakable(phrase)
    ]


def synthetiser(moteur, texte, voix, chemin):
    """Synthétise une réplique entière en UN wav, phrase par phrase.

    Le découpage n'est pas une optimisation de latence ici (rien ne presse
    hors ligne) : c'est la fidélité au runtime. XTTS avertit au-delà de 273
    caractères en français — « this might cause truncated audio » — et le p90
    du corpus est à 409. Le moteur du dépôt ne rencontre jamais cette limite
    parce qu'il découpe ; le pack doit faire pareil, sinon il précompile un
    audio que le direct n'aurait pas produit.

    Mesuré au banc du 2026-08-10 : sans découpage, le débit de parole MONTE
    avec la longueur (12,0 car/s sous 80 caractères, 16,9 au-dessus de 450)
    — donc rien n'était tronqué sur cet échantillon. L'avertissement est
    conservateur, mais on ne parie pas là-dessus sur 55 037 répliques.
    """
    phrases = phrases_a_dire(texte)
    if not phrases:
        raise ValueError("aucune phrase prononçable")

    dossier = tempfile.mkdtemp(prefix="pack-phrase-")
    morceaux = []
    try:
        for rang, phrase in enumerate(phrases):
            bout = os.path.join(dossier, f"{rang}.wav")
            moteur.tts_to_file(
                text=phrase, language="fr", speaker=voix, file_path=bout
            )
            morceaux.append(bout)
        with wave.open(morceaux[0], "rb") as premier:
            parametres = premier.getparams()
        with wave.open(chemin, "wb") as sortie:
            sortie.setparams(parametres)
            for bout in morceaux:
                with wave.open(bout, "rb") as entree:
                    sortie.writeframes(entree.readframes(entree.getnframes()))
    finally:
        for bout in morceaux:
            with contextlib.suppress(OSError):
                os.unlink(bout)
        with contextlib.suppress(OSError):
            os.rmdir(dossier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True,
                        help="JSON {repliques: [{id, texte}]} (echantillon_repliques.py)")
    parser.add_argument("--pack", required=True, help="dossier du pack à écrire")
    parser.add_argument("--canal", default="pnj_masculin", choices=sorted(VOIX_PAR_CANAL))
    parser.add_argument("--limite", type=int, help="borner le nombre de répliques")
    parser.add_argument("--sec-a-blanc", action="store_true",
                        help="inventorier le travail sans rien synthétiser")
    args = parser.parse_args()

    with open(args.source, encoding="utf-8") as fichier:
        source = json.load(fichier)
    repliques = source["repliques"]
    if args.limite:
        repliques = repliques[: args.limite]

    os.makedirs(os.path.join(args.pack, "audio"), exist_ok=True)
    manifeste = charger_manifeste(args.pack)
    manifeste["_pack"] = args.pack  # transitoire, retiré avant écriture
    a_faire, inchangees, orphelins = plan_de_travail(repliques, manifeste, args.canal)

    print(f"canal {args.canal} — {len(repliques)} répliques en source")
    print(f"  à synthétiser : {len(a_faire)}")
    print(f"  inchangées    : {len(inchangees)}")
    print(f"  orphelines    : {len(orphelins)}")

    if args.sec_a_blanc:
        caracteres = sum(len(r["texte"]) for r in a_faire)
        heures = (caracteres / DEBIT_CAR_PAR_S) * RATIO_SYNTHESE / 3600
        print(f"  {caracteres} caractères → ≈ {heures:.2f} h de GPU estimées")
        return

    if not a_faire:
        print("rien à faire.")
        manifeste.pop("_pack", None)
        ecrire_manifeste(args.pack, manifeste)
        return

    voix = VOIX_PAR_CANAL[args.canal]
    depart_charge = time.monotonic()
    moteur = _charger_moteur()
    print(f"modèle chargé en {time.monotonic() - depart_charge:.1f} s", file=sys.stderr)

    manifeste["voix"][args.canal] = voix
    fait, echecs, depart = 0, 0, time.monotonic()
    for rang, replique in enumerate(a_faire, start=1):
        nom = f"{replique['id']}-{args.canal}.wav"
        chemin = os.path.join(args.pack, "audio", nom)
        try:
            synthetiser(moteur, replique["texte"], voix, chemin)
        except Exception as erreur:  # noqa: BLE001 — un échec ne tue pas la passe
            print(f"  id {replique['id']} : {erreur}", file=sys.stderr)
            echecs += 1
            continue
        manifeste["entrees"][f"{replique['id']}:{args.canal}"] = {
            "empreinte": empreinte(replique["texte"]),
            "canal": args.canal,
            "fichier": nom,
            "duree_s": round(_duree_wav(chemin), 2),
        }
        fait += 1
        # Manifeste réécrit régulièrement : une passe de plusieurs dizaines
        # d'heures DOIT pouvoir être interrompue sans perdre le travail fait.
        if fait % 25 == 0:
            sauvegarde = dict(manifeste)
            sauvegarde.pop("_pack", None)
            ecrire_manifeste(args.pack, sauvegarde)
            ecoule = time.monotonic() - depart
            reste = (len(a_faire) - rang) * ecoule / rang
            print(f"  {rang}/{len(a_faire)} — {ecoule / 60:.1f} min écoulées, "
                  f"≈ {reste / 60:.1f} min restantes", file=sys.stderr)

    manifeste.pop("_pack", None)
    manifeste["date"] = datetime.date.today().isoformat()
    manifeste["source"] = source.get("source")
    ecrire_manifeste(args.pack, manifeste)

    duree = time.monotonic() - depart
    print(f"\nPack écrit : {args.pack}")
    print(f"  {fait} synthétisées, {echecs} échecs, en {duree / 60:.1f} min")
    print(f"  {len(manifeste['entrees'])} entrées au manifeste")


if __name__ == "__main__":
    main()
