"""Tests du générateur de pack audio (outils/generer_pack_audio.py, ADR-0004).

Ce qui est testé ici est la mécanique INCRÉMENTALE (§2 de l'ADR) : elle décide
si des dizaines d'heures de GPU sont dépensées ou non, et c'est la seule
partie de l'outil qui tourne sans carte graphique. La synthèse elle-même n'est
pas testée — elle exige CUDA et le modèle de 2 Go ; le banc la mesure.

Le critère d'acceptation de l'ADR à couvrir : « deux re-scrapes consécutifs
sans mise à jour du jeu ⇒ ZÉRO synthèse déclenchée. Un texte modifié à la main
dans le manifeste ⇒ exactement une re-synthèse. »
"""

import importlib.util
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# L'outil vit hors du paquet « keraconte » (dossier « outils/ », sans
# __init__) : on le charge par chemin, comme un script — c'est ainsi qu'il est
# lancé en vrai.
_OUTIL = (
    pathlib.Path(__file__).resolve().parents[1] / "outils" / "generer_pack_audio.py"
)
_spec = importlib.util.spec_from_file_location("generer_pack_audio", _OUTIL)
pack_audio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pack_audio)


def _pack_avec(tmp_path, repliques, canal="pnj_masculin"):
    """Fabrique un pack déjà « généré » : manifeste + fichiers audio factices.

    On ne synthétise pas : le plan de travail ne regarde que l'empreinte et
    l'existence du fichier, jamais son contenu.
    """
    (tmp_path / "audio").mkdir(parents=True, exist_ok=True)
    entrees = {}
    for replique in repliques:
        nom = f"{replique['id']}-{canal}.wav"
        (tmp_path / "audio" / nom).write_bytes(b"RIFF-factice")
        entrees[f"{replique['id']}:{canal}"] = {
            "empreinte": pack_audio.empreinte(replique["texte"]),
            "canal": canal,
            "fichier": nom,
            "duree_s": 1.0,
        }
    manifeste = {
        "version": 1,
        "modele": pack_audio.MODELE,
        "voix": {canal: pack_audio.VOIX_PAR_CANAL[canal]},
        "entrees": entrees,
        "_pack": str(tmp_path),
    }
    return manifeste


# --- L'empreinte -------------------------------------------------------------


def test_empreinte_stable_entre_deux_processus():
    """Valeur en dur : un manifeste écrit hier doit se relire aujourd'hui.

    C'est LA raison du crc32 plutôt que hash() — salé par processus. Si ce
    test casse, tous les packs existants se re-synthétisent en silence.
    """
    assert pack_audio.empreinte("Bonjour, aventurier !") == 579_154_065


def test_empreinte_distingue_une_virgule():
    """La ponctuation change la prosodie : elle doit périmer l'audio.

    Contrairement à « genre.jetons » qui compare des VOCABULAIRES (pour
    rapprocher deux lectures OCR d'un même texte), l'empreinte du pack porte
    sur le texte entier — on cherche la correction d'Ankama, pas le
    rapprochement flou.
    """
    assert pack_audio.empreinte("Viens, ami.") != pack_audio.empreinte("Viens ami.")


# --- Le plan de travail (§2 de l'ADR) ---------------------------------------


def test_pack_vierge_synthetise_tout(tmp_path):
    repliques = [{"id": 1, "texte": "Bonjour."}, {"id": 2, "texte": "Adieu."}]
    manifeste = pack_audio.charger_manifeste(str(tmp_path))
    manifeste["_pack"] = str(tmp_path)
    a_faire, inchangees, orphelins = pack_audio.plan_de_travail(
        repliques, manifeste, "pnj_masculin"
    )
    assert len(a_faire) == 2
    assert inchangees == []
    assert orphelins == []


def test_deuxieme_passe_identique_ne_synthetise_rien(tmp_path):
    """Critère d'acceptation de l'ADR : zéro synthèse sans mise à jour du jeu.

    C'est ce test qui garantit qu'un re-scrape ne rouvre pas les 40 heures.
    """
    repliques = [{"id": 1, "texte": "Bonjour."}, {"id": 2, "texte": "Adieu."}]
    manifeste = _pack_avec(tmp_path, repliques)
    a_faire, inchangees, _ = pack_audio.plan_de_travail(
        repliques, manifeste, "pnj_masculin"
    )
    assert a_faire == []
    assert len(inchangees) == 2


def test_texte_corrige_declenche_exactement_une_resynthese(tmp_path):
    """Ankama corrige une réplique : elle seule est refaite."""
    repliques = [{"id": 1, "texte": "Bonjour."}, {"id": 2, "texte": "Adieu."}]
    manifeste = _pack_avec(tmp_path, repliques)
    corrigees = [{"id": 1, "texte": "Bonjour !"}, {"id": 2, "texte": "Adieu."}]
    a_faire, inchangees, _ = pack_audio.plan_de_travail(
        corrigees, manifeste, "pnj_masculin"
    )
    assert [r["id"] for r in a_faire] == [1]
    assert [r["id"] for r in inchangees] == [2]


def test_replique_nouvelle_est_la_seule_ajoutee(tmp_path):
    repliques = [{"id": 1, "texte": "Bonjour."}]
    manifeste = _pack_avec(tmp_path, repliques)
    elargies = repliques + [{"id": 7, "texte": "Une quête t'attend."}]
    a_faire, inchangees, _ = pack_audio.plan_de_travail(
        elargies, manifeste, "pnj_masculin"
    )
    assert [r["id"] for r in a_faire] == [7]
    assert len(inchangees) == 1


def test_audio_efface_est_resynthetise_malgre_empreinte_intacte(tmp_path):
    """Le manifeste ne suffit pas : le fichier doit exister sur le disque.

    Sans cette vérification, un pack amputé (copie partielle, disque plein)
    se déclarerait complet et le runtime chercherait des fichiers absents.
    """
    repliques = [{"id": 1, "texte": "Bonjour."}]
    manifeste = _pack_avec(tmp_path, repliques)
    (tmp_path / "audio" / "1-pnj_masculin.wav").unlink()
    a_faire, inchangees, _ = pack_audio.plan_de_travail(
        repliques, manifeste, "pnj_masculin"
    )
    assert [r["id"] for r in a_faire] == [1]
    assert inchangees == []


def test_id_disparu_de_la_source_devient_orphelin(tmp_path):
    """L'ADR conserve les orphelins : un id retiré peut réapparaître."""
    repliques = [{"id": 1, "texte": "Bonjour."}, {"id": 2, "texte": "Adieu."}]
    manifeste = _pack_avec(tmp_path, repliques)
    a_faire, _, orphelins = pack_audio.plan_de_travail(
        [repliques[0]], manifeste, "pnj_masculin"
    )
    assert a_faire == []
    assert orphelins == ["2:pnj_masculin"]
    # Conservé, pas supprimé : le fichier est toujours là.
    assert (tmp_path / "audio" / "2-pnj_masculin.wav").is_file()


def test_un_meme_texte_sur_deux_canaux_sont_deux_entrees(tmp_path):
    """La clé est « id + canal » : deux voix, deux fichiers, deux synthèses.

    C'est ce qui fait que le coût de la passe dépend du nombre de canaux
    réellement employés, pas du seul nombre de répliques.
    """
    repliques = [{"id": 1, "texte": "Bonjour."}]
    manifeste = _pack_avec(tmp_path, repliques, canal="pnj_masculin")
    a_faire, inchangees, orphelins = pack_audio.plan_de_travail(
        repliques, manifeste, "pnj_feminin"
    )
    assert len(a_faire) == 1
    assert inchangees == []
    # L'entrée masculine n'est pas orpheline : elle relève d'un autre canal.
    assert orphelins == []


# --- Le manifeste ------------------------------------------------------------


def test_manifeste_ne_contient_aucun_texte_en_clair(tmp_path):
    """Règle héritée de l'ADR-0003 : un index de faits, pas une copie.

    L'audio est du contenu dérivé — c'est assumé et cadré par l'ADR — mais le
    manifeste, lui, reste un index : il ne redistribue pas les dialogues.
    """
    secret = "Va parler au forgeron de Bonta, il t'attend."
    manifeste = _pack_avec(tmp_path, [{"id": 1, "texte": secret}])
    manifeste.pop("_pack")
    pack_audio.ecrire_manifeste(str(tmp_path), manifeste)
    ecrit = (tmp_path / "manifeste.json").read_text(encoding="utf-8")
    assert secret not in ecrit
    assert "forgeron" not in ecrit
    assert str(pack_audio.empreinte(secret)) in ecrit


def test_manifeste_relu_est_identique(tmp_path):
    """Écriture puis relecture : le cycle incrémental repose là-dessus."""
    repliques = [{"id": 1, "texte": "Bonjour."}]
    manifeste = _pack_avec(tmp_path, repliques)
    manifeste.pop("_pack")
    pack_audio.ecrire_manifeste(str(tmp_path), manifeste)
    relu = pack_audio.charger_manifeste(str(tmp_path))
    assert relu["entrees"] == manifeste["entrees"]
    relu["_pack"] = str(tmp_path)
    a_faire, _, _ = pack_audio.plan_de_travail(repliques, relu, "pnj_masculin")
    assert a_faire == []


def test_ecriture_du_manifeste_est_atomique(tmp_path):
    """Aucun fichier « .tmp » ne survit : une passe interrompue reste lisible.

    Un manifeste tronqué ferait re-synthétiser tout le pack — c'est
    exactement le coût que l'incrémental existe pour éviter.
    """
    manifeste = pack_audio.charger_manifeste(str(tmp_path))
    pack_audio.ecrire_manifeste(str(tmp_path), manifeste)
    assert (tmp_path / "manifeste.json").is_file()
    assert not list(tmp_path.glob("*.tmp"))


# --- Fidélité au runtime -----------------------------------------------------


def test_le_decoupage_est_celui_du_runtime():
    """Le pack doit précompiler CE QUE LE DIRECT AURAIT DIT, phrase à phrase.

    XTTS avertit au-delà de 273 caractères en français (« this might cause
    truncated audio »), relevé au banc du 2026-08-10 ; le moteur du dépôt ne
    rencontre jamais la limite parce qu'il découpe. Si le pack synthétisait
    le bloc entier, il produirait un audio que le direct n'aurait pas rendu —
    et le repli « pack absent » ne sonnerait plus pareil.
    """
    from keraconte.text import pronounce, speakable, split_sentences

    texte = "Bonjour, aventurier. Va voir le forgeron ! Il t'attend."
    attendu = [
        phrase for phrase in split_sentences(pronounce(texte)) if speakable(phrase)
    ]
    assert pack_audio.phrases_a_dire(texte) == attendu
    assert len(attendu) == 3


def test_les_segments_sans_phoneme_sont_ecartes():
    """Un segment vide fait LEVER les moteurs (bug déjà connu sur Kokoro)."""
    assert pack_audio.phrases_a_dire("...") == []
    assert pack_audio.phrases_a_dire("") == []


def test_une_replique_longue_est_decoupee_sous_la_limite_xtts():
    """Le p90 du corpus (409 car.) dépasse la limite : c'est le cas courant."""
    texte = " ".join(f"Voici la phrase numéro {n} de ce dialogue." for n in range(12))
    assert len(texte) > 273
    phrases = pack_audio.phrases_a_dire(texte)
    assert len(phrases) == 12
    assert all(len(phrase) <= 273 for phrase in phrases)


def test_le_pack_declare_une_voix_par_canal():
    """ADR-0004 §4 : un canal rend un seul timbre, déclaré au manifeste.

    Sans cette déclaration, une installation qui complète le pack ne peut pas
    reprendre les mêmes voix — et le joueur entend le timbre changer.
    """
    assert set(pack_audio.VOIX_PAR_CANAL) == {
        "pnj_masculin",
        "pnj_feminin",
        "narration",
    }
    assert len(set(pack_audio.VOIX_PAR_CANAL.values())) == 3
