#!/usr/bin/env python3
"""Banc d'essai : combien de temps coûte la précompilation complète (ADR-0004).

À lancer À LA MAIN, sur une machine dont le GPU est LIBRE (jeu fermé) : le
ratio mesuré jeu lancé n'est pas celui d'une passe de précompilation, qui a
la carte pour elle seule.

Ce que ce banc répond, et que le ratio 0,24× de l'ADR ne dit pas :

1. **Le ratio tient-il sur les répliques LONGUES ?** Le 0,24× vient de trois
   répliques de 12 à 171 caractères, alors que le corpus a une médiane de 181
   et un p90 de 409. L'échantillon d'entrée est stratifié exprès.
2. **Quel est le coût FIXE par appel ?** Une régression durée = a + b×car sur
   les mesures : si « a » domine sur une réplique médiane, l'extrapolation de
   l'ADR est optimiste et les 46 h sont un plancher.
3. **Le chargement du modèle (83 s) est-il bien amorti ?** Il est mesuré à
   part et jamais fondu dans le ratio — sur une passe complète il est payé
   une fois, donc négligeable, mais il fausserait un banc court.

Le banc N'ÉCRIT PAS de pack : il synthétise en fichiers temporaires et mesure.
La génération du pack est un autre outil ; ici on décide seulement si la passe
complète vaut d'être engagée, et pour combien d'heures.

Usage :
    python3 outils/banc_precompilation.py --echantillon FICHIER [--n 60]
                                          [--voix NOM] [--json SORTIE]

Exige le venv XTTS (torch + coqui-tts), pas celui du projet.
"""

import argparse
import contextlib
import json
import os
import statistics
import sys
import tempfile
import time
import wave

# Voix nommée du modèle : l'ADR-0002 impose un timbre par canal, et le moteur
# du dépôt (« voice_argument ») préfère une voix nommée à un WAV cloné — les
# défauts d'un échantillon déjà synthétique s'accumulent sinon.
VOIX_DEFAUT = "Damien Black"

# Débit du corpus, mesuré le 2026-08-03 sur l'API DofusDB.
TOTAL_REPLIQUES = 55_037
TOTAL_CARACTERES = 11_300_000


def _charger_moteur(voix):
    """Charge XTTS sur CUDA et renvoie (moteur, secondes de chargement).

    Reprend EXACTEMENT la rustine « isin_mps_friendly » du moteur du dépôt
    (keraconte/engines/xtts.py) : coqui importe ce symbole, retiré de
    transformers 5.x, et sans lui « from TTS.api import TTS » lève.
    """
    depart = time.monotonic()
    import torch
    import transformers.pytorch_utils as pu

    if not hasattr(pu, "isin_mps_friendly"):
        pu.isin_mps_friendly = lambda elements, test_elements: torch.isin(
            elements, test_elements
        )

    if not torch.cuda.is_available():
        print("CUDA indisponible : le banc mesurerait un chemin CPU inutilisable "
              "en production. Arrêt.", file=sys.stderr)
        raise SystemExit(1)

    from TTS.api import TTS

    moteur = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cuda")
    return moteur, time.monotonic() - depart


def _duree_wav(chemin):
    with contextlib.closing(wave.open(chemin, "rb")) as fichier:
        return fichier.getnframes() / float(fichier.getframerate())


def _tirer(repliques, combien):
    """Prend « combien » répliques en respectant l'ordre stratifié d'entrée.

    L'échantillon arrive déjà groupé par strate ; on prélève à pas régulier
    pour garder la couverture en longueur même quand on réduit la taille.
    """
    if combien >= len(repliques):
        return list(repliques)
    pas = len(repliques) / combien
    return [repliques[int(rang * pas)] for rang in range(combien)]


def _regression(points):
    """Moindres carrés sur (caractères, secondes) → (fixe, par_caractere).

    C'est la mesure qui décide si l'extrapolation de l'ADR tient : un coût
    fixe élevé ne se voit pas dans un ratio moyen, mais il domine le total
    quand on multiplie par 55 037 appels.
    """
    n = len(points)
    if n < 2:
        return 0.0, 0.0
    moy_x = sum(x for x, _ in points) / n
    moy_y = sum(y for _, y in points) / n
    variance = sum((x - moy_x) ** 2 for x, _ in points)
    if variance == 0:
        return moy_y, 0.0
    pente = sum((x - moy_x) * (y - moy_y) for x, y in points) / variance
    return moy_y - pente * moy_x, pente


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--echantillon", required=True, help="JSON d'echantillon_repliques.py")
    parser.add_argument("--n", type=int, default=60, help="répliques à synthétiser")
    parser.add_argument("--voix", default=VOIX_DEFAUT, help="voix nommée XTTS")
    parser.add_argument("--json", default=None, help="écrire le relevé en JSON")
    args = parser.parse_args()

    with open(args.echantillon, encoding="utf-8") as fichier:
        echantillon = json.load(fichier)
    repliques = _tirer(echantillon["repliques"], args.n)
    print(f"{len(repliques)} répliques à synthétiser "
          f"({sum(len(r['texte']) for r in repliques)} caractères)", file=sys.stderr)

    moteur, chargement = _charger_moteur(args.voix)
    print(f"modèle chargé en {chargement:.1f} s", file=sys.stderr)

    mesures = []
    dossier = tempfile.mkdtemp(prefix="banc-xtts-")
    depart_total = time.monotonic()
    for rang, replique in enumerate(repliques, start=1):
        chemin = os.path.join(dossier, f"{replique['id']}.wav")
        depart = time.monotonic()
        try:
            moteur.tts_to_file(
                text=replique["texte"],
                language="fr",
                speaker=args.voix,
                file_path=chemin,
            )
        except Exception as erreur:  # noqa: BLE001 — un échec ne doit pas tuer le banc
            print(f"  [{rang}] id {replique['id']} : {erreur}", file=sys.stderr)
            continue
        synthese = time.monotonic() - depart
        audio = _duree_wav(chemin)
        octets = os.path.getsize(chemin)
        mesures.append(
            {
                "id": replique["id"],
                "caracteres": len(replique["texte"]),
                "synthese_s": round(synthese, 3),
                "audio_s": round(audio, 3),
                "ratio": round(synthese / audio, 3) if audio else None,
                "wav_octets": octets,
            }
        )
        os.unlink(chemin)
        if rang % 10 == 0:
            print(f"  {rang}/{len(repliques)}…", file=sys.stderr)
    mural = time.monotonic() - depart_total
    os.rmdir(dossier)

    if not mesures:
        print("aucune mesure : rien à conclure", file=sys.stderr)
        raise SystemExit(1)

    synthese_totale = sum(m["synthese_s"] for m in mesures)
    audio_total = sum(m["audio_s"] for m in mesures)
    caracteres = sum(m["caracteres"] for m in mesures)
    ratio_global = synthese_totale / audio_total
    ratios = [m["ratio"] for m in mesures if m["ratio"]]
    fixe, par_car = _regression([(m["caracteres"], m["synthese_s"]) for m in mesures])

    # Deux extrapolations, volontairement distinctes : le ratio global suppose
    # que le coût suit la durée d'audio (hypothèse de l'ADR) ; le modèle
    # affine « fixe + pente » compte les 55 037 appels un par un. L'écart
    # entre les deux EST le résultat intéressant.
    heures_par_ratio = (TOTAL_CARACTERES / (caracteres / audio_total)) * ratio_global / 3600
    heures_par_modele = (TOTAL_REPLIQUES * fixe + TOTAL_CARACTERES * par_car) / 3600

    releve = {
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "voix": args.voix,
        "n": len(mesures),
        "chargement_s": round(chargement, 1),
        "mural_s": round(mural, 1),
        "synthese_s": round(synthese_totale, 1),
        "audio_s": round(audio_total, 1),
        "caracteres": caracteres,
        "ratio_global": round(ratio_global, 3),
        "ratio_median": round(statistics.median(ratios), 3),
        "car_par_s_audio": round(caracteres / audio_total, 1),
        "cout_fixe_s": round(fixe, 3),
        "cout_par_caractere_s": round(par_car, 5),
        "extrapolation_h_par_ratio": round(heures_par_ratio, 1),
        "extrapolation_h_par_modele": round(heures_par_modele, 1),
        "octets_wav_par_s_audio": round(sum(m["wav_octets"] for m in mesures) / audio_total),
        "mesures": mesures,
    }

    print()
    print(f"  chargement du modèle    : {releve['chargement_s']} s (payé une fois)")
    print(f"  synthèse (hors charg.)  : {releve['synthese_s']} s")
    print(f"  audio produit           : {releve['audio_s']} s")
    print(f"  ratio global            : {releve['ratio_global']}×  "
          f"(médian {releve['ratio_median']}×, ADR : 0,24×)")
    print(f"  débit de parole         : {releve['car_par_s_audio']} car/s "
          f"(ADR : 16,3)")
    print(f"  coût fixe par appel     : {releve['cout_fixe_s']} s")
    print(f"  coût par caractère      : {releve['cout_par_caractere_s']} s")
    print()
    print(f"  passe complète (ratio)  : {releve['extrapolation_h_par_ratio']} h")
    print(f"  passe complète (modèle) : {releve['extrapolation_h_par_modele']} h")
    print(f"  ADR annonce             : 46 h sur cette carte")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fichier:
            json.dump(releve, fichier, ensure_ascii=False, indent=2)
        print(f"\nRelevé écrit : {args.json}")


if __name__ == "__main__":
    main()
