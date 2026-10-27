"""
Serveur d'administration de MUNKOYO MUSIQUE (à déployer sur Render).

Il fait ce que l'application ne peut pas faire seule et en sécurité :
  - envoyer une notification à TOUS les utilisateurs (Firebase Cloud Messaging)
  - lancer / désactiver l'alerte de mise à jour

Les routes /api/admin/... sont réservées à l'administrateur : chaque requête doit contenir le jeton de
connexion Firebase de ernoerno226@gmail.com (vérifié ici avec le SDK Firebase Admin).
La route /api/message/nouveau est appelée par un utilisateur connecté, pour prévenir l'administrateur
qu'il vient de recevoir un message (le serveur vérifie que le message appartient bien à cet utilisateur).

Variables d'environnement :
  FIREBASE_CREDENTIALS  contenu JSON du compte de service Firebase (OBLIGATOIRE)
  ADMIN_EMAIL           e-mail de l'administrateur (par défaut : ernoerno226@gmail.com)
  SITE_URL              adresse du site de téléchargement (par défaut : https://munkoyomusique.onrender.com).
                        Quand tu passeras à munkoyomusique.online, change seulement cette variable :
                        les applications suivent sans mise à jour.
"""
import hmac
import json
import os
import time

import firebase_admin
import re
import uuid
from firebase_admin import auth, credentials, firestore, messaging
from flask import Flask, jsonify, request

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ernoerno226@gmail.com").strip().lower()
SITE_URL = os.environ.get("SITE_URL", "https://munkoyomusique.onrender.com").strip().rstrip("/")
SUJET = "tous"
CANAL = "annonces"
CANAL_MESSAGES = "messages"

firebase_admin.initialize_app(credentials.Certificate(json.loads(os.environ["FIREBASE_CREDENTIALS"])))
db = firestore.client()
app = Flask(__name__)


def admin_connecte():
    """Retourne les infos du jeton si la requête vient de l'administrateur, sinon None."""
    entete = request.headers.get("Authorization", "")
    if not entete.startswith("Bearer "):
        return None
    try:
        jeton = auth.verify_id_token(entete[7:])
    except Exception:
        return None
    if jeton.get("email_verified") and str(jeton.get("email", "")).strip().lower() == ADMIN_EMAIL:
        return jeton
    return None


def utilisateur_connecte():
    """Retourne le jeton vérifié d'un utilisateur connecté (n'importe lequel), sinon None."""
    entete = request.headers.get("Authorization", "")
    if not entete.startswith("Bearer "):
        return None
    try:
        return auth.verify_id_token(entete[7:])
    except Exception:
        return None


def envoyer_au_sujet(sujet, titre, texte, donnees=None, canal=CANAL):
    message = messaging.Message(
        topic=sujet,
        notification=messaging.Notification(title=titre, body=texte),
        data={k: str(v) for k, v in (donnees or {}).items()},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id=canal),
        ),
    )
    return messaging.send(message)


def envoyer_a_tous(titre, texte, donnees=None):
    return envoyer_au_sujet(SUJET, titre, texte, donnees, CANAL)


@app.get("/")
def accueil():
    return "OK"


@app.post("/api/admin/annonce")
def annonce():
    if not admin_connecte():
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    titre = str(data.get("titre", "")).strip()[:60]
    texte = str(data.get("message", "")).strip()[:240]
    if not titre or not texte:
        return jsonify({"erreur": "titre et message obligatoires"}), 400

    db.collection("munkoyo_annonces").add({"titre": titre, "message": texte, "date": int(time.time() * 1000)})
    identifiant = envoyer_a_tous(titre, texte, {"type": "annonce"})
    return jsonify({"ok": True, "fcm": identifiant})


@app.post("/api/admin/mise-a-jour")
def mise_a_jour():
    if not admin_connecte():
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}

    try:
        code = int(data.get("versionCode", 0))
    except (TypeError, ValueError):
        code = 0
    if code < 1:
        return jsonify({"erreur": "versionCode invalide"}), 400
    actif = bool(data.get("actif", True))
    nom = str(data.get("versionName", ""))[:20]
    texte = str(data.get("message", "")).strip()[:240]

    db.collection("munkoyo_config").document("mise_a_jour").set({
        "actif": actif,
        "versionCode": code,
        "versionName": nom,
        "message": texte,
        "obligatoire": bool(data.get("obligatoire", False)),
        "url": SITE_URL,
        "date": int(time.time() * 1000),
    })

    identifiant = None
    if actif:
        identifiant = envoyer_a_tous(
            "Mise à jour disponible",
            texte or f"La version {nom} de MUNKOYO MUSIQUE est disponible. Appuie ici pour la télécharger sur {SITE_URL.replace('https://', '')}.",
            {"type": "update", "url": SITE_URL},
        )
    return jsonify({"ok": True, "fcm": identifiant})


@app.post("/api/message/nouveau")
def message_nouveau():
    """Un utilisateur vient d'écrire : on prévient l'administrateur (une seule fois par message)."""
    jeton = utilisateur_connecte()
    if not jeton:
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    identifiant = str(data.get("id", "")).strip()
    if not identifiant or "/" in identifiant:
        return jsonify({"erreur": "message inconnu"}), 400

    ref = db.collection("munkoyo_messages").document(identifiant)
    doc = ref.get()
    if not doc.exists or doc.get("uid") != jeton.get("uid"):
        return jsonify({"erreur": "message inconnu"}), 404
    if doc.to_dict().get("notifie_admin"):
        return jsonify({"ok": True, "deja": True})

    envoyer_au_sujet(
        "admin",
        "Nouveau message",
        "Ouvre ta boîte de réception pour le lire.",
        {"type": "message", "mid": identifiant},
        CANAL_MESSAGES,
    )
    ref.update({"notifie_admin": True})
    return jsonify({"ok": True})


@app.post("/api/admin/message/repondre")
def message_repondre():
    """L'administrateur répond : la réponse est enregistrée et l'utilisateur reçoit une notification."""
    if not admin_connecte():
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    identifiant = str(data.get("id", "")).strip()
    reponse = str(data.get("reponse", "")).strip()[:1000]
    if not identifiant or "/" in identifiant or not reponse:
        return jsonify({"erreur": "id et réponse obligatoires"}), 400

    ref = db.collection("munkoyo_messages").document(identifiant)
    doc = ref.get()
    if not doc.exists:
        return jsonify({"erreur": "message inconnu"}), 404

    ref.update({
        "reponse": reponse,
        "statut": "traite",
        "date_reponse": int(time.time() * 1000),
        "reponse_lue": False,
    })
    uid = doc.get("uid")
    if uid:
        envoyer_au_sujet(
            "u_" + str(uid),
            "MalakiLabs a répondu à ton message",
            "Ouvre ta boîte de réception pour lire la réponse.",
            {"type": "reponse", "mid": identifiant},
            CANAL_MESSAGES,
        )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Staffs : image sur Cloudflare R2 (lien d'envoi temporaire) et notifications
# Variables : R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_URL
# La clé secrète R2 ne doit JAMAIS figurer dans l'application : elle reste sur Render.
# ---------------------------------------------------------------------------
def _client_r2():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


@app.post("/api/staff/image-url")
def staff_image_url():
    """Donne à l'administrateur (ou au créateur) un lien pour envoyer une image compressée sur R2."""
    jeton = utilisateur_connecte()
    if not jeton:
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    taille = int(data.get("taille", 0))
    if data.get("type") != "image/jpeg" or taille <= 0 or taille > 2_000_000:
        return jsonify({"erreur": "image refusée (JPEG, 2 Mo maximum)"}), 400
    cle = "staffs/%s.jpg" % uuid.uuid4().hex
    lien = _client_r2().generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["R2_BUCKET"], "Key": cle, "ContentType": "image/jpeg"},
        ExpiresIn=600,
    )
    return jsonify({"uploadUrl": lien, "publicUrl": os.environ["R2_PUBLIC_URL"].rstrip("/") + "/" + cle})


@app.post("/api/staff/notifier")
def staff_notifier():
    """Notifications de staff : bienvenue au nouveau membre, arrivée annoncée aux autres, nouveau message."""
    jeton = utilisateur_connecte()
    if not jeton:
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    type_ = str(data.get("type", ""))
    staff_id = str(data.get("staffId", ""))
    if not re.match(r"^[A-Za-z0-9]{10,40}$", staff_id):
        return jsonify({"erreur": "staff inconnu"}), 400
    uid = jeton.get("uid")
    ref = db.collection("munkoyo_staffs").document(staff_id)
    staff = ref.get()
    if not staff.exists:
        return jsonify({"erreur": "staff inconnu"}), 404
    nom_staff = staff.get("nom") or "staff"

    if type_ == "arrivee":
        # Vérifie que l'appelant est bien membre du staff
        if not ref.collection("membres").document(uid).get().exists:
            return jsonify({"erreur": "accès refusé"}), 403
        nouveau = str(data.get("nom", "Un membre"))[:30]
        envoyer_au_sujet("u_" + uid, "Bienvenue dans « %s » !" % nom_staff,
                         "Tu peux maintenant lire et commenter les messages du staff.",
                         {"type": "staff_bienvenue", "staffId": staff_id, "auteurUid": ""}, CANAL_STAFF)
        envoyer_au_sujet("staff_" + staff_id, "Nouveau membre",
                         "%s a rejoint « %s »." % (nouveau, nom_staff),
                         {"type": "staff_arrivee", "staffId": staff_id, "auteurUid": uid}, CANAL_STAFF)
        return jsonify({"ok": True})

    if type_ == "message":
        mid = str(data.get("mid", ""))
        msg = ref.collection("messages").document(mid).get()
        if not msg.exists or msg.get("auteurUid") != uid:
            return jsonify({"erreur": "message inconnu"}), 404
        texte = (msg.get("texte") or "Nouveau message")[:120]
        envoyer_au_sujet("staff_" + staff_id, "%s · %s" % (nom_staff, msg.get("auteurNom") or "Membre"),
                         texte, {"type": "staff_message", "staffId": staff_id, "auteurUid": uid}, CANAL_STAFF)
        return jsonify({"ok": True})

    return jsonify({"erreur": "type inconnu"}), 400


# ---------------------------------------------------------------------------
# v1.4 : fichiers audio (karaoké partagé), diagnostic du stockage, notification quotidienne
# ---------------------------------------------------------------------------
@app.post("/api/staff/audio-url")
def staff_audio_url():
    """Lien temporaire pour envoyer un karaoké (M4A ou MP3) sur Cloudflare R2."""
    jeton = utilisateur_connecte()
    if not jeton:
        return jsonify({"erreur": "accès refusé"}), 403
    data = request.get_json(silent=True) or {}
    taille = int(data.get("taille", 0))
    type_ = str(data.get("type", ""))
    if type_ not in ("audio/mp4", "audio/mpeg") or taille <= 0 or taille > 60_000_000:
        return jsonify({"erreur": "fichier audio refusé (M4A ou MP3, 60 Mo maximum)"}), 400
    cle = "karaoke/%s.m4a" % uuid.uuid4().hex
    lien = _client_r2().generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["R2_BUCKET"], "Key": cle, "ContentType": type_},
        ExpiresIn=900,
    )
    return jsonify({"uploadUrl": lien, "publicUrl": os.environ["R2_PUBLIC_URL"].rstrip("/") + "/" + cle})


@app.get("/api/staff/sante")
def staff_sante():
    """Diagnostic : indique si le stockage R2 est configuré (sans jamais afficher les secrets)."""
    attendues = ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_URL")
    manquantes = [v for v in attendues if not os.environ.get(v)]
    return jsonify({"pret": not manquantes, "variables_manquantes": manquantes})


MESSAGES_QUOTIDIENS = [
    ("Bonjour !", "Découvre une chanson nouvelle aujourd'hui sur MUNKOYO MUSIQUE."),
    ("Ta musique t'attend", "Lance une recherche et ajoute un titre à ta playlist."),
    ("Essaie le karaoké", "Chante sur ta chanson préférée et partage-la dans ton staff."),
    ("Pense à tes staffs", "Tes amis ont peut-être partagé de nouvelles chansons."),
]


@app.post("/api/cron/quotidien")
def cron_quotidien():
    """Notification quotidienne. À appeler une fois par jour (cron externe), avec l'en-tête X-Cron-Secret."""
    attendu = os.environ.get("CRON_SECRET", "")
    recu = request.headers.get("X-Cron-Secret", "")
    if not attendu or not hmac.compare_digest(recu, attendu):
        return jsonify({"erreur": "accès refusé"}), 403
    import datetime as dt
    titre, texte = MESSAGES_QUOTIDIENS[dt.date.today().toordinal() % len(MESSAGES_QUOTIDIENS)]
    envoyer_a_tous(titre, texte, {"type": "quotidien"})
    return jsonify({"ok": True})
