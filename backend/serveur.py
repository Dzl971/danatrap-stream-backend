from fastapi import FastAPI, HTTPException, Depends, Request, Query
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel
from jose import jwt, JWTError
from datetime import datetime, timedelta
from typing import Optional, Any
import bcrypt
import pickle
import os
import json
import base64
import io
import requests
import pathlib
import subprocess
import tempfile
import threading
import time
import shutil
import re
import hashlib
import unicodedata
from difflib import SequenceMatcher

SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file'
]
CLE_SECRETE_JWT = os.environ.get('CLE_SECRETE_JWT', 'change-moi-en-production')
ALGORITHME = "HS256"
DUREE_TOKEN_JOURS = 30
ADMIN_PSEUDO = "Dzl 971"
USERS_CACHE_TTL = int(os.environ.get("USERS_CACHE_TTL", "30"))
LIBRARY_CACHE_TTL = int(os.environ.get("LIBRARY_CACHE_TTL", "300"))
DEFAULT_MAX_DEVICES = max(1, int(os.environ.get("DEFAULT_MAX_DEVICES", "3")))

TMDB_API_KEY = os.environ.get('TMDB_API_KEY', '')
cache_tmdb = {}

# ==================== CONNEXION GOOGLE DRIVE ====================
def _trouver_token_pickle():
    """Cherche token.pickle dans le CWD et dans le dossier du script."""
    candidats = [
        'token.pickle',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'token.pickle'),
    ]
    for chemin in candidats:
        if os.path.exists(chemin):
            return chemin
    return None

def charger_credentials():
    token_env = os.environ.get('TOKEN_BASE64')
    if token_env:
        contenu = base64.b64decode(token_env)
        creds = pickle.loads(contenu)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        return creds

    chemin = _trouver_token_pickle()
    if not chemin:
        raise RuntimeError(
            "Aucun token Google valide. Execute reauth_google.py en local "
            "et mets a jour TOKEN_BASE64 sur Render."
        )
    with open(chemin, 'rb') as token:
        creds = pickle.load(token)
    if not creds:
        raise RuntimeError(
            f"Token Google invalide ou corrupt ({chemin}). "
            "Execute reauth_google.py en local puis mets a jour TOKEN_BASE64 sur Render."
        )
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        # Save refreshed token locally for next startup
        try:
            with open(chemin, 'wb') as token:
                pickle.dump(creds, token)
        except Exception:
            pass
    if not creds.valid:
        raise RuntimeError(
            f"Token Google invalide ou expire ({chemin}). "
            "Execute reauth_google.py en local puis mets a jour TOKEN_BASE64 sur Render."
        )
    return creds

CREDENTIALS = None
try:
    CREDENTIALS = charger_credentials()
    print(f"Google Drive connecte (token valide: {CREDENTIALS.valid})")
except Exception as e:
    print(f"ATTENTION: Initialisation Google Drive echouee : {e}")
    print("(Le token sera re-tente automatiquement a chaque requete)")

def _charger_si_manquant():
    """Re-tente de charger les credentials si pas encore fait."""
    global CREDENTIALS
    if CREDENTIALS is not None:
        if CREDENTIALS.expired and CREDENTIALS.refresh_token:
            try:
                CREDENTIALS.refresh(GoogleRequest())
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"Refresh Google Drive echoue: {e}")
        return
    try:
        CREDENTIALS = charger_credentials()
        print(f"Credentials re-charges au premier appel (valid: {CREDENTIALS.valid})")
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Service Google Drive non disponible: {e}. "
                   "Execute reauth_google.py en local puis mets a jour TOKEN_BASE64 sur Render."
        )

def get_access_token():
    if CREDENTIALS is None:
        _charger_si_manquant()
    if not CREDENTIALS.valid:
        try:
            CREDENTIALS.refresh(GoogleRequest())
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"Refresh token Google invalide ({e}). "
                       "Regenere TOKEN_BASE64 avec reauth_google.py puis redeploie."
            )
    return CREDENTIALS.token

# ==================== GESTION UTILISATEURS ====================
def trouver_dossier_racine(service):
    resultats = service.files().list(
        q="name = 'Danatrap Stream' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
        fields="files(id, name)"
    ).execute()
    fichiers = resultats.get('files', [])
    return fichiers[0]['id'] if fichiers else None

def trouver_fichier_utilisateurs(service, racine_id):
    resultats = service.files().list(
        q=f"name = 'utilisateurs.json' and '{racine_id}' in parents and trashed = false",
        fields="files(id, name)"
    ).execute()
    fichiers = resultats.get('files', [])
    return fichiers[0]['id'] if fichiers else None

_USERS_CACHE: dict[str, Any] = {"cached_at": 0.0, "data": None}
_USERS_CACHE_LOCK = threading.Lock()


def _normaliser_fiche_utilisateur(fiche: Any) -> dict:
    """Rend les anciens fichiers utilisateurs compatibles avec les nouvelles options."""
    if isinstance(fiche, str):
        fiche = {"mot_de_passe_hash": fiche}
    if not isinstance(fiche, dict):
        fiche = {}
    resultat = dict(fiche)
    resultat.setdefault("mot_de_passe_hash", "")
    resultat.setdefault("expires_at", None)
    try:
        resultat["max_devices"] = max(1, int(resultat.get("max_devices") or DEFAULT_MAX_DEVICES))
    except (TypeError, ValueError):
        resultat["max_devices"] = DEFAULT_MAX_DEVICES
    devices = resultat.get("devices")
    resultat["devices"] = devices if isinstance(devices, dict) else {}
    resultat.setdefault("created_at", None)
    return resultat


def charger_utilisateurs(force: bool = False):
    now = time.time()
    with _USERS_CACHE_LOCK:
        cached = _USERS_CACHE.get("data")
        if not force and isinstance(cached, dict) and now - float(_USERS_CACHE.get("cached_at") or 0) < USERS_CACHE_TTL:
            return {pseudo: _normaliser_fiche_utilisateur(fiche) for pseudo, fiche in cached.items()}

    if CREDENTIALS is None:
        _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        return {}
    fichier_id = trouver_fichier_utilisateurs(service, racine_id)
    if not fichier_id:
        return {}
    access_token = get_access_token()
    url = f"https://www.googleapis.com/drive/v3/files/{fichier_id}?alt=media"
    reponse = requests.get(url, headers={'Authorization': f'Bearer {access_token}'}, timeout=20)
    reponse.raise_for_status()
    data = reponse.json()
    if not isinstance(data, dict):
        data = {}
    normalise = {pseudo: _normaliser_fiche_utilisateur(fiche) for pseudo, fiche in data.items()}
    with _USERS_CACHE_LOCK:
        _USERS_CACHE["cached_at"] = now
        _USERS_CACHE["data"] = normalise
    return {pseudo: dict(fiche) for pseudo, fiche in normalise.items()}


def sauvegarder_utilisateurs(utilisateurs):
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise RuntimeError("Dossier racine 'Danatrap Stream' introuvable sur Google Drive")
    fichier_id = trouver_fichier_utilisateurs(service, racine_id)
    normalise = {pseudo: _normaliser_fiche_utilisateur(fiche) for pseudo, fiche in utilisateurs.items()}
    contenu_json = json.dumps(normalise, indent=2, ensure_ascii=False)
    media = MediaIoBaseUpload(io.BytesIO(contenu_json.encode('utf-8')), mimetype='application/json', resumable=False)
    if fichier_id:
        service.files().update(fileId=fichier_id, media_body=media).execute()
    else:
        metadata = {'name': 'utilisateurs.json', 'parents': [racine_id]}
        service.files().create(body=metadata, media_body=media, fields='id').execute()
    with _USERS_CACHE_LOCK:
        _USERS_CACHE["cached_at"] = time.time()
        _USERS_CACHE["data"] = normalise


def verifier_mot_de_passe(mot_de_passe_clair, mot_de_passe_hash):
    try:
        return bcrypt.checkpw(mot_de_passe_clair.encode('utf-8'), str(mot_de_passe_hash).encode('utf-8'))
    except Exception:
        return False


def _expiration_datetime(expires_at: Optional[str]) -> Optional[datetime]:
    if not expires_at:
        return None
    texte = str(expires_at).strip()
    if not texte:
        return None
    try:
        if len(texte) == 10:
            return datetime.strptime(texte, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        parsed = datetime.fromisoformat(texte.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _compte_expire(fiche: dict) -> bool:
    expiration = _expiration_datetime(fiche.get("expires_at"))
    return bool(expiration and datetime.utcnow() > expiration)


def _nettoyer_device_id(device_id: Optional[str]) -> str:
    brut = re.sub(r"[^A-Za-z0-9_.:-]", "", str(device_id or ""))[:128]
    return brut


def _device_legacy(pseudo: str, user_agent: str) -> str:
    digest = hashlib.sha256(f"{pseudo}|{user_agent}".encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"legacy-{digest}"


def creer_token(pseudo: str, device_id: Optional[str] = None, expires_at: Optional[str] = None):
    expiration = datetime.utcnow() + timedelta(days=DUREE_TOKEN_JOURS)
    expiration_compte = _expiration_datetime(expires_at)
    if expiration_compte and expiration_compte < expiration:
        expiration = expiration_compte
    donnees = {
        "sub": pseudo,
        "exp": expiration,
        "est_admin": pseudo == ADMIN_PSEUDO,
        "device_id": device_id or "",
    }
    return jwt.encode(donnees, CLE_SECRETE_JWT, algorithm=ALGORITHME)


security = HTTPBearer()


def verifier_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, CLE_SECRETE_JWT, algorithms=[ALGORITHME])
        pseudo = payload.get("sub")
        if pseudo is None:
            raise HTTPException(status_code=401, detail="Token invalide")
        utilisateurs = charger_utilisateurs()
        fiche = utilisateurs.get(pseudo)
        if not fiche:
            raise HTTPException(status_code=401, detail="Compte introuvable")
        if _compte_expire(fiche):
            raise HTTPException(status_code=403, detail="Ce compte a expiré")
        device_id = _nettoyer_device_id(payload.get("device_id"))
        # Les anciens jetons sans device_id restent valides jusqu'à leur expiration.
        if device_id and device_id not in (fiche.get("devices") or {}):
            raise HTTPException(status_code=401, detail="Cet appareil a été déconnecté")
        return {
            "pseudo": pseudo,
            "est_admin": payload.get("est_admin", False),
            "device_id": device_id,
            "expires_at": fiche.get("expires_at"),
            "max_devices": fiche.get("max_devices", DEFAULT_MAX_DEVICES),
        }
    except HTTPException:
        raise
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expire")


def verifier_admin(utilisateur: dict = Depends(verifier_token)):
    if not utilisateur.get("est_admin") and utilisateur.get("pseudo") != ADMIN_PSEUDO:
        raise HTTPException(status_code=403, detail="Acces reserve a l'administrateur")
    return utilisateur["pseudo"]


def _fiche_publique(pseudo: str, fiche: dict) -> dict:
    devices = fiche.get("devices") or {}
    liste_devices = []
    for device_id, device in devices.items():
        device = device if isinstance(device, dict) else {}
        liste_devices.append({
            "id": device_id,
            "name": device.get("name") or "Appareil inconnu",
            "first_seen": device.get("first_seen"),
            "last_seen": device.get("last_seen"),
        })
    liste_devices.sort(key=lambda d: d.get("last_seen") or "", reverse=True)
    return {
        "pseudo": pseudo,
        "expires_at": fiche.get("expires_at"),
        "expired": _compte_expire(fiche),
        "max_devices": int(fiche.get("max_devices") or DEFAULT_MAX_DEVICES),
        "device_count": len(devices),
        "devices": liste_devices,
        "created_at": fiche.get("created_at"),
    }

# ==================== APP FASTAPI ====================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== ROUTES API ====================
class LoginRequest(BaseModel):
    pseudo: str
    mot_de_passe: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None


@app.post("/login")
def login(data: LoginRequest, request: Request):
    import traceback
    try:
        utilisateurs = charger_utilisateurs(force=True)
        utilisateur = utilisateurs.get(data.pseudo)
        if not utilisateur or not verifier_mot_de_passe(data.mot_de_passe, utilisateur.get("mot_de_passe_hash", "")):
            raise HTTPException(status_code=401, detail="Pseudo ou mot de passe incorrect")
        if _compte_expire(utilisateur):
            raise HTTPException(status_code=403, detail="Ce compte a expiré. Contacte l’administrateur.")

        device_id = _nettoyer_device_id(data.device_id)
        if not device_id:
            device_id = _device_legacy(data.pseudo, request.headers.get("user-agent", "navigateur"))
        device_name = str(data.device_name or request.headers.get("user-agent") or "Navigateur").strip()[:120]
        devices = utilisateur.setdefault("devices", {})
        max_devices = max(1, int(utilisateur.get("max_devices") or DEFAULT_MAX_DEVICES))
        if device_id not in devices and len(devices) >= max_devices and data.pseudo != ADMIN_PSEUDO:
            raise HTTPException(
                status_code=403,
                detail=f"Limite de {max_devices} appareil(s) atteinte. Demande à l’administrateur de déconnecter un ancien appareil.",
            )
        maintenant = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        ancien = devices.get(device_id) if isinstance(devices.get(device_id), dict) else {}
        devices[device_id] = {
            "name": device_name or ancien.get("name") or "Appareil",
            "first_seen": ancien.get("first_seen") or maintenant,
            "last_seen": maintenant,
        }
        utilisateurs[data.pseudo] = utilisateur
        sauvegarder_utilisateurs(utilisateurs)

        token = creer_token(data.pseudo, device_id, utilisateur.get("expires_at"))
        est_admin = data.pseudo == ADMIN_PSEUDO
        return {
            "access_token": token,
            "pseudo": data.pseudo,
            "est_admin": est_admin,
            "expires_at": utilisateur.get("expires_at"),
            "max_devices": max_devices,
            "device_count": len(devices),
        }
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        with open("server.log", "a", encoding="utf-8") as f:
            f.write("\n\n=== " + datetime.now().isoformat() + " ===\n" + tb)
        raise HTTPException(status_code=500, detail=f"Erreur: {type(e).__name__}: {e}")


@app.get("/me")
def me(utilisateur: dict = Depends(verifier_token)):
    return utilisateur


class NouvelUtilisateur(BaseModel):
    pseudo: str
    mot_de_passe: str
    expires_at: Optional[str] = None
    max_devices: Optional[int] = DEFAULT_MAX_DEVICES


class MiseAJourUtilisateur(BaseModel):
    expires_at: Optional[str] = None
    max_devices: Optional[int] = None


@app.post("/admin/ajouter-utilisateur")
def ajouter_utilisateur(data: NouvelUtilisateur, admin: str = Depends(verifier_admin)):
    pseudo = data.pseudo.strip()
    if not pseudo or not data.mot_de_passe:
        raise HTTPException(status_code=400, detail="Pseudo et mot de passe requis")
    utilisateurs = charger_utilisateurs(force=True)
    mot_de_passe_hash = bcrypt.hashpw(data.mot_de_passe.encode('utf-8'), bcrypt.gensalt())
    utilisateurs[pseudo] = {
        "mot_de_passe_hash": mot_de_passe_hash.decode('utf-8'),
        "expires_at": data.expires_at or None,
        "max_devices": max(1, min(20, int(data.max_devices or DEFAULT_MAX_DEVICES))),
        "devices": {},
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    sauvegarder_utilisateurs(utilisateurs)
    return {"message": f"Utilisateur '{pseudo}' ajoute avec succes", "utilisateur": _fiche_publique(pseudo, utilisateurs[pseudo])}


@app.get("/admin/liste-utilisateurs")
def liste_utilisateurs(admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    return {"utilisateurs": [_fiche_publique(pseudo, fiche) for pseudo, fiche in sorted(utilisateurs.items(), key=lambda x: x[0].lower())]}


@app.patch("/admin/utilisateur/{pseudo}")
def mettre_a_jour_utilisateur(pseudo: str, data: MiseAJourUtilisateur, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    if pseudo not in utilisateurs:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    fiche = utilisateurs[pseudo]
    if data.max_devices is not None:
        fiche["max_devices"] = max(1, min(20, int(data.max_devices)))
    # Une chaîne vide enlève la date d'expiration.
    if data.expires_at is not None:
        fiche["expires_at"] = str(data.expires_at).strip() or None
    utilisateurs[pseudo] = fiche
    sauvegarder_utilisateurs(utilisateurs)
    return {"message": "Compte mis à jour", "utilisateur": _fiche_publique(pseudo, fiche)}


@app.post("/admin/utilisateur/{pseudo}/deconnecter-appareils")
def deconnecter_appareils(pseudo: str, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    if pseudo not in utilisateurs:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if pseudo == ADMIN_PSEUDO:
        raise HTTPException(status_code=400, detail="Le compte administrateur ne peut pas être déconnecté depuis cette action")
    utilisateurs[pseudo]["devices"] = {}
    sauvegarder_utilisateurs(utilisateurs)
    return {"message": f"Tous les appareils de '{pseudo}' ont été déconnectés"}


@app.delete("/admin/supprimer-utilisateur/{pseudo}")
def supprimer_utilisateur(pseudo: str, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    if pseudo not in utilisateurs:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if pseudo == ADMIN_PSEUDO:
        raise HTTPException(status_code=400, detail="Impossible de supprimer le compte administrateur")
    del utilisateurs[pseudo]
    sauvegarder_utilisateurs(utilisateurs)
    return {"message": f"Utilisateur '{pseudo}' supprime"}

# ==================== SCANNER DRIVE ====================
def get_id_dossier(service, nom, parent_id=None):
    query = f"name = '{nom}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    resultats = service.files().list(q=query, fields="files(id, name)").execute()
    fichiers = resultats.get('files', [])
    return fichiers[0]['id'] if fichiers else None

def normaliser_nom(nom):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', nom) if unicodedata.category(c) != 'Mn').lower().strip()

def trouver_dossier_flexible(service, noms_possibles, parent_id=None):
    query = "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    resultats = service.files().list(q=query, fields="files(id, name)").execute()
    fichiers = resultats.get('files', [])
    cibles = [normaliser_nom(n) for n in noms_possibles]
    for f in fichiers:
        if normaliser_nom(f['name']) in cibles:
            return f['id']
    return None

def lister_contenu(service, parent_id):
    query = f"'{parent_id}' in parents and trashed = false"
    resultats = service.files().list(
        q=query,
        fields="files(id,name,mimeType,modifiedTime,size,videoMediaMetadata(width,height,durationMillis))",
        pageSize=1000,
    ).execute()
    return resultats.get('files', [])


def _qualite_video(fichier: Optional[dict]) -> Optional[str]:
    if not fichier:
        return None
    meta = fichier.get("videoMediaMetadata") or {}
    try:
        hauteur = int(meta.get("height") or 0)
    except (TypeError, ValueError):
        hauteur = 0
    if hauteur >= 2160:
        return "4K"
    if hauteur >= 1440:
        return "1440p"
    if hauteur >= 1080:
        return "1080p"
    if hauteur >= 720:
        return "720p"
    if hauteur >= 480:
        return "480p"
    return None


def _duree_drive(fichier: Optional[dict]) -> Optional[str]:
    if not fichier:
        return None
    try:
        total_sec = int((fichier.get("videoMediaMetadata") or {}).get("durationMillis") or 0) // 1000
    except (TypeError, ValueError):
        total_sec = 0
    if total_sec <= 0:
        return None
    heures, reste = divmod(total_sec, 3600)
    minutes = reste // 60
    return f"{heures}h{minutes:02d}" if heures else f"{minutes} min"


def _choisir_bande_annonce(videos: dict) -> Optional[str]:
    resultats = videos.get("results", []) if isinstance(videos, dict) else []
    youtube = [v for v in resultats if v.get("site") == "YouTube" and v.get("key")]
    if not youtube:
        return None
    def score(video):
        type_video = str(video.get("type") or "").lower()
        nom = str(video.get("name") or "").lower()
        return (
            4 if type_video == "trailer" else 2 if type_video == "teaser" else 0,
            2 if video.get("official") else 0,
            1 if "fr" in str(video.get("iso_639_1") or "").lower() else 0,
            1 if "bande-annonce" in nom or "trailer" in nom else 0,
        )
    choix = sorted(youtube, key=score, reverse=True)[0]
    return f"https://www.youtube.com/watch?v={choix['key']}"


def _choisir_certification(details: dict, type_contenu: str) -> Optional[str]:
    if type_contenu == "film":
        resultats = (details.get("release_dates") or {}).get("results", [])
        pays = next((r for r in resultats if r.get("iso_3166_1") == "FR"), None) or next((r for r in resultats if r.get("iso_3166_1") == "US"), None)
        certifications = [str(x.get("certification") or "").strip() for x in (pays or {}).get("release_dates", [])]
        return next((c for c in certifications if c), None)
    resultats = (details.get("content_ratings") or {}).get("results", [])
    pays = next((r for r in resultats if r.get("iso_3166_1") == "FR"), None) or next((r for r in resultats if r.get("iso_3166_1") == "US"), None)
    valeur = str((pays or {}).get("rating") or "").strip()
    return valeur or None

def _retirer_accents(valeur: str) -> str:
    texte = unicodedata.normalize("NFKD", str(valeur or ""))
    return "".join(c for c in texte if not unicodedata.combining(c))


def _normaliser_titre_comparaison(titre: str) -> str:
    propre = _retirer_accents(titre).lower()
    propre = re.sub(r"[^a-z0-9]+", " ", propre)
    propre = re.sub(r"\b(?:the|a|an|le|la|les|un|une|des)\b", " ", propre)
    return re.sub(r"\s+", " ", propre).strip()


def _nettoyer_titre_tmdb(titre: str) -> str:
    """Retire les marqueurs techniques sans détruire le vrai titre de l'œuvre."""
    propre = str(titre or "")
    propre = re.sub(r"\.(?:mkv|mp4|avi|mov|m4v|webm)$", "", propre, flags=re.I)
    propre = re.sub(r"\[[^\]]*\]", " ", propre)
    propre = re.sub(
        r"\([^)]*(?:4k|2160p|1080p|720p|480p|vf|vostfr|multi|version\s+(?:longue|courte|complete)|integrale|remaster|fan\s*edit)[^)]*\)",
        " ", propre, flags=re.I,
    )
    propre = re.sub(
        r"\b(?:4k|2160p|1080p|720p|480p|uhd|bluray|web[- .]?dl|webrip|x264|x265|hevc|multi|vostfr|vf2?|truefrench|french|subfrench)\b",
        " ", propre, flags=re.I,
    )
    propre = re.sub(r"[._]+", " ", propre)
    propre = re.sub(r"\s+", " ", propre).strip(" -_")
    return propre or str(titre or "").strip()


def _variantes_titre_tmdb(titre: str) -> list[str]:
    """Produit plusieurs recherches sûres pour les montages Kai, versions longues, tags de fans, etc."""
    base = _nettoyer_titre_tmdb(titre)
    candidats = [base]

    # Les mentions ajoutées par l'utilisateur sont souvent placées entre parenthèses.
    sans_parentheses = re.sub(r"\([^)]*\)", " ", base)
    candidats.append(re.sub(r"\s+", " ", sans_parentheses).strip(" -_"))

    # Les montages personnels utilisent souvent ces suffixes alors que TMDB garde le titre officiel.
    suffixes = re.compile(
        r"(?:\s*[-–—:]\s*)?(?:kai|version\s+(?:longue|courte|complete|integrale)|integrale|complet(?:e)?|remaster(?:ed)?|fan\s*edit|cut)\s*$",
        flags=re.I,
    )
    courant = base
    for _ in range(3):
        suivant = suffixes.sub("", courant).strip(" -_")
        if suivant == courant:
            break
        candidats.append(suivant)
        courant = suivant

    # Retire aussi les numéros de saison ajoutés au nom du dossier principal.
    candidats.append(re.sub(r"\b(?:saison|season)\s*\d+\b.*$", "", base, flags=re.I).strip(" -_"))

    resultat = []
    vus = set()
    for candidat in candidats:
        candidat = re.sub(r"\s+", " ", candidat or "").strip(" -_")
        cle = candidat.casefold()
        if candidat and cle not in vus:
            vus.add(cle)
            resultat.append(candidat)
    return resultat or [str(titre or "").strip()]


def _score_resultat_tmdb(resultat: dict, variantes: list[str], index_requete: int) -> float:
    noms = [
        resultat.get("title"), resultat.get("original_title"),
        resultat.get("name"), resultat.get("original_name"),
    ]
    noms = [_normaliser_titre_comparaison(n) for n in noms if n]
    meilleur = 0.0
    for variante in variantes:
        q = _normaliser_titre_comparaison(variante)
        if not q:
            continue
        for nom in noms:
            if q == nom:
                score = 130.0
            elif q in nom or nom in q:
                score = 102.0 - abs(len(q) - len(nom)) * 0.3
            else:
                score = SequenceMatcher(None, q, nom).ratio() * 100.0
            meilleur = max(meilleur, score)
    # La première variante est la plus proche du nom original du dossier.
    meilleur -= min(index_requete, 4) * 1.5
    popularite = float(resultat.get("popularity") or 0)
    meilleur += min(popularite / 100.0, 4.0)
    return meilleur


def rechercher_tmdb(titre, type_contenu="film"):
    variantes = _variantes_titre_tmdb(titre)
    cle_cache = f"{type_contenu}:{'|'.join(v.casefold() for v in variantes)}"
    if cle_cache in cache_tmdb:
        return cache_tmdb[cle_cache]
    if not TMDB_API_KEY:
        return {}

    endpoint = "movie" if type_contenu == "film" else "tv"
    url_recherche = f"https://api.themoviedb.org/3/search/{endpoint}"
    try:
        candidats: dict[int, tuple[dict, float, str]] = {}
        for index, titre_recherche in enumerate(variantes[:5]):
            params = {
                "api_key": TMDB_API_KEY,
                "query": titre_recherche,
                "language": "fr-FR",
                "include_adult": "false",
            }
            reponse = requests.get(url_recherche, params=params, timeout=8)
            reponse.raise_for_status()
            resultats = reponse.json().get("results", [])

            # Certains titres japonais sont mieux indexés avec la recherche anglaise.
            if not resultats:
                params["language"] = "en-US"
                reponse = requests.get(url_recherche, params=params, timeout=8)
                reponse.raise_for_status()
                resultats = reponse.json().get("results", [])

            for resultat_brut in resultats[:8]:
                identifiant = resultat_brut.get("id")
                if not identifiant:
                    continue
                score = _score_resultat_tmdb(resultat_brut, variantes, index)
                precedent = candidats.get(int(identifiant))
                if precedent is None or score > precedent[1]:
                    candidats[int(identifiant)] = (resultat_brut, score, titre_recherche)

        if not candidats:
            cache_tmdb[cle_cache] = {}
            return {}

        premier, score_match, requete_utilisee = max(candidats.values(), key=lambda x: x[1])
        if score_match < 48:
            print(f"TMDB: correspondance trop faible pour '{titre}' ({score_match:.1f})")
            cache_tmdb[cle_cache] = {}
            return {}

        tmdb_id = premier["id"]
        url_details = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
        append = "credits,videos,release_dates" if type_contenu == "film" else "credits,videos,content_ratings"
        reponse_details = requests.get(
            url_details,
            params={"api_key": TMDB_API_KEY, "language": "fr-FR", "append_to_response": append},
            timeout=10,
        )
        reponse_details.raise_for_status()
        details = reponse_details.json()

        synopsis = str(details.get("overview") or "").strip()
        synopsis_langue = "fr"
        if not synopsis:
            # Si TMDB n'a pas encore de traduction française, on récupère automatiquement l'anglais.
            reponse_en = requests.get(
                url_details,
                params={"api_key": TMDB_API_KEY, "language": "en-US"},
                timeout=8,
            )
            reponse_en.raise_for_status()
            details_en = reponse_en.json()
            synopsis = str(details_en.get("overview") or premier.get("overview") or "").strip()
            synopsis_langue = "en" if synopsis else None

        genres = [g["name"] for g in details.get("genres", [])]
        if type_contenu == "film":
            duree_minutes = details.get("runtime", 0)
            date_sortie = details.get("release_date", "")
        else:
            durees = details.get("episode_run_time", [])
            duree_minutes = durees[0] if durees else 0
            date_sortie = details.get("first_air_date", "")
        annee = date_sortie[:4] if date_sortie else None
        heures = int(duree_minutes or 0) // 60
        minutes = int(duree_minutes or 0) % 60
        duree_texte = (f"{heures}h{minutes:02d}" if heures else f"{minutes} min") if duree_minutes else None
        cast = [p.get("name") for p in (details.get("credits") or {}).get("cast", [])[:8] if p.get("name")]
        realisateurs = [
            p.get("name") for p in (details.get("credits") or {}).get("crew", [])
            if p.get("job") in {"Director", "Creator"} and p.get("name")
        ][:4]
        if type_contenu != "film":
            for createur in details.get("created_by") or []:
                nom = createur.get("name")
                if nom and nom not in realisateurs:
                    realisateurs.append(nom)
            realisateurs = realisateurs[:4]

        resultat = {
            "tmdb_id": tmdb_id,
            "titre_tmdb": details.get("title") or details.get("name") or premier.get("title") or premier.get("name"),
            "requete_tmdb": requete_utilisee,
            "score_tmdb": round(score_match, 1),
            "genres": genres,
            "duree": duree_texte,
            "annee": annee,
            "note": round(float(details.get("vote_average", 0) or 0), 1),
            "synopsis": synopsis,
            "synopsis_langue": synopsis_langue,
            "backdrop_url": f"https://image.tmdb.org/t/p/w1280{details.get('backdrop_path')}" if details.get("backdrop_path") else None,
            "poster_tmdb_url": f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None,
            "acteurs": cast,
            "realisateurs": realisateurs,
            "certification": _choisir_certification(details, type_contenu),
            "bande_annonce_url": _choisir_bande_annonce(details.get("videos") or {}),
            "langue_originale": details.get("original_language"),
            "popularite": round(float(details.get("popularity", 0) or 0), 2),
        }
        cache_tmdb[cle_cache] = resultat
        return resultat
    except Exception as e:
        print(f"Erreur TMDB pour '{titre}': {e}")
        return {}


def scanner_films(service, films_id):
    films = []
    for dossier in lister_contenu(service, films_id):
        if dossier['mimeType'] == 'application/vnd.google-apps.folder':
            contenu = lister_contenu(service, dossier['id'])
            video = next((f for f in contenu if f['mimeType'].startswith('video/')), None)
            poster = next((f for f in contenu if f['name'].lower() in {'icon.jpg', 'icon.jpeg', 'icon.png', 'poster.jpg', 'poster.png'}), None)
            infos_tmdb = rechercher_tmdb(dossier['name'], "film")
            films.append({
                "titre": dossier['name'],
                "video_id": video['id'] if video else None,
                "video_nom": video.get('name') if video else None,
                "poster_id": poster['id'] if poster else None,
                "date_ajout": dossier.get("modifiedTime") or (video or {}).get("modifiedTime"),
                "qualite": _qualite_video(video),
                "duree_drive": _duree_drive(video),
                **infos_tmdb
            })
    films.sort(key=lambda f: f.get("date_ajout") or "", reverse=True)
    return films


def scanner_series(service, series_id):
    series = []
    for dossier_serie in lister_contenu(service, series_id):
        if dossier_serie['mimeType'] == 'application/vnd.google-apps.folder':
            contenu_serie = lister_contenu(service, dossier_serie['id'])
            poster = next((f for f in contenu_serie if f['name'].lower() in {'icon.jpg', 'icon.jpeg', 'icon.png', 'poster.jpg', 'poster.png'}), None)
            saisons = []
            for dossier_saison in contenu_serie:
                if dossier_saison['mimeType'] == 'application/vnd.google-apps.folder':
                    episodes_bruts = lister_contenu(service, dossier_saison['id'])
                    episodes = [
                        {
                            "nom": ep['name'],
                            "video_id": ep['id'],
                            "qualite": _qualite_video(ep),
                            "duree": _duree_drive(ep),
                            "date_ajout": ep.get("modifiedTime"),
                        }
                        for ep in episodes_bruts if ep['mimeType'].startswith('video/')
                    ]
                    saisons.append({"nom_saison": dossier_saison['name'], "episodes": episodes, "date_ajout": dossier_saison.get("modifiedTime")})
            infos_tmdb = rechercher_tmdb(dossier_serie['name'], "tv")
            series.append({
                "titre": dossier_serie['name'],
                "poster_id": poster['id'] if poster else None,
                "date_ajout": dossier_serie.get("modifiedTime"),
                "saisons": saisons,
                **infos_tmdb
            })
    series.sort(key=lambda f: f.get("date_ajout") or "", reverse=True)
    return series


_LIBRARY_CACHE: dict[str, Any] = {"cached_at": 0.0, "data": None}
_LIBRARY_CACHE_LOCK = threading.Lock()


def _scanner_bibliotheque(force: bool = False) -> dict:
    now = time.time()
    with _LIBRARY_CACHE_LOCK:
        cached = _LIBRARY_CACHE.get("data")
        if not force and isinstance(cached, dict) and now - float(_LIBRARY_CACHE.get("cached_at") or 0) < LIBRARY_CACHE_TTL:
            return cached
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = get_id_dossier(service, "Danatrap Stream")
    if not racine_id:
        raise HTTPException(status_code=404, detail="Dossier 'Danatrap Stream' introuvable sur Google Drive")
    films_id = trouver_dossier_flexible(service, ["Films", "FILMS", "films"], racine_id)
    series_id = trouver_dossier_flexible(service, ["Séries", "Series", "SÉRIES", "SERIES", "séries", "series"], racine_id)
    anime_id = trouver_dossier_flexible(service, ["Animes", "Anime", "ANIMES", "ANIME", "animes", "anime"], racine_id)
    data = {
        "films": scanner_films(service, films_id) if films_id else [],
        "series": scanner_series(service, series_id) if series_id else [],
        "anime": scanner_series(service, anime_id) if anime_id else [],
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE["cached_at"] = now
        _LIBRARY_CACHE["data"] = data
    return data


# ==================== ROUTES PROTEGEES ====================
@app.get("/bibliotheque")
def get_bibliotheque(utilisateur: dict = Depends(verifier_token)):
    return _scanner_bibliotheque()


@app.post("/admin/rafraichir-bibliotheque")
def rafraichir_bibliotheque(admin: str = Depends(verifier_admin)):
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE["cached_at"] = 0.0
        _LIBRARY_CACHE["data"] = None
    cache_tmdb.clear()
    data = _scanner_bibliotheque(force=True)
    return {"message": "Bibliothèque actualisée", "generated_at": data.get("generated_at")}


@app.get("/admin/dashboard")
def dashboard_admin(admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    bibliotheque = _scanner_bibliotheque()
    nb_episodes_series = sum(len(saison.get("episodes", [])) for serie in bibliotheque.get("series", []) for saison in serie.get("saisons", []))
    nb_episodes_anime = sum(len(saison.get("episodes", [])) for serie in bibliotheque.get("anime", []) for saison in serie.get("saisons", []))
    return {
        "utilisateurs": len(utilisateurs),
        "comptes_expires": sum(1 for fiche in utilisateurs.values() if _compte_expire(fiche)),
        "appareils": sum(len(fiche.get("devices") or {}) for fiche in utilisateurs.values()),
        "films": len(bibliotheque.get("films", [])),
        "series": len(bibliotheque.get("series", [])),
        "anime": len(bibliotheque.get("anime", [])),
        "episodes": nb_episodes_series + nb_episodes_anime,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "drive": CREDENTIALS is not None,
        "tmdb": bool(TMDB_API_KEY),
        "sans_description": sum(
            1 for categorie in ("films", "series", "anime")
            for contenu in bibliotheque.get(categorie, [])
            if not str(contenu.get("synopsis") or "").strip()
        ),
        "cache_pistes": len(_TRACKS_CACHE) if "_TRACKS_CACHE" in globals() else 0,
        "bibliotheque_generee": bibliotheque.get("generated_at"),
    }


@app.get("/stream/{video_id}")
def stream_video(
    video_id: str,
    request: Request,
    token: Optional[str] = Query(None)
):
    token_final = token
    if not token_final:
        auth_header = request.headers.get('authorization', '')
        if auth_header.startswith('Bearer '):
            token_final = auth_header.replace('Bearer ', '')
    if not token_final:
        raise HTTPException(status_code=401, detail="Token manquant")
    try:
        jwt.decode(token_final, CLE_SECRETE_JWT, algorithms=[ALGORITHME])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expire")

    access_token = get_access_token()
    range_header = request.headers.get('range', 'bytes=0-')
    drive_url = f"https://www.googleapis.com/drive/v3/files/{video_id}?alt=media"
    headers_requete = {
        'Authorization': f'Bearer {access_token}',
        'Range': range_header
    }
    reponse_drive = requests.get(drive_url, headers=headers_requete, stream=True)

    def iterateur():
        for morceau in reponse_drive.iter_content(chunk_size=1024 * 1024):
            yield morceau

    headers_reponse = {'Accept-Ranges': 'bytes'}
    if 'Content-Range' in reponse_drive.headers:
        headers_reponse['Content-Range'] = reponse_drive.headers['Content-Range']
    if 'Content-Length' in reponse_drive.headers:
        headers_reponse['Content-Length'] = reponse_drive.headers['Content-Length']

    return StreamingResponse(
        iterateur(),
        status_code=reponse_drive.status_code,
        headers=headers_reponse,
        media_type='video/mp4'
    )

@app.get("/api/health")
def health():
    return {"status": "ok", "drive_configured": CREDENTIALS is not None, "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}



# ==================== PISTES AUDIO / SOUS-TITRES ====================
# Les navigateurs ne permettent pas de changer de piste audio de façon fiable
# dans un MP4/MKV progressif. Le serveur expose donc chaque piste audio comme un
# flux audio séparé, synchronisé par le lecteur web.

def _get_drive_media_url(video_id: str) -> str:
    return f"https://www.googleapis.com/drive/v3/files/{video_id}?alt=media"


def _get_drive_auth_header() -> str:
    # FFmpeg attend une ligne d'en-tête HTTP terminée par CRLF.
    access_token = get_access_token()
    return f"Authorization: Bearer {access_token}\r\n"


_TRACKS_CACHE: dict[str, dict] = {}
_TRACKS_CACHE_TTL = int(os.environ.get("TRACKS_CACHE_TTL", "3600"))
_TRACKS_CACHE_LOCK = threading.Lock()


def _normaliser_langue(tags: dict) -> str:
    for key in ("language", "LANGUAGE", "lang", "LANG"):
        if tags.get(key):
            return str(tags[key])
    return ""


_HANDLERS_GENERIQUES = {
    "soundhandler", "subtitleshandler", "subtitlehandler", "videohandler", "handler"
}


def _format_track_label(tags: dict, stream_type: str, index: int) -> str:
    lang = _normaliser_langue(tags).strip()
    raw_title = str(tags.get("title") or tags.get("name") or tags.get("handler_name") or "").strip()
    title = raw_title if raw_title.lower() not in _HANDLERS_GENERIQUES else ""
    prefix = "Audio" if stream_type == "audio" else "Sous-titre"
    if lang and title:
        return f"{lang.upper()} - {title}"
    if lang:
        return f"{prefix} {index + 1} ({lang.upper()})"
    if title:
        return f"{prefix} {index + 1} - {title}"
    return f"{prefix} {index + 1}"


def _probe_media(video_id: str, force: bool = False) -> dict:
    """Analyse une seule fois les flux audio/sous-titres du média distant."""
    now = time.time()
    with _TRACKS_CACHE_LOCK:
        cached = _TRACKS_CACHE.get(video_id)
        if not force and cached and now - cached["cached_at"] < _TRACKS_CACHE_TTL:
            return cached["data"]

    cmd = [
        "ffprobe",
        "-headers", _get_drive_auth_header(),
        "-v", "error",
        "-show_entries",
        "format=duration:stream=index,codec_name,codec_type,width,height:stream_disposition=default,attached_pic:stream_tags=language,title,handler_name",
        "-of", "json",
        _get_drive_media_url(video_id),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
    except FileNotFoundError as exc:
        raise RuntimeError("ffprobe n'est pas installé sur le serveur Render") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("L'analyse des pistes a dépassé 75 secondes") from exc

    if result.returncode != 0:
        detail = (result.stderr or "Erreur ffprobe inconnue").strip()[-1200:]
        raise RuntimeError(f"ffprobe a échoué: {detail}")

    try:
        probe_data = json.loads(result.stdout or "{}")
        streams = probe_data.get("streams", [])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Réponse ffprobe invalide") from exc

    video_streams = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not bool((s.get("disposition") or {}).get("attached_pic", 0))
    ]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    audio = []
    for i, stream in enumerate(audio_streams):
        tags = stream.get("tags") or {}
        audio.append({
            "index": i,
            "stream_index": stream.get("index"),
            "language": _normaliser_langue(tags),
            "title": _format_track_label(tags, "audio", i),
            "codec": stream.get("codec_name", ""),
            "default": bool((stream.get("disposition") or {}).get("default", 0)),
        })

    subtitles = []
    for i, stream in enumerate(subtitle_streams):
        tags = stream.get("tags") or {}
        subtitles.append({
            "index": i,
            "stream_index": stream.get("index"),
            "language": _normaliser_langue(tags),
            "title": _format_track_label(tags, "subtitle", i),
            "codec": stream.get("codec_name", ""),
            "default": bool((stream.get("disposition") or {}).get("default", 0)),
        })

    duration_raw = (probe_data.get("format") or {}).get("duration")
    try:
        duration = max(0.0, float(duration_raw))
    except (TypeError, ValueError):
        duration = 0.0

    primary_video = video_streams[0] if video_streams else {}
    try:
        largeur = int(primary_video.get("width") or 0)
        hauteur = int(primary_video.get("height") or 0)
    except (TypeError, ValueError):
        largeur, hauteur = 0, 0
    qualite = "4K" if hauteur >= 2160 else "1440p" if hauteur >= 1440 else "1080p" if hauteur >= 1080 else "720p" if hauteur >= 720 else "480p" if hauteur >= 480 else None
    data = {
        "audio": audio,
        "subtitles": subtitles,
        "duration": duration,
        "video_stream_index": primary_video.get("index"),
        "video_codec": primary_video.get("codec_name", ""),
        "width": largeur,
        "height": hauteur,
        "quality": qualite,
    }
    with _TRACKS_CACHE_LOCK:
        _TRACKS_CACHE[video_id] = {"cached_at": now, "data": data}
    return data


@app.get("/tracks/{video_id}")
def get_tracks(video_id: str, utilisateur: dict = Depends(verifier_token)):
    try:
        # Important : cette route ne lance plus l'extraction complète des pistes.
        # Elle répond dès que ffprobe a identifié les langues disponibles.
        return _probe_media(video_id)
    except RuntimeError as exc:
        print(f"[DanaTrap] analyse des pistes impossible pour {video_id}: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))


def _verifier_token_query(request: Request, token: Optional[str] = Query(None)):
    """Vérifie le JWT depuis la query string ou le header Authorization."""
    token_final = token
    if not token_final:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token_final = auth_header[7:]
    if not token_final:
        raise HTTPException(status_code=401, detail="Token manquant")
    try:
        payload = jwt.decode(token_final, CLE_SECRETE_JWT, algorithms=[ALGORITHME])
        pseudo = payload.get("sub")
        if not pseudo:
            raise HTTPException(status_code=401, detail="Token invalide")
        return {"pseudo": pseudo, "est_admin": payload.get("est_admin", False)}
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")


_IMAGE_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"
}
_SUBTITLE_CACHE_DIR = pathlib.Path(tempfile.gettempdir()) / "danatrap_subtitles"
_SUBTITLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_SUBTITLE_LOCKS: dict[str, threading.Lock] = {}
_SUBTITLE_LOCKS_GUARD = threading.Lock()


def _subtitle_cache_path(video_id: str, track_index: int) -> pathlib.Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
    return _SUBTITLE_CACHE_DIR / f"{safe_id}_{track_index}.vtt"


def _subtitle_lock(video_id: str, track_index: int) -> threading.Lock:
    key = f"{video_id}:{track_index}"
    with _SUBTITLE_LOCKS_GUARD:
        return _SUBTITLE_LOCKS.setdefault(key, threading.Lock())


@app.get("/subtitle/{video_id}/{track_index}")
def get_subtitle(
    video_id: str,
    track_index: int,
    request: Request,
    token: Optional[str] = Query(None),
):
    _verifier_token_query(request, token)
    try:
        tracks = _probe_media(video_id)
        subtitles = tracks["subtitles"]
        if track_index < 0 or track_index >= len(subtitles):
            raise HTTPException(status_code=400, detail="Index de sous-titre invalide")

        selected = subtitles[track_index]
        codec = selected.get("codec", "")
        if codec in _IMAGE_SUBTITLE_CODECS:
            raise HTTPException(
                status_code=422,
                detail="Cette piste contient des sous-titres en image (PGS/DVD) et ne peut pas être affichée comme texte dans le navigateur.",
            )

        cache_path = _subtitle_cache_path(video_id, track_index)
        with _subtitle_lock(video_id, track_index):
            if not cache_path.exists() or cache_path.stat().st_size == 0:
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-headers", _get_drive_auth_header(),
                    "-i", _get_drive_media_url(video_id),
                    "-map", f"0:{selected['stream_index']}",
                    "-vn", "-an", "-dn",
                    "-c:s", "webvtt",
                    "-f", "webvtt",
                    str(cache_path),
                ]
                try:
                    result = subprocess.run(cmd, capture_output=True, timeout=180)
                except FileNotFoundError as exc:
                    raise HTTPException(status_code=503, detail="ffmpeg n'est pas installé sur Render") from exc
                except subprocess.TimeoutExpired as exc:
                    raise HTTPException(status_code=504, detail="Extraction des sous-titres trop longue") from exc
                if result.returncode != 0:
                    error = result.stderr.decode("utf-8", errors="replace").strip()[-1200:]
                    try:
                        cache_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise HTTPException(status_code=422, detail=f"Sous-titres incompatibles: {error or 'conversion impossible'}")

        data = cache_path.read_bytes()
        if not data.strip():
            raise HTTPException(status_code=422, detail="La piste de sous-titres est vide")
        return Response(
            content=data,
            media_type="text/vtt; charset=utf-8",
            headers={
                "Cache-Control": "private, max-age=3600",
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        print(f"[DanaTrap] extraction sous-titres impossible: {exc}")
        raise HTTPException(status_code=500, detail=f"Erreur extraction sous-titres: {exc}")



@app.get("/mux/{video_id}/{track_index}")
def get_muxed_video(
    video_id: str,
    track_index: int,
    request: Request,
    token: Optional[str] = Query(None),
    start: Optional[float] = Query(None),
):
    """Diffuse la vidéo et la piste audio choisie dans un seul flux MP4.

    Le son et l'image partagent ainsi la même horloge média. Cela évite les
    décalages et les micro-coupures provoqués par deux éléments HTML séparés.
    """
    _verifier_token_query(request, token)
    try:
        tracks = _probe_media(video_id)
        audio_tracks = tracks["audio"]
        if track_index < 0 or track_index >= len(audio_tracks):
            raise HTTPException(status_code=400, detail="Index audio invalide")

        video_stream_index = tracks.get("video_stream_index")
        if video_stream_index is None:
            raise HTTPException(status_code=422, detail="Aucune piste vidéo exploitable")

        selected = audio_tracks[track_index]
        audio_stream_index = selected.get("stream_index")
        if audio_stream_index is None:
            raise HTTPException(status_code=422, detail="Piste audio invalide")

        start_sec = max(0.0, float(start or 0.0))
        # Recherche rapide près de la position demandée, puis petite recherche
        # précise. On évite ainsi de relire tout le film depuis le début.
        coarse_seek = max(0.0, start_sec - 3.0)
        fine_seek = max(0.0, start_sec - coarse_seek)

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-headers", _get_drive_auth_header(),
        ]
        if coarse_seek > 0:
            cmd += ["-ss", f"{coarse_seek:.3f}"]
        cmd += ["-i", _get_drive_media_url(video_id)]
        if fine_seek > 0:
            cmd += ["-ss", f"{fine_seek:.3f}"]

        cmd += [
            "-map", f"0:{video_stream_index}",
            "-map", f"0:{audio_stream_index}",
            "-c:v", "copy",
            # On normalise toujours la piste choisie en AAC. Cela corrige les
            # timestamps irréguliers de certaines pistes AAC/AC3/E-AC3/DTS.
            "-c:a", "aac", "-b:a", "192k",
            "-af", "aresample=async=1000:first_pts=0",
            "-sn", "-dn",
            "-map_metadata", "-1",
            "-fflags", "+genpts",
            "-avoid_negative_ts", "make_zero",
            "-max_muxing_queue_size", "4096",
        ]
        if str(tracks.get("video_codec") or "").lower() in {"hevc", "h265"}:
            # Safari/iPhone et plusieurs téléviseurs attendent le tag hvc1.
            cmd += ["-tag:v", "hvc1"]
        cmd += [
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
            "-frag_duration", "1000000",
            "-flush_packets", "1",
            "pipe:1",
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="ffmpeg n'est pas installé sur Render") from exc

        def generate():
            try:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(128 * 1024)
                    if not chunk:
                        break
                    yield chunk
                code = process.wait(timeout=5)
                if code != 0 and process.stderr is not None:
                    error = process.stderr.read().decode("utf-8", errors="replace").strip()[-1200:]
                    print(f"[DanaTrap] mux {video_id}/{track_index} code={code}: {error}")
            except GeneratorExit:
                pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        process.kill()

        return StreamingResponse(
            generate(),
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
                "Accept-Ranges": "none",
            },
        )
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        print(f"[DanaTrap] mux audio/vidéo impossible: {exc}")
        raise HTTPException(status_code=500, detail=f"Erreur de lecture avec cette piste audio: {exc}")


@app.get("/audio/{video_id}/{track_index}")
def get_audio(
    video_id: str,
    track_index: int,
    request: Request,
    token: Optional[str] = Query(None),
    start: Optional[float] = Query(None),
):
    _verifier_token_query(request, token)
    try:
        tracks = _probe_media(video_id)
        audio_tracks = tracks["audio"]
        if track_index < 0 or track_index >= len(audio_tracks):
            raise HTTPException(status_code=400, detail="Index audio invalide")

        selected = audio_tracks[track_index]
        stream_index = selected.get("stream_index")
        codec = selected.get("codec", "")
        start_sec = max(0.0, float(start or 0.0))
        seek_args = ["-ss", f"{start_sec:.3f}"] if start_sec > 0 else []

        # AAC peut être remuxé sans perte. Les autres codecs sont convertis en AAC,
        # le format le plus compatible avec Chrome, Safari, iOS et Android.
        if codec == "aac":
            codec_args = ["-c:a", "copy"]
        else:
            codec_args = ["-c:a", "aac", "-b:a", "192k", "-af", "aresample=async=1:first_pts=0"]

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-headers", _get_drive_auth_header(),
            *seek_args,
            "-i", _get_drive_media_url(video_id),
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            *codec_args,
            "-map_metadata", "-1",
            "-avoid_negative_ts", "make_zero",
            "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "1000000",
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="ffmpeg n'est pas installé sur Render") from exc

        def generate():
            try:
                assert process.stdout is not None
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
                code = process.wait(timeout=5)
                if code != 0 and process.stderr is not None:
                    error = process.stderr.read().decode("utf-8", errors="replace").strip()[-1000:]
                    print(f"[DanaTrap] ffmpeg audio {video_id}/{track_index} code={code}: {error}")
            except GeneratorExit:
                pass
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except Exception:
                        process.kill()

        return StreamingResponse(
            generate(),
            media_type="audio/mp4",
            headers={
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except HTTPException:
        raise
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        print(f"[DanaTrap] extraction audio impossible: {exc}")
        raise HTTPException(status_code=500, detail=f"Erreur extraction audio: {exc}")


# ==================== FRONTEND STATIQUE (DOIT ETRE EN DERNIER !) ====================
_FRONTEND_DIR = (pathlib.Path(__file__).parent.parent / "frontend").resolve()
if (_FRONTEND_DIR / "index.html").exists():
    ROUTES_PROTEGEES = {
        "login", "bibliotheque", "stream", "tracks", "mux", "audio", "subtitle", "admin", "api",
        "favicon.ico", "token.pickle", "credentials.json",
        "utilisateurs.json", "static",
    }
    EXTENSIONS_BLOQUEES = {".py", ".json", ".pickle", ".log", ".env", ".txt"}

    @app.get("/", include_in_schema=False)
    def _serve_index():
        resp = FileResponse(str(_FRONTEND_DIR / "index.html"), media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

    @app.get("/{filename:path}", include_in_schema=False)
    def _serve_static(filename: str):
        if ".." in filename or filename.startswith("/") or "\\" in filename:
            raise HTTPException(status_code=400, detail="Chemin invalide")
        premier = filename.split("/")[0].lower()
        if premier in ROUTES_PROTEGEES:
            raise HTTPException(status_code=404, detail="Route inconnue")
        import mimetypes
        ext = os.path.splitext(filename)[1].lower()
        if ext in EXTENSIONS_BLOQUEES:
            raise HTTPException(status_code=403, detail="Type de fichier interdit")
        fichier = _FRONTEND_DIR / filename
        if not fichier.is_file():
            raise HTTPException(status_code=404, detail="Fichier introuvable")
        mime, _ = mimetypes.guess_type(str(fichier))
        if mime is None:
            mime = "application/octet-stream"
        return FileResponse(str(fichier), media_type=mime)
    print(f"Frontend servi depuis {_FRONTEND_DIR}")
else:
    print(f"ATTENTION: dossier frontend introuvable a {_FRONTEND_DIR}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
