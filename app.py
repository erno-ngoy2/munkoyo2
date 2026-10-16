"""
Serveur d'administration de MUNKOYO MUSIQUE (à déployer sur Render).

Il fait ce que l'application ne peut pas faire seule et en sécurité :
  - envoyer une notification à TOUS les utilisateurs (Firebase Cloud Messaging)
  - lancer / désactiver l'alerte de mise à jour

Seul l'administrateur peut l'appeler : chaque requête doit contenir le jeton de connexion Firebase
de ernoerno226@gmail.com (vérifié ici avec le SDK Firebase Admin).

Variables d'environnement :
  FIREBASE_CREDENTIALS  contenu JSON du compte de service Firebase (OBLIGATOIRE)
  ADMIN_EMAIL           e-mail de l'administrateur (par défaut : ernoerno226@gmail.com)
  SITE_URL              adresse du site de téléchargement (par défaut : https://munkoyomusique.onrender.com).
                        Quand tu passeras à munkoyomusique.online, change seulement cette variable :
                        les applications suivent sans mise à jour.
"""
import json
import os
import time

import firebase_admin
from firebase_admin import auth, credentials, firestore, messaging
from flask import Flask, jsonify, request

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ernoerno226@gmail.com").strip().lower()
SITE_URL = os.environ.get("SITE_URL", "https://munkoyomusique.onrender.com").strip().rstrip("/")
SUJET = "tous"
CANAL = "annonces"

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


def envoyer_a_tous(titre, texte, donnees=None):
    message = messaging.Message(
        topic=SUJET,
        notification=messaging.Notification(title=titre, body=texte),
        data={k: str(v) for k, v in (donnees or {}).items()},
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id=CANAL),
        ),
    )
    return messaging.send(message)


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