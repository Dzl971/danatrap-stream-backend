from fastapi import FastAPI, HTTPException, Depends, Request, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaFileUpload
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
import gzip
import mimetypes
from difflib import SequenceMatcher
from PIL import Image, ImageOps

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

# ==================== STEP 1 : PERFORMANCES ====================
CATALOGUE_CACHE_FILENAME = "dts_catalogue_cache.json"
THUMBNAIL_MAP_FILENAME = "dts_poster_thumbnails.json"
THUMBNAIL_FOLDER_NAME = "Miniatures DTS"
THUMBNAIL_WIDTH = max(180, int(os.environ.get("THUMBNAIL_WIDTH", "360")))
THUMBNAIL_HEIGHT = max(270, int(os.environ.get("THUMBNAIL_HEIGHT", "540")))
THUMBNAIL_QUALITY = max(55, min(90, int(os.environ.get("THUMBNAIL_QUALITY", "78"))))
POSTER_CACHE_DIR = pathlib.Path(tempfile.gettempdir()) / "dts_poster_cache"
POSTER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_POSTER_LOCKS: dict[str, threading.Lock] = {}
_POSTER_LOCKS_GUARD = threading.Lock()
_THUMBNAIL_MAP_LOCK = threading.Lock()
_THUMBNAIL_PERSIST_SEMAPHORE = threading.Semaphore(2)
_STATIC_GZIP_CACHE: dict[str, tuple[float, bytes]] = {}

# ==================== AUDIO UNIVERSEL DTS ====================
# Les pistes incompatibles avec les navigateurs sont converties une seule fois
# en AAC-LC stéréo 48 kHz, puis conservées dans Google Drive. Le fichier vidéo
# original n'est jamais modifié.
AUDIO_COMPATIBILITY_FILENAME = "dts_audio_compatibility.json"
AUDIO_CACHE_PARENT_FOLDER = "Cache DTS"
AUDIO_CACHE_FOLDER_NAME = "Audio compatible"
AUDIO_CACHE_BITRATE = str(os.environ.get("AUDIO_CACHE_BITRATE", "160k")).strip() or "160k"
AUTO_AUDIO_PREPARE = str(os.environ.get("AUTO_AUDIO_PREPARE", "1")).strip().lower() not in {"0", "false", "non", "no"}
# Analyse distante plus tolérante : les gros MKV/MP4 de Drive peuvent être lents
# ou temporairement limités (HTTP 429/5xx).
AUDIO_PROBE_TIMEOUT = max(90, int(os.environ.get("AUDIO_PROBE_TIMEOUT", "240")))
AUDIO_PROBE_RETRIES = max(1, min(5, int(os.environ.get("AUDIO_PROBE_RETRIES", "3"))))
AUDIO_PROBE_RETRY_DELAY = max(1.0, float(os.environ.get("AUDIO_PROBE_RETRY_DELAY", "4")))
AUDIO_CACHE_LOCAL_DIR = pathlib.Path(tempfile.gettempdir()) / "dts_audio_compatible"
AUDIO_CACHE_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
_AUDIO_MAPPING_LOCK = threading.Lock()
_AUDIO_CONVERSION_LOCKS: dict[str, threading.Lock] = {}
_AUDIO_CONVERSION_LOCKS_GUARD = threading.Lock()
_AUDIO_CONVERSION_SEMAPHORE = threading.Semaphore(1)
_AUDIO_PREPARE_LOCK = threading.Lock()
_AUDIO_PREPARE_JOB: dict[str, Any] = {
    "running": False, "phase": "idle", "progress": 0, "total": 0,
    "videos": 0, "tracks": 0, "direct_compatible": 0, "prepared": 0,
    "pending": 0, "errors": 0, "current": "", "items": [],
    "started_at": None, "finished_at": None, "error": None,
}


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


CATALOGUE_METADATA_FILENAME = "catalogue_metadata.json"
_METADATA_CACHE: dict[str, Any] = {"cached_at": 0.0, "data": None}
_METADATA_CACHE_LOCK = threading.Lock()
_METADATA_CACHE_TTL = int(os.environ.get("METADATA_CACHE_TTL", "30"))

# Champs administrables. Les valeurs présentes dans catalogue_metadata.json
# ont toujours priorité sur les informations automatiques de TMDB.
_METADATA_FIELDS = {
    "titre", "synopsis", "annee", "duree", "genres", "certification",
    "note", "langue_originale", "acteurs", "realisateurs", "poster_url",
    "backdrop_url", "bande_annonce_url", "statut", "prochaine_sortie",
    "saga", "ordre_saga", "alias_recherche",
}


def trouver_fichier_metadata_catalogue(service, racine_id):
    resultats = service.files().list(
        q=f"name = '{CATALOGUE_METADATA_FILENAME}' and '{racine_id}' in parents and trashed = false",
        fields="files(id, name)",
    ).execute()
    fichiers = resultats.get('files', [])
    return fichiers[0]['id'] if fichiers else None


def _normaliser_liste_metadata(valeur: Any) -> list[str]:
    if valeur is None:
        return []
    if isinstance(valeur, str):
        items = re.split(r"[,;\n]", valeur)
    elif isinstance(valeur, list):
        items = valeur
    else:
        return []
    resultat = []
    for item in items:
        texte = str(item or "").strip()
        if texte and texte not in resultat:
            resultat.append(texte[:160])
    return resultat[:30]


def _normaliser_override_metadata(valeur: Any) -> dict:
    if not isinstance(valeur, dict):
        return {}
    resultat: dict[str, Any] = {}
    for champ in _METADATA_FIELDS:
        if champ not in valeur:
            continue
        brut = valeur.get(champ)
        if champ in {"genres", "acteurs", "realisateurs"}:
            resultat[champ] = _normaliser_liste_metadata(brut)
        elif champ == "note":
            if brut in (None, ""):
                resultat[champ] = None
            else:
                try:
                    resultat[champ] = max(0.0, min(10.0, round(float(brut), 1)))
                except (TypeError, ValueError):
                    resultat[champ] = None
        else:
            texte = "" if brut is None else str(brut).strip()
            limites = {
                "titre": 240, "synopsis": 5000, "annee": 20, "duree": 80,
                "certification": 40, "langue_originale": 30,
                "poster_url": 2000, "backdrop_url": 2000,
                "bande_annonce_url": 2000, "statut": 30, "prochaine_sortie": 40,
                "saga": 240, "ordre_saga": 20, "alias_recherche": 500,
            }
            if champ == "statut":
                aliases_statut = {
                    "en cours": "En cours", "encours": "En cours", "termine": "Terminé",
                    "terminé": "Terminé", "fini": "Terminé", "abandonne": "Abandonné",
                    "abandonné": "Abandonné", "auto": "Automatique", "automatique": "Automatique",
                }
                texte = aliases_statut.get(_retirer_accents(texte).lower().strip(), texte)
                if texte not in {"Automatique", "En cours", "Terminé", "Abandonné"}:
                    texte = "Automatique"
            resultat[champ] = texte[:limites.get(champ, 1000)]
    resultat["updated_at"] = str(valeur.get("updated_at") or datetime.utcnow().isoformat(timespec="seconds") + "Z")
    return resultat


def charger_metadata_catalogue(force: bool = False) -> dict:
    now = time.time()
    with _METADATA_CACHE_LOCK:
        cached = _METADATA_CACHE.get("data")
        if not force and isinstance(cached, dict) and now - float(_METADATA_CACHE.get("cached_at") or 0) < _METADATA_CACHE_TTL:
            return json.loads(json.dumps(cached, ensure_ascii=False))

    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        return {}
    fichier_id = trouver_fichier_metadata_catalogue(service, racine_id)
    if not fichier_id:
        data = {}
    else:
        access_token = get_access_token()
        url = f"https://www.googleapis.com/drive/v3/files/{fichier_id}?alt=media"
        reponse = requests.get(url, headers={'Authorization': f'Bearer {access_token}'}, timeout=20)
        reponse.raise_for_status()
        brut = reponse.json()
        data = brut if isinstance(brut, dict) else {}
    normalise = {str(cle): _normaliser_override_metadata(valeur) for cle, valeur in data.items() if isinstance(valeur, dict)}
    with _METADATA_CACHE_LOCK:
        _METADATA_CACHE["cached_at"] = now
        _METADATA_CACHE["data"] = normalise
    return json.loads(json.dumps(normalise, ensure_ascii=False))


def sauvegarder_metadata_catalogue(metadata: dict):
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise RuntimeError("Dossier racine 'Danatrap Stream' introuvable sur Google Drive")
    fichier_id = trouver_fichier_metadata_catalogue(service, racine_id)
    normalise = {str(cle): _normaliser_override_metadata(valeur) for cle, valeur in metadata.items() if isinstance(valeur, dict)}
    contenu_json = json.dumps(normalise, indent=2, ensure_ascii=False)
    media = MediaIoBaseUpload(io.BytesIO(contenu_json.encode('utf-8')), mimetype='application/json', resumable=False)
    if fichier_id:
        service.files().update(fileId=fichier_id, media_body=media).execute()
    else:
        meta_fichier = {'name': CATALOGUE_METADATA_FILENAME, 'parents': [racine_id]}
        service.files().create(body=meta_fichier, media_body=media, fields='id').execute()
    with _METADATA_CACHE_LOCK:
        _METADATA_CACHE["cached_at"] = time.time()
        _METADATA_CACHE["data"] = normalise




# ==================== DONNÉES DTS SUR GOOGLE DRIVE ====================
SORTIES_FILENAME = "sorties_dts.json"
ADMIN_LOGS_FILENAME = "admin_logs.json"
BACKUP_FOLDER_NAME = "Sauvegardes DTS"
_GENERIC_JSON_CACHE: dict[str, dict[str, Any]] = {}
_GENERIC_JSON_LOCK = threading.Lock()


def _trouver_fichier_dans_dossier(service, parent_id: str, nom: str) -> Optional[str]:
    nom_sure = str(nom).replace("'", "\\'")
    resultats = service.files().list(
        q=f"name = '{nom_sure}' and '{parent_id}' in parents and trashed = false",
        fields="files(id,name,modifiedTime)", pageSize=10,
    ).execute()
    fichiers = resultats.get("files", [])
    return fichiers[0]["id"] if fichiers else None


def _charger_json_drive(nom: str, valeur_defaut: Any, force: bool = False) -> Any:
    now = time.time()
    with _GENERIC_JSON_LOCK:
        cache = _GENERIC_JSON_CACHE.get(nom)
        if not force and cache and now - float(cache.get("cached_at") or 0) < 30:
            return json.loads(json.dumps(cache.get("data"), ensure_ascii=False))
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        return json.loads(json.dumps(valeur_defaut, ensure_ascii=False))
    fichier_id = _trouver_fichier_dans_dossier(service, racine_id, nom)
    data = valeur_defaut
    if fichier_id:
        try:
            reponse = requests.get(
                f"https://www.googleapis.com/drive/v3/files/{fichier_id}?alt=media",
                headers={'Authorization': f'Bearer {get_access_token()}'}, timeout=20,
            )
            reponse.raise_for_status()
            data = reponse.json()
        except Exception as exc:
            print(f"[DTS] lecture de {nom} impossible: {exc}")
            data = valeur_defaut
    with _GENERIC_JSON_LOCK:
        _GENERIC_JSON_CACHE[nom] = {"cached_at": now, "data": data}
    return json.loads(json.dumps(data, ensure_ascii=False))


def _sauvegarder_json_drive(nom: str, data: Any) -> str:
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise RuntimeError("Dossier racine 'Danatrap Stream' introuvable sur Google Drive")
    fichier_id = _trouver_fichier_dans_dossier(service, racine_id, nom)
    contenu = json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')
    media = MediaIoBaseUpload(io.BytesIO(contenu), mimetype='application/json', resumable=False)
    if fichier_id:
        service.files().update(fileId=fichier_id, media_body=media).execute()
    else:
        fichier_id = service.files().create(
            body={'name': nom, 'parents': [racine_id]}, media_body=media, fields='id'
        ).execute().get('id')
    with _GENERIC_JSON_LOCK:
        _GENERIC_JSON_CACHE[nom] = {"cached_at": time.time(), "data": data}
    return str(fichier_id or "")


def _charger_cache_catalogue_persistant(force: bool = False) -> Optional[dict]:
    """Charge le dernier catalogue préconstruit depuis un seul fichier Drive."""
    try:
        data = _charger_json_drive(CATALOGUE_CACHE_FILENAME, {}, force)
        if isinstance(data, dict) and isinstance(data.get("films"), list):
            return data
    except Exception as exc:
        print(f"[DTS] cache catalogue persistant indisponible: {exc}")
    return None


def _sauvegarder_cache_catalogue_persistant(data: dict) -> None:
    try:
        _sauvegarder_json_drive(CATALOGUE_CACHE_FILENAME, data)
    except Exception as exc:
        print(f"[DTS] sauvegarde du cache catalogue impossible: {exc}")


def _etag_json(data: Any) -> tuple[str, bytes]:
    brut = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return '"' + hashlib.sha256(brut).hexdigest()[:32] + '"', brut


def _reponse_json_optimisee(request: Request, data: Any, cache_control: str = "private, max-age=60, stale-while-revalidate=86400") -> Response:
    etag, brut = _etag_json(data)
    headers = {"Cache-Control": cache_control, "ETag": etag, "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in request.headers.get("accept-encoding", "").lower() and len(brut) >= 1024:
        brut = gzip.compress(brut, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return Response(content=brut, media_type="application/json; charset=utf-8", headers=headers)


def _poster_lock(file_id: str) -> threading.Lock:
    with _POSTER_LOCKS_GUARD:
        return _POSTER_LOCKS.setdefault(file_id, threading.Lock())


def _telecharger_fichier_drive(file_id: str, timeout: int = 30) -> tuple[bytes, str]:
    reponse = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
        headers={"Authorization": f"Bearer {get_access_token()}"}, timeout=timeout,
    )
    reponse.raise_for_status()
    return reponse.content, str(reponse.headers.get("Content-Type") or "application/octet-stream").split(";")[0]


def _creer_miniature_webp(source: bytes) -> bytes:
    with Image.open(io.BytesIO(source)) as image:
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        # Recadrage léger au format affiche 2:3, sans déformation.
        cible_ratio = THUMBNAIL_WIDTH / THUMBNAIL_HEIGHT
        ratio = image.width / max(1, image.height)
        if ratio > cible_ratio:
            largeur = int(image.height * cible_ratio)
            gauche = max(0, (image.width - largeur) // 2)
            image = image.crop((gauche, 0, gauche + largeur, image.height))
        elif ratio < cible_ratio:
            hauteur = int(image.width / cible_ratio)
            haut = max(0, (image.height - hauteur) // 2)
            image = image.crop((0, haut, image.width, haut + hauteur))
        image.thumbnail((THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT), Image.Resampling.LANCZOS)
        sortie = io.BytesIO()
        image.save(sortie, format="WEBP", quality=THUMBNAIL_QUALITY, method=4, optimize=True)
        return sortie.getvalue()


def _persister_miniature_drive(poster_id: str, contenu: bytes) -> None:
    """Enregistre la miniature sur Drive pour survivre aux redémarrages Render."""
    try:
        with _THUMBNAIL_PERSIST_SEMAPHORE:
            with _THUMBNAIL_MAP_LOCK:
                mapping = _charger_json_drive(THUMBNAIL_MAP_FILENAME, {}, force=True)
                if isinstance(mapping, dict) and mapping.get(poster_id):
                    return
                _charger_si_manquant()
                service = build('drive', 'v3', credentials=CREDENTIALS)
                racine_id = trouver_dossier_racine(service)
                if not racine_id:
                    return
                dossier_id = _trouver_ou_creer_dossier(service, racine_id, THUMBNAIL_FOLDER_NAME)
                nom = f"{poster_id}.webp"
                existant = _trouver_fichier_dans_dossier(service, dossier_id, nom)
                media = MediaIoBaseUpload(io.BytesIO(contenu), mimetype='image/webp', resumable=False)
                if existant:
                    fichier_id = service.files().update(fileId=existant, media_body=media, fields='id').execute().get('id')
                else:
                    fichier_id = service.files().create(
                        body={'name': nom, 'parents': [dossier_id]}, media_body=media, fields='id'
                    ).execute().get('id')
                if fichier_id:
                    mapping = mapping if isinstance(mapping, dict) else {}
                    mapping[poster_id] = {"file_id": fichier_id, "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
                    _sauvegarder_json_drive(THUMBNAIL_MAP_FILENAME, mapping)
    except Exception as exc:
        print(f"[DTS] miniature Drive non persistée pour {poster_id}: {exc}")


def _miniature_poster(poster_id: str) -> tuple[pathlib.Path, str]:
    nom_sure = re.sub(r'[^A-Za-z0-9_.-]', '_', poster_id)
    chemin = POSTER_CACHE_DIR / f"{nom_sure}.webp"
    if chemin.exists() and chemin.stat().st_size > 0:
        return chemin, "image/webp"
    with _poster_lock(poster_id):
        if chemin.exists() and chemin.stat().st_size > 0:
            return chemin, "image/webp"
        # Essaie d'abord la miniature déjà persistée sur Google Drive.
        mapping = _charger_json_drive(THUMBNAIL_MAP_FILENAME, {}, force=False)
        fiche = mapping.get(poster_id) if isinstance(mapping, dict) else None
        thumb_id = fiche.get("file_id") if isinstance(fiche, dict) else fiche
        if thumb_id:
            try:
                contenu, _ = _telecharger_fichier_drive(str(thumb_id), timeout=20)
                if contenu:
                    chemin.write_bytes(contenu)
                    return chemin, "image/webp"
            except Exception as exc:
                print(f"[DTS] miniature persistée invalide pour {poster_id}: {exc}")
        source, source_mime = _telecharger_fichier_drive(poster_id, timeout=35)
        try:
            contenu = _creer_miniature_webp(source)
            chemin.write_bytes(contenu)
            threading.Thread(target=_persister_miniature_drive, args=(poster_id, contenu), daemon=True).start()
            return chemin, "image/webp"
        except Exception as exc:
            # Fallback : l'affiche originale reste visible si Pillow ne sait pas lire un format rare.
            print(f"[DTS] conversion WebP impossible pour {poster_id}, original utilisé: {exc}")
            if not source_mime.startswith("image/"):
                raise
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/avif": ".avif"}.get(source_mime, ".img")
            original = POSTER_CACHE_DIR / f"{nom_sure}{extension}"
            original.write_bytes(source)
            return original, source_mime


def charger_sorties(force: bool = False) -> list[dict]:
    data = _charger_json_drive(SORTIES_FILENAME, [], force)
    if not isinstance(data, list):
        return []
    resultat = []
    for item in data:
        if not isinstance(item, dict):
            continue
        titre = str(item.get("titre") or "").strip()[:240]
        date = str(item.get("date") or "").strip()[:40]
        if not titre or not date:
            continue
        resultat.append({
            "id": str(item.get("id") or hashlib.sha1(f"{titre}|{date}".encode()).hexdigest()[:12]),
            "titre": titre, "date": date,
            "description": str(item.get("description") or "").strip()[:1500],
            "categorie": str(item.get("categorie") or "").strip()[:30],
            "contenu_id": str(item.get("contenu_id") or "").strip()[:200],
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
        })
    resultat.sort(key=lambda x: x.get("date") or "")
    return resultat


def _journaliser_admin(admin: str, action: str, details: Any = None):
    try:
        logs = _charger_json_drive(ADMIN_LOGS_FILENAME, [], force=True)
        if not isinstance(logs, list):
            logs = []
        logs.insert(0, {
            "id": hashlib.sha1(f"{time.time_ns()}|{admin}|{action}".encode()).hexdigest()[:16],
            "date": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "admin": str(admin or ADMIN_PSEUDO),
            "action": str(action or "Action")[:240],
            "details": details if isinstance(details, (dict, list, str, int, float, bool)) or details is None else str(details),
        })
        _sauvegarder_json_drive(ADMIN_LOGS_FILENAME, logs[:500])
    except Exception as exc:
        print(f"[DTS] journal administrateur non enregistré: {exc}")


def _trouver_ou_creer_dossier(service, parent_id: str, nom: str) -> str:
    existant = get_id_dossier(service, nom, parent_id)
    if existant:
        return existant
    return service.files().create(
        body={'name': nom, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]},
        fields='id',
    ).execute()['id']


def creer_sauvegarde_drive(admin: str = "SYSTEM", automatique: bool = False) -> dict:
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise RuntimeError("Dossier racine introuvable")
    dossier_id = _trouver_ou_creer_dossier(service, racine_id, BACKUP_FOLDER_NAME)
    stamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    nom = f"sauvegarde_dts_{stamp}.json"
    payload = {
        "version": "6.0", "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "automatic": bool(automatique),
        "utilisateurs": charger_utilisateurs(force=True),
        "catalogue_metadata": charger_metadata_catalogue(force=True),
        "sorties": charger_sorties(force=True),
        "audio_compatibility": _charger_json_drive(AUDIO_COMPATIBILITY_FILENAME, {}, force=True),
        "admin_logs": _charger_json_drive(ADMIN_LOGS_FILENAME, [], force=True),
    }
    media = MediaIoBaseUpload(io.BytesIO(json.dumps(payload, indent=2, ensure_ascii=False).encode('utf-8')), mimetype='application/json', resumable=False)
    fichier = service.files().create(body={'name': nom, 'parents': [dossier_id]}, media_body=media, fields='id,name').execute()
    if not automatique:
        _journaliser_admin(admin, "Sauvegarde manuelle créée", {"fichier": nom})
    return {"id": fichier.get("id"), "name": fichier.get("name"), "created_at": payload["created_at"]}


def _assurer_sauvegarde_quotidienne():
    try:
        _charger_si_manquant()
        service = build('drive', 'v3', credentials=CREDENTIALS)
        racine_id = trouver_dossier_racine(service)
        if not racine_id:
            return
        dossier_id = _trouver_ou_creer_dossier(service, racine_id, BACKUP_FOLDER_NAME)
        prefixe = "sauvegarde_dts_" + datetime.utcnow().strftime("%Y-%m-%d")
        resultats = service.files().list(
            q=f"'{dossier_id}' in parents and name contains '{prefixe}' and trashed = false",
            fields="files(id,name)", pageSize=5,
        ).execute()
        if not resultats.get("files"):
            creer_sauvegarde_drive("SYSTEM", automatique=True)
    except Exception as exc:
        print(f"[DTS] sauvegarde quotidienne impossible: {exc}")


def _boucle_sauvegarde_automatique():
    # Vérifie au démarrage puis toutes les six heures qu'une sauvegarde du jour existe.
    time.sleep(20)
    while True:
        _assurer_sauvegarde_quotidienne()
        time.sleep(6 * 60 * 60)

def _cle_metadata(categorie: str, contenu_id: str) -> str:
    return f"{categorie}:{contenu_id}"


def _appliquer_metadata_manuelle(contenu: dict, categorie: str, contenu_id: str, overrides: dict) -> dict:
    resultat = dict(contenu)
    resultat["contenu_id"] = contenu_id
    resultat["categorie"] = categorie
    resultat.setdefault("titre_original", resultat.get("titre"))
    manuel = overrides.get(_cle_metadata(categorie, contenu_id))
    if isinstance(manuel, dict) and manuel:
        for champ in _METADATA_FIELDS:
            if champ in manuel:
                resultat[champ] = manuel[champ]
        resultat["metadata_manuel"] = True
        resultat["metadata_updated_at"] = manuel.get("updated_at")
    else:
        resultat["metadata_manuel"] = False
        resultat["metadata_updated_at"] = None
    return resultat


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


@app.on_event("startup")
def demarrer_sauvegardes_automatiques():
    threading.Thread(target=_boucle_sauvegarde_automatique, name="dts-backup", daemon=True).start()

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


class MiseAJourMetadataContenu(BaseModel):
    titre: Optional[str] = None
    synopsis: Optional[str] = None
    annee: Optional[str] = None
    duree: Optional[str] = None
    genres: Optional[list[str]] = None
    certification: Optional[str] = None
    note: Optional[float] = None
    langue_originale: Optional[str] = None
    acteurs: Optional[list[str]] = None
    realisateurs: Optional[list[str]] = None
    poster_url: Optional[str] = None
    backdrop_url: Optional[str] = None
    bande_annonce_url: Optional[str] = None
    statut: Optional[str] = None
    prochaine_sortie: Optional[str] = None
    saga: Optional[str] = None
    ordre_saga: Optional[str] = None
    alias_recherche: Optional[str] = None


class SortiePlanifiee(BaseModel):
    titre: str
    date: str
    description: Optional[str] = None
    categorie: Optional[str] = None
    contenu_id: Optional[str] = None




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
    _journaliser_admin(admin, "Utilisateur créé", {"pseudo": pseudo, "expires_at": data.expires_at, "max_devices": data.max_devices})
    _assurer_sauvegarde_quotidienne()
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
    _journaliser_admin(admin, "Compte utilisateur modifié", {"pseudo": pseudo, "expires_at": fiche.get("expires_at"), "max_devices": fiche.get("max_devices")})
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
    _journaliser_admin(admin, "Tous les appareils déconnectés", {"pseudo": pseudo})
    return {"message": f"Tous les appareils de '{pseudo}' ont été déconnectés"}



@app.delete("/admin/utilisateur/{pseudo}/appareil/{device_id}")
def deconnecter_un_appareil(pseudo: str, device_id: str, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    if pseudo not in utilisateurs:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    device_id = _nettoyer_device_id(device_id)
    devices = utilisateurs[pseudo].get("devices") or {}
    if device_id not in devices:
        raise HTTPException(status_code=404, detail="Appareil introuvable")
    appareil = devices.pop(device_id)
    utilisateurs[pseudo]["devices"] = devices
    sauvegarder_utilisateurs(utilisateurs)
    _journaliser_admin(admin, "Appareil déconnecté", {"pseudo": pseudo, "device_id": device_id, "name": (appareil or {}).get("name") if isinstance(appareil, dict) else ""})
    return {"message": "Appareil déconnecté", "device_count": len(devices)}


@app.delete("/admin/supprimer-utilisateur/{pseudo}")
def supprimer_utilisateur(pseudo: str, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs(force=True)
    if pseudo not in utilisateurs:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable")
    if pseudo == ADMIN_PSEUDO:
        raise HTTPException(status_code=400, detail="Impossible de supprimer le compte administrateur")
    del utilisateurs[pseudo]
    sauvegarder_utilisateurs(utilisateurs)
    _journaliser_admin(admin, "Utilisateur supprimé", {"pseudo": pseudo})
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
        fields="files(id,name,mimeType,modifiedTime,size,md5Checksum,videoMediaMetadata(width,height,durationMillis))",
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
            "collection_id": ((details.get("belongs_to_collection") or {}).get("id") if type_contenu == "film" else None),
            "collection_name": ((details.get("belongs_to_collection") or {}).get("name") if type_contenu == "film" else None),
            "tmdb_status": details.get("status"),
            "tmdb_total_episodes": details.get("number_of_episodes") if type_contenu != "film" else None,
        }
        cache_tmdb[cle_cache] = resultat
        return resultat
    except Exception as e:
        print(f"Erreur TMDB pour '{titre}': {e}")
        return {}



def _statut_automatique(contenu: dict, categorie: str) -> str:
    manuel = str(contenu.get("statut") or "Automatique").strip()
    if manuel in {"En cours", "Terminé", "Abandonné"}:
        return manuel
    if categorie == "films":
        return "Terminé" if contenu.get("video_id") else "En cours"
    nb_local = sum(len(s.get("episodes") or []) for s in contenu.get("saisons") or [])
    total = int(contenu.get("tmdb_total_episodes") or 0)
    statut_tmdb = str(contenu.get("tmdb_status") or "").lower()
    if statut_tmdb in {"canceled", "cancelled"}:
        return "Abandonné"
    if statut_tmdb in {"ended"} and nb_local > 0 and (not total or nb_local >= total):
        return "Terminé"
    return "En cours"


def _finaliser_statut(contenu: dict, categorie: str) -> dict:
    resultat = dict(contenu)
    statut_demande = str(resultat.get("statut") or "Automatique").strip()
    resultat["statut_auto"] = statut_demande not in {"En cours", "Terminé", "Abandonné"}
    resultat["statut"] = _statut_automatique(resultat, categorie)
    return resultat


def _base_saga_titre(titre: str) -> str:
    propre = _nettoyer_titre_tmdb(titre)
    propre = re.sub(r"\b(?:partie|part|chapitre|chapter|volume|vol|film|movie|saison|season)\s*[0-9ivx]+\b", " ", propre, flags=re.I)
    propre = re.sub(r"\b[0-9ivx]{1,4}\b$", " ", propre, flags=re.I)
    propre = re.split(r"\s*[:\-–—]\s*", propre)[0]
    return re.sub(r"\s+", " ", propre).strip()


def _construire_collections(data: dict) -> list[dict]:
    groupes: dict[str, dict] = {}
    for categorie, kind in (("films", "film"), ("series", "series"), ("anime", "anime")):
        for item in data.get(categorie, []):
            nom = str(item.get("saga") or item.get("collection_name") or "").strip()
            if not nom:
                base = _base_saga_titre(item.get("titre") or "")
                # Une collection automatique par préfixe n'est retenue que si plusieurs titres correspondent.
                nom = base
            cle = _normaliser_titre_comparaison(nom)
            if len(cle) < 3:
                continue
            groupe = groupes.setdefault(cle, {"nom": nom, "items": [], "automatique": not bool(item.get("saga"))})
            groupe["items"].append({
                "categorie": categorie, "kind": kind, "contenu_id": item.get("contenu_id"),
                "titre": item.get("titre"), "annee": item.get("annee"),
                "ordre": item.get("ordre_saga"), "poster_id": item.get("poster_id"),
            })
    resultat = []
    for groupe in groupes.values():
        if len(groupe["items"]) < 2:
            continue
        def ordre(item):
            raw = str(item.get("ordre") or "").strip()
            try: ordre_manuel = float(raw)
            except Exception: ordre_manuel = 999999.0
            annee = int(re.sub(r"\D", "", str(item.get("annee") or ""))[:4] or 9999)
            return (ordre_manuel, annee, str(item.get("titre") or "").lower())
        groupe["items"].sort(key=ordre)
        resultat.append(groupe)
    resultat.sort(key=lambda g: (-len(g["items"]), str(g["nom"]).lower()))
    return resultat[:50]


def scanner_films(service, films_id, overrides: Optional[dict] = None):
    films = []
    overrides = overrides or {}
    for dossier in lister_contenu(service, films_id):
        if dossier['mimeType'] == 'application/vnd.google-apps.folder':
            contenu = lister_contenu(service, dossier['id'])
            video = next((f for f in contenu if f['mimeType'].startswith('video/')), None)
            poster = next((f for f in contenu if f['name'].lower() in {'icon.jpg', 'icon.jpeg', 'icon.png', 'icon.webp', 'poster.jpg', 'poster.jpeg', 'poster.png', 'poster.webp', 'icon.avif', 'poster.avif'}), None)
            infos_tmdb = rechercher_tmdb(dossier['name'], "film")
            contenu = {
                "titre": dossier['name'],
                "titre_original": dossier['name'],
                "video_id": video['id'] if video else None,
                "video_nom": video.get('name') if video else None,
                "poster_id": poster['id'] if poster else None,
                "date_ajout": dossier.get("modifiedTime") or (video or {}).get("modifiedTime"),
                "qualite": _qualite_video(video),
                "duree_drive": _duree_drive(video),
                **infos_tmdb
            }
            films.append(_finaliser_statut(_appliquer_metadata_manuelle(contenu, "films", dossier['id'], overrides), "films"))
    films.sort(key=lambda f: f.get("date_ajout") or "", reverse=True)
    return films


def scanner_series(service, series_id, categorie: str = "series", overrides: Optional[dict] = None):
    series = []
    overrides = overrides or {}
    for dossier_serie in lister_contenu(service, series_id):
        if dossier_serie['mimeType'] == 'application/vnd.google-apps.folder':
            contenu_serie = lister_contenu(service, dossier_serie['id'])
            poster = next((f for f in contenu_serie if f['name'].lower() in {'icon.jpg', 'icon.jpeg', 'icon.png', 'icon.webp', 'poster.jpg', 'poster.jpeg', 'poster.png', 'poster.webp', 'icon.avif', 'poster.avif'}), None)
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
                    episodes.sort(key=lambda ep: [int(x) for x in re.findall(r"\d+", ep.get("nom") or "")[:2]] or [0])
                    saisons.append({"nom_saison": dossier_saison['name'], "episodes": episodes, "date_ajout": dossier_saison.get("modifiedTime")})
            saisons.sort(key=lambda sa: int((re.findall(r"\d+", sa.get("nom_saison") or "0") or [0])[0]))
            infos_tmdb = rechercher_tmdb(dossier_serie['name'], "tv")
            contenu = {
                "titre": dossier_serie['name'],
                "titre_original": dossier_serie['name'],
                "poster_id": poster['id'] if poster else None,
                "date_ajout": dossier_serie.get("modifiedTime"),
                "saisons": saisons,
                **infos_tmdb
            }
            series.append(_finaliser_statut(_appliquer_metadata_manuelle(contenu, categorie, dossier_serie['id'], overrides), categorie))
    series.sort(key=lambda f: f.get("date_ajout") or "", reverse=True)
    return series


_LIBRARY_CACHE: dict[str, Any] = {"cached_at": 0.0, "data": None}
_LIBRARY_CACHE_LOCK = threading.Lock()
_LIBRARY_PERSISTENT_DIRTY = False


def _scanner_bibliotheque(force: bool = False) -> dict:
    global _LIBRARY_PERSISTENT_DIRTY
    now = time.time()
    with _LIBRARY_CACHE_LOCK:
        cached = _LIBRARY_CACHE.get("data")
        if not force and isinstance(cached, dict) and now - float(_LIBRARY_CACHE.get("cached_at") or 0) < LIBRARY_CACHE_TTL:
            return cached
    # Après un redémarrage Render, un seul petit JSON Drive suffit au lieu de rescanner tous les dossiers.
    if not force and not _LIBRARY_PERSISTENT_DIRTY:
        persistant = _charger_cache_catalogue_persistant(force=False)
        if persistant:
            with _LIBRARY_CACHE_LOCK:
                _LIBRARY_CACHE["cached_at"] = now
                _LIBRARY_CACHE["data"] = persistant
            return persistant
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = get_id_dossier(service, "Danatrap Stream")
    if not racine_id:
        raise HTTPException(status_code=404, detail="Dossier 'Danatrap Stream' introuvable sur Google Drive")
    films_id = trouver_dossier_flexible(service, ["Films", "FILMS", "films"], racine_id)
    series_id = trouver_dossier_flexible(service, ["Séries", "Series", "SÉRIES", "SERIES", "séries", "series"], racine_id)
    anime_id = trouver_dossier_flexible(service, ["Animes", "Anime", "ANIMES", "ANIME", "animes", "anime"], racine_id)
    overrides = charger_metadata_catalogue()
    data = {
        "films": scanner_films(service, films_id, overrides) if films_id else [],
        "series": scanner_series(service, series_id, "series", overrides) if series_id else [],
        "anime": scanner_series(service, anime_id, "anime", overrides) if anime_id else [],
        "sorties": charger_sorties(),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "cache_version": 2,
    }
    data["collections"] = _construire_collections(data)
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE["cached_at"] = now
        _LIBRARY_CACHE["data"] = data
    _LIBRARY_PERSISTENT_DIRTY = False
    _sauvegarder_cache_catalogue_persistant(data)
    return data


# ==================== ROUTES PROTEGEES ====================
@app.get("/bibliotheque")
def get_bibliotheque(request: Request, utilisateur: dict = Depends(verifier_token)):
    return _reponse_json_optimisee(request, _scanner_bibliotheque())


def _invalider_cache_bibliotheque():
    global _LIBRARY_PERSISTENT_DIRTY
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE["cached_at"] = 0.0
        _LIBRARY_CACHE["data"] = None
    _LIBRARY_PERSISTENT_DIRTY = True
    # Empêche un ancien catalogue d'être repris après un redémarrage juste après une modification admin.
    try:
        _sauvegarder_json_drive(CATALOGUE_CACHE_FILENAME, {})
    except Exception as exc:
        print(f"[DTS] invalidation du cache catalogue Drive impossible: {exc}")


@app.post("/admin/rafraichir-bibliotheque")
def rafraichir_bibliotheque(admin: str = Depends(verifier_admin)):
    _invalider_cache_bibliotheque()
    cache_tmdb.clear()
    # Les modifications manuelles restent intactes et sont réappliquées après TMDB.
    data = _scanner_bibliotheque(force=True)
    _journaliser_admin(admin, "Catalogue actualisé", {"generated_at": data.get("generated_at")})
    _assurer_sauvegarde_quotidienne()
    audio_job = None
    if AUTO_AUDIO_PREPARE:
        audio_job = _demarrer_preparation_audio(admin="SYSTEM", force=False)
    return {
        "message": "Bibliothèque actualisée",
        "generated_at": data.get("generated_at"),
        "audio_preparation_started": bool(audio_job and audio_job.get("running")),
    }


def _categorie_catalogue_valide(categorie: str) -> str:
    valeur = str(categorie or "").strip().lower()
    aliases = {"film": "films", "films": "films", "serie": "series", "série": "series", "series": "series", "anime": "anime", "animes": "anime"}
    normalisee = aliases.get(valeur)
    if not normalisee:
        raise HTTPException(status_code=400, detail="Catégorie invalide")
    return normalisee


def _trouver_contenu_catalogue(categorie: str, contenu_id: str) -> Optional[dict]:
    bibliotheque = _scanner_bibliotheque()
    return next((item for item in bibliotheque.get(categorie, []) if str(item.get("contenu_id")) == str(contenu_id)), None)


def _valider_url_metadata(url: Any, champ: str) -> str:
    texte = str(url or "").strip()
    if texte and not re.match(r"^https?://", texte, flags=re.I):
        raise HTTPException(status_code=400, detail=f"{champ} doit commencer par http:// ou https://")
    return texte


@app.patch("/admin/catalogue/{categorie}/{contenu_id}")
def modifier_metadata_contenu(
    categorie: str,
    contenu_id: str,
    data: MiseAJourMetadataContenu,
    admin: str = Depends(verifier_admin),
):
    categorie = _categorie_catalogue_valide(categorie)
    contenu = _trouver_contenu_catalogue(categorie, contenu_id)
    if not contenu:
        raise HTTPException(status_code=404, detail="Contenu introuvable dans le catalogue")

    brut = data.model_dump(exclude_unset=True) if hasattr(data, "model_dump") else data.dict(exclude_unset=True)
    if "titre" in brut and not str(brut.get("titre") or "").strip():
        raise HTTPException(status_code=400, detail="Le titre affiché est obligatoire")
    # Le formulaire envoie tous les champs : les valeurs vides sont donc des choix manuels valides.
    for champ in ("poster_url", "backdrop_url", "bande_annonce_url"):
        if champ in brut:
            brut[champ] = _valider_url_metadata(brut.get(champ), champ)
    brut["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    override = _normaliser_override_metadata(brut)

    metadata = charger_metadata_catalogue(force=True)
    metadata[_cle_metadata(categorie, contenu_id)] = override
    sauvegarder_metadata_catalogue(metadata)
    _invalider_cache_bibliotheque()
    _journaliser_admin(admin, "Informations de contenu modifiées", {"categorie": categorie, "contenu_id": contenu_id, "titre": override.get("titre")})
    _assurer_sauvegarde_quotidienne()
    return {
        "message": "Informations du contenu enregistrées",
        "categorie": categorie,
        "contenu_id": contenu_id,
        "metadata": override,
    }


@app.delete("/admin/catalogue/{categorie}/{contenu_id}")
def reinitialiser_metadata_contenu(
    categorie: str,
    contenu_id: str,
    admin: str = Depends(verifier_admin),
):
    categorie = _categorie_catalogue_valide(categorie)
    metadata = charger_metadata_catalogue(force=True)
    cle = _cle_metadata(categorie, contenu_id)
    existait = cle in metadata
    if existait:
        del metadata[cle]
        sauvegarder_metadata_catalogue(metadata)
    _invalider_cache_bibliotheque()
    if existait:
        _journaliser_admin(admin, "Informations automatiques restaurées", {"categorie": categorie, "contenu_id": contenu_id})
    return {
        "message": "Informations automatiques restaurées" if existait else "Aucune modification manuelle à supprimer",
        "removed": existait,
    }



@app.get("/sorties")
def liste_sorties_publiques(utilisateur: dict = Depends(verifier_token)):
    return {"sorties": charger_sorties()}


@app.get("/admin/sorties")
def liste_sorties_admin(admin: str = Depends(verifier_admin)):
    return {"sorties": charger_sorties(force=True)}


@app.post("/admin/sorties")
def ajouter_sortie(data: SortiePlanifiee, admin: str = Depends(verifier_admin)):
    titre = data.titre.strip()
    date = data.date.strip()
    if not titre or not date:
        raise HTTPException(status_code=400, detail="Titre et date obligatoires")
    sorties = charger_sorties(force=True)
    maintenant = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    item = {
        "id": hashlib.sha1(f"{time.time_ns()}|{titre}|{date}".encode()).hexdigest()[:12],
        "titre": titre[:240], "date": date[:40],
        "description": str(data.description or "").strip()[:1500],
        "categorie": str(data.categorie or "").strip()[:30],
        "contenu_id": str(data.contenu_id or "").strip()[:200],
        "created_at": maintenant, "updated_at": maintenant,
    }
    sorties.append(item)
    sorties.sort(key=lambda x: x.get("date") or "")
    _sauvegarder_json_drive(SORTIES_FILENAME, sorties)
    _invalider_cache_bibliotheque()
    _journaliser_admin(admin, "Sortie planifiée", {"titre": titre, "date": date})
    return {"message": "Sortie ajoutée", "sortie": item}


@app.delete("/admin/sorties/{sortie_id}")
def supprimer_sortie(sortie_id: str, admin: str = Depends(verifier_admin)):
    sorties = charger_sorties(force=True)
    apres = [item for item in sorties if str(item.get("id")) != str(sortie_id)]
    if len(apres) == len(sorties):
        raise HTTPException(status_code=404, detail="Sortie introuvable")
    _sauvegarder_json_drive(SORTIES_FILENAME, apres)
    _invalider_cache_bibliotheque()
    _journaliser_admin(admin, "Sortie planifiée supprimée", {"id": sortie_id})
    return {"message": "Sortie supprimée"}


@app.get("/admin/journal")
def journal_admin(admin: str = Depends(verifier_admin)):
    logs = _charger_json_drive(ADMIN_LOGS_FILENAME, [], force=True)
    return {"logs": logs[:500] if isinstance(logs, list) else []}


@app.post("/admin/sauvegarde")
def sauvegarde_admin(admin: str = Depends(verifier_admin)):
    return {"message": "Sauvegarde créée", "backup": creer_sauvegarde_drive(admin, automatique=False)}


_ALLOWED_POSTER_MIME = {"image/jpeg", "image/png", "image/webp", "image/avif"}
_ALLOWED_POSTER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


@app.post("/admin/catalogue/creer")
async def creer_contenu_catalogue(
    categorie: str = Form(...),
    titre: str = Form(...),
    synopsis: str = Form(""),
    annee: str = Form(""),
    genres: str = Form(""),
    statut: str = Form("Automatique"),
    saga: str = Form(""),
    ordre_saga: str = Form(""),
    bande_annonce_url: str = Form(""),
    prochaine_sortie: str = Form(""),
    poster: Optional[UploadFile] = File(None),
    admin: str = Depends(verifier_admin),
):
    categorie = _categorie_catalogue_valide(categorie)
    titre = str(titre or "").strip()
    if not titre:
        raise HTTPException(status_code=400, detail="Le titre est obligatoire")
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise HTTPException(status_code=404, detail="Dossier racine 'Danatrap Stream' introuvable sur Google Drive")
    noms = {"films": ["Films", "FILMS"], "series": ["Séries", "Series"], "anime": ["Animes", "Anime"]}[categorie]
    parent_id = trouver_dossier_flexible(service, noms, racine_id)
    if not parent_id:
        parent_id = _trouver_ou_creer_dossier(service, racine_id, noms[0])
    # Refuse un doublon exact dans la même catégorie.
    existants = lister_contenu(service, parent_id)
    if any(normaliser_nom(f.get("name") or "") == normaliser_nom(titre) and f.get("mimeType") == 'application/vnd.google-apps.folder' for f in existants):
        raise HTTPException(status_code=409, detail="Un dossier portant ce titre existe déjà dans cette catégorie")
    dossier_id = service.files().create(
        body={'name': titre, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}, fields='id'
    ).execute()['id']
    if categorie in {"series", "anime"}:
        _trouver_ou_creer_dossier(service, dossier_id, "Saison 1")
    poster_id = None
    if poster and poster.filename:
        ext = pathlib.Path(poster.filename).suffix.lower()
        mime = str(poster.content_type or "").lower()
        if ext not in _ALLOWED_POSTER_EXT or mime not in _ALLOWED_POSTER_MIME:
            raise HTTPException(status_code=400, detail="Affiche invalide. Utilise PNG, JPG, WEBP ou AVIF.")
        contenu = await poster.read()
        if len(contenu) > 15 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="L'affiche dépasse 15 Mo")
        media = MediaIoBaseUpload(io.BytesIO(contenu), mimetype=mime, resumable=False)
        poster_id = service.files().create(
            body={'name': 'icon' + ext, 'parents': [dossier_id]}, media_body=media, fields='id'
        ).execute().get('id')
    infos_tmdb = rechercher_tmdb(titre, "film" if categorie == "films" else "tv")
    override_data = {
        "titre": titre,
        "synopsis": str(synopsis or "").strip() or str(infos_tmdb.get("synopsis") or ""),
        "annee": str(annee or "").strip() or str(infos_tmdb.get("annee") or ""),
        "genres": _normaliser_liste_metadata(genres) or infos_tmdb.get("genres") or [],
        "statut": statut or "Automatique", "saga": saga, "ordre_saga": ordre_saga,
        "bande_annonce_url": _valider_url_metadata(bande_annonce_url, "bande_annonce_url"),
        "prochaine_sortie": prochaine_sortie,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    metadata = charger_metadata_catalogue(force=True)
    metadata[_cle_metadata(categorie, dossier_id)] = _normaliser_override_metadata(override_data)
    sauvegarder_metadata_catalogue(metadata)
    _invalider_cache_bibliotheque()
    _journaliser_admin(admin, "Contenu créé", {"categorie": categorie, "titre": titre, "dossier_id": dossier_id, "poster_id": poster_id})
    _assurer_sauvegarde_quotidienne()
    return {"message": "Contenu créé dans Google Drive", "categorie": categorie, "contenu_id": dossier_id, "poster_id": poster_id}


_ANALYSE_LOCK = threading.Lock()
_ANALYSE_JOB: dict[str, Any] = {"running": False, "progress": 0, "total": 0, "issues": [], "started_at": None, "finished_at": None}


def _ajouter_anomalie(issues: list, niveau: str, type_anomalie: str, titre: str, details: str, video_id: Optional[str] = None):
    issues.append({"niveau": niveau, "type": type_anomalie, "titre": titre, "details": details, "video_id": video_id})


def _executer_analyse_catalogue(profonde: bool = True):
    global _ANALYSE_JOB
    try:
        bibliotheque = _scanner_bibliotheque(force=True)
        videos = []
        issues = []
        vus_titres: dict[str, str] = {}
        for categorie in ("films", "series", "anime"):
            for item in bibliotheque.get(categorie, []):
                titre = str(item.get("titre") or "Sans titre")
                cle = _normaliser_titre_comparaison(titre)
                if cle in vus_titres:
                    _ajouter_anomalie(issues, "attention", "doublon", titre, f"Titre proche de « {vus_titres[cle]} »")
                else:
                    vus_titres[cle] = titre
                if not item.get("poster_id") and not item.get("poster_url") and not item.get("poster_tmdb_url"):
                    _ajouter_anomalie(issues, "attention", "affiche", titre, "Aucune affiche détectée")
                if not str(item.get("synopsis") or "").strip():
                    _ajouter_anomalie(issues, "attention", "description", titre, "Description absente")
                if categorie == "films":
                    if not item.get("video_id"):
                        _ajouter_anomalie(issues, "erreur", "video_absente", titre, "Aucun fichier vidéo dans le dossier")
                    else:
                        videos.append((titre, item.get("video_id"), item.get("duree_drive")))
                else:
                    eps = [ep for saison in item.get("saisons") or [] for ep in saison.get("episodes") or []]
                    if not eps:
                        _ajouter_anomalie(issues, "attention", "episodes", titre, "Aucun épisode importé")
                    for ep in eps:
                        videos.append((titre + " — " + str(ep.get("nom") or "Épisode"), ep.get("video_id"), ep.get("duree")))
        with _ANALYSE_LOCK:
            _ANALYSE_JOB.update({"total": len(videos), "issues": issues, "progress": 0})
        if profonde and shutil.which("ffprobe"):
            for index, (titre, video_id, duree_drive) in enumerate(videos):
                try:
                    info = _probe_media(str(video_id), force=False)
                    if not info.get("audio"):
                        _ajouter_anomalie(issues, "erreur", "audio", titre, "Aucune piste audio détectée", video_id)
                    if float(info.get("duration") or 0) <= 1:
                        _ajouter_anomalie(issues, "erreur", "duree", titre, "Durée vidéo invalide", video_id)
                    for sub in info.get("subtitles") or []:
                        if sub.get("codec") in _IMAGE_SUBTITLE_CODECS:
                            _ajouter_anomalie(issues, "info", "sous_titre_image", titre, f"Sous-titre {sub.get('title')} en image, non affichable dans le lecteur web", video_id)
                except Exception as exc:
                    _ajouter_anomalie(issues, "erreur", "inaccessible", titre, str(exc)[:500], video_id)
                with _ANALYSE_LOCK:
                    _ANALYSE_JOB["progress"] = index + 1
                    _ANALYSE_JOB["issues"] = list(issues)
        with _ANALYSE_LOCK:
            _ANALYSE_JOB.update({"running": False, "issues": issues, "progress": len(videos), "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"})
    except Exception as exc:
        with _ANALYSE_LOCK:
            _ANALYSE_JOB.update({"running": False, "error": str(exc), "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z"})


@app.post("/admin/analyse-catalogue")
def lancer_analyse_catalogue(profonde: bool = Query(True), admin: str = Depends(verifier_admin)):
    global _ANALYSE_JOB
    with _ANALYSE_LOCK:
        if _ANALYSE_JOB.get("running"):
            return dict(_ANALYSE_JOB)
        _ANALYSE_JOB = {"running": True, "progress": 0, "total": 0, "issues": [], "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z", "finished_at": None, "error": None}
    threading.Thread(target=_executer_analyse_catalogue, args=(profonde,), daemon=True).start()
    _journaliser_admin(admin, "Analyse du catalogue lancée", {"profonde": profonde})
    return dict(_ANALYSE_JOB)


@app.get("/admin/analyse-catalogue")
def statut_analyse_catalogue(admin: str = Depends(verifier_admin)):
    with _ANALYSE_LOCK:
        return json.loads(json.dumps(_ANALYSE_JOB, ensure_ascii=False))


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
        "audio_cache_drive": sum(1 for v in _charger_audio_mapping(force=False).values() if isinstance(v, dict) and v.get("file_id")),
        "audio_job_running": bool(_audio_job_snapshot().get("running")),
        "bibliotheque_generee": bibliotheque.get("generated_at"),
        "sorties_planifiees": len(bibliotheque.get("sorties") or []),
        "collections": len(bibliotheque.get("collections") or []),
        "en_cours": sum(1 for c in ("films", "series", "anime") for i in bibliotheque.get(c, []) if i.get("statut") == "En cours"),
        "termines": sum(1 for c in ("films", "series", "anime") for i in bibliotheque.get(c, []) if i.get("statut") == "Terminé"),
        "abandonnes": sum(1 for c in ("films", "series", "anime") for i in bibliotheque.get(c, []) if i.get("statut") == "Abandonné"),
    }


@app.get("/poster/{poster_id}")
def poster_optimise(
    poster_id: str,
    request: Request,
    token: Optional[str] = Query(None),
):
    token_final = token
    if not token_final:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token_final = auth_header[7:]
    if not token_final:
        raise HTTPException(status_code=401, detail="Token manquant")
    try:
        jwt.decode(token_final, CLE_SECRETE_JWT, algorithms=[ALGORITHME])
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expiré")
    try:
        chemin, media_type = _miniature_poster(poster_id)
    except Exception as exc:
        print(f"[DTS] miniature impossible pour {poster_id}: {exc}")
        raise HTTPException(status_code=502, detail="Affiche temporairement indisponible")
    etag = '"poster-' + hashlib.sha1((poster_id + str(chemin.stat().st_size)).encode()).hexdigest()[:24] + '"'
    headers = {
        "Cache-Control": "private, max-age=604800, stale-while-revalidate=2592000",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(str(chemin), media_type=media_type, headers=headers)


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _audio_cache_key(video_id: str, track_index: int) -> str:
    return f"{video_id}:{int(track_index)}"


def _audio_conversion_lock(video_id: str, track_index: int) -> threading.Lock:
    key = _audio_cache_key(video_id, track_index)
    with _AUDIO_CONVERSION_LOCKS_GUARD:
        return _AUDIO_CONVERSION_LOCKS.setdefault(key, threading.Lock())


def _audio_compatibility(track: dict) -> dict:
    """Détermine si une piste peut rester telle quelle dans tous les navigateurs.

    Même une piste marquée compatible peut être normalisée à la volée par /mux
    afin de réparer ses timestamps. Les pistes non compatibles sont en plus
    préparées et conservées sur Drive pour les lectures suivantes.
    """
    codec = str(track.get("codec") or "").strip().lower()
    profile = str(track.get("profile") or "").strip().lower()
    sample_rate = _safe_int(track.get("sample_rate"))
    channels = _safe_int(track.get("channels"))
    reasons = []
    if codec != "aac":
        reasons.append(f"codec {codec.upper() or 'inconnu'}")
    if codec == "aac" and any(tag in profile for tag in ("he-aac", "he_aac", "sbr", "aac he")):
        reasons.append("profil HE-AAC")
    if channels > 2:
        reasons.append(f"{channels} canaux")
    if sample_rate and sample_rate not in {44100, 48000}:
        reasons.append(f"{sample_rate} Hz")
    if channels <= 0:
        reasons.append("nombre de canaux inconnu")
    needs_conversion = bool(reasons)
    return {
        "needs_conversion": needs_conversion,
        "browser_compatible": not needs_conversion,
        "compatibility_reason": ", ".join(reasons) if reasons else "AAC compatible",
        "target_codec": "AAC-LC",
        "target_channels": 2,
        "target_sample_rate": 48000,
        "target_bitrate": AUDIO_CACHE_BITRATE,
    }


def _audio_track_fingerprint(video_id: str, track: dict, media: dict) -> str:
    payload = {
        "video_id": str(video_id),
        "track_index": _safe_int(track.get("index"), -1),
        "stream_index": _safe_int(track.get("stream_index"), -1),
        "codec": str(track.get("codec") or ""),
        "profile": str(track.get("profile") or ""),
        "sample_rate": _safe_int(track.get("sample_rate")),
        "channels": _safe_int(track.get("channels")),
        "channel_layout": str(track.get("channel_layout") or ""),
        "start_time": round(_safe_float(track.get("start_time")), 6),
        "duration": round(_safe_float(media.get("duration")), 3),
        "video_start_time": round(_safe_float(media.get("video_start_time")), 6),
        "target": f"aac-lc|stereo|48000|{AUDIO_CACHE_BITRATE}",
    }
    brut = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(brut).hexdigest()[:24]


def _charger_audio_mapping(force: bool = False) -> dict:
    data = _charger_json_drive(AUDIO_COMPATIBILITY_FILENAME, {}, force=force)
    return data if isinstance(data, dict) else {}


def _audio_cache_entry(video_id: str, track: dict, media: dict, mapping: Optional[dict] = None) -> Optional[dict]:
    mapping = mapping if isinstance(mapping, dict) else _charger_audio_mapping(force=False)
    entry = mapping.get(_audio_cache_key(video_id, _safe_int(track.get("index"), -1)))
    if not isinstance(entry, dict) or not entry.get("file_id"):
        return None
    if str(entry.get("fingerprint") or "") != _audio_track_fingerprint(video_id, track, media):
        return None
    return entry


def _annoter_pistes_audio(video_id: str, media: dict) -> dict:
    resultat = json.loads(json.dumps(media, ensure_ascii=False))
    try:
        mapping = _charger_audio_mapping(force=False)
    except Exception:
        mapping = {}
    for track in resultat.get("audio") or []:
        compat = _audio_compatibility(track)
        entry = _audio_cache_entry(video_id, track, resultat, mapping)
        track.update(compat)
        track["cache_ready"] = bool(entry)
        track["cache_status"] = "optimisee" if entry else ("a_preparer" if compat["needs_conversion"] else "compatible")
        track["cache_label"] = "Optimisée sur Drive" if entry else ("Conversion automatique" if compat["needs_conversion"] else "Compatible")
    return resultat


def _audio_alignment_filter(media: dict, track: dict) -> str:
    """Crée une piste alignée sur le début de la vidéo avant son cache Drive."""
    video_start = _safe_float(media.get("video_start_time"), _safe_float(media.get("format_start_time"), 0.0))
    audio_start = _safe_float(track.get("start_time"), video_start)
    delta = audio_start - video_start
    filters = []
    if delta > 0.025:
        filters.append(f"adelay={max(0, int(round(delta * 1000)))}:all=1")
    elif delta < -0.025:
        filters.extend([f"atrim=start={abs(delta):.6f}", "asetpts=PTS-STARTPTS"])
    filters.append("aresample=48000:async=1000:min_hard_comp=0.100:first_pts=0")
    return ",".join(filters)


def _audio_universal_codec_args(filter_chain: Optional[str] = None) -> list[str]:
    filtre = filter_chain or "aresample=48000:async=1000:min_hard_comp=0.100:first_pts=0"
    return [
        "-af", filtre,
        "-c:a", "aac", "-profile:a", "aac_low",
        "-b:a", AUDIO_CACHE_BITRATE, "-ar", "48000", "-ac", "2",
    ]


_TRACKS_CACHE: dict[str, dict] = {}
_TRACKS_CACHE_TTL = int(os.environ.get("TRACKS_CACHE_TTL", "3600"))
_TRACKS_CACHE_LOCK = threading.Lock()
_MEDIA_PROBE_LOCKS: dict[str, threading.Lock] = {}
_MEDIA_PROBE_LOCKS_GUARD = threading.Lock()


def _media_probe_lock(video_id: str) -> threading.Lock:
    """Évite deux analyses ffprobe simultanées du même fichier Drive."""
    with _MEDIA_PROBE_LOCKS_GUARD:
        return _MEDIA_PROBE_LOCKS.setdefault(str(video_id), threading.Lock())


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
    """Analyse les flux d'un média Drive avec reprise automatique.

    Google Drive peut répondre temporairement en 429/5xx, et certains gros
    conteneurs mettent plus de 75 secondes à exposer leurs pistes. Le probe est
    donc verrouillé par fichier, retenté et doté d'un délai plus réaliste.
    """
    now = time.time()
    with _TRACKS_CACHE_LOCK:
        cached = _TRACKS_CACHE.get(video_id)
        if not force and cached and now - cached["cached_at"] < _TRACKS_CACHE_TTL:
            return cached["data"]

    lock = _media_probe_lock(video_id)
    with lock:
        # Une autre requête a peut-être terminé pendant l'attente du verrou.
        now = time.time()
        with _TRACKS_CACHE_LOCK:
            cached = _TRACKS_CACHE.get(video_id)
            if not force and cached and now - cached["cached_at"] < _TRACKS_CACHE_TTL:
                return cached["data"]

        last_error = ""
        result = None
        for attempt in range(1, AUDIO_PROBE_RETRIES + 1):
            cmd = [
                "ffprobe",
                "-headers", _get_drive_auth_header(),
                "-rw_timeout", "60000000",
                "-reconnect", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "429,500,502,503,504",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "8",
                "-v", "error",
                "-show_entries",
                "format=start_time,duration:stream=index,codec_name,codec_long_name,profile,codec_type,width,height,sample_rate,channels,channel_layout,bit_rate,start_time,duration:stream_disposition=default,attached_pic:stream_tags=language,title,handler_name",
                "-of", "json",
                _get_drive_media_url(video_id),
            ]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=AUDIO_PROBE_TIMEOUT,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("ffprobe n'est pas installé sur le serveur Render") from exc
            except subprocess.TimeoutExpired:
                last_error = f"L'analyse des pistes a dépassé {AUDIO_PROBE_TIMEOUT} secondes"
                result = None
            else:
                if result.returncode == 0:
                    break
                last_error = (result.stderr or "Erreur ffprobe inconnue").strip()[-1200:]

            if attempt < AUDIO_PROBE_RETRIES:
                # Le jeton peut expirer pendant une longue campagne d'analyse.
                try:
                    if CREDENTIALS is not None and (not CREDENTIALS.valid or CREDENTIALS.expired):
                        CREDENTIALS.refresh(GoogleRequest())
                except Exception:
                    pass
                time.sleep(AUDIO_PROBE_RETRY_DELAY * attempt)

        if result is None or result.returncode != 0:
            message = last_error or "Erreur ffprobe inconnue"
            if "4XX Client Error" in message or "429" in message:
                message = "Google Drive a temporairement limité l'accès au fichier (HTTP 429/4XX). Réessaie dans quelques minutes. Détail : " + message
            raise RuntimeError(f"ffprobe a échoué après {AUDIO_PROBE_RETRIES} tentative(s): {message}")

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
                "codec_long_name": stream.get("codec_long_name", ""),
                "profile": stream.get("profile", ""),
                "sample_rate": _safe_int(stream.get("sample_rate")),
                "channels": _safe_int(stream.get("channels")),
                "channel_layout": stream.get("channel_layout", ""),
                "bit_rate": _safe_int(stream.get("bit_rate")),
                "start_time": _safe_float(stream.get("start_time")),
                "stream_duration": _safe_float(stream.get("duration")),
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
        format_start_time = _safe_float((probe_data.get("format") or {}).get("start_time"), 0.0)
        video_start_time = _safe_float(primary_video.get("start_time"), format_start_time)
        data = {
            "audio": audio,
            "subtitles": subtitles,
            "duration": duration,
            "format_start_time": format_start_time,
            "video_start_time": video_start_time,
            "video_stream_index": primary_video.get("index"),
            "video_codec": primary_video.get("codec_name", ""),
            "width": largeur,
            "height": hauteur,
            "quality": qualite,
        }
        with _TRACKS_CACHE_LOCK:
            _TRACKS_CACHE[video_id] = {"cached_at": now, "data": data}
        return data

def _enumerer_videos_audio(bibliotheque: Optional[dict] = None) -> list[dict]:
    bibliotheque = bibliotheque if isinstance(bibliotheque, dict) else _scanner_bibliotheque()
    videos: list[dict] = []
    vus: set[str] = set()
    for item in bibliotheque.get("films") or []:
        video_id = str(item.get("video_id") or "").strip()
        if video_id and video_id not in vus:
            vus.add(video_id)
            videos.append({"video_id": video_id, "titre": str(item.get("titre") or "Film")})
    for categorie in ("series", "anime"):
        for item in bibliotheque.get(categorie) or []:
            titre_serie = str(item.get("titre") or ("Anime" if categorie == "anime" else "Série"))
            for saison in item.get("saisons") or []:
                saison_nom = str(saison.get("nom") or "")
                for ep in saison.get("episodes") or []:
                    video_id = str(ep.get("video_id") or "").strip()
                    if not video_id or video_id in vus:
                        continue
                    vus.add(video_id)
                    ep_nom = str(ep.get("nom") or "Épisode")
                    suffixe = " — ".join(v for v in (saison_nom, ep_nom) if v)
                    videos.append({"video_id": video_id, "titre": titre_serie + (" — " + suffixe if suffixe else "")})
    return videos


def _audio_cache_filename(video_id: str, track_index: int, fingerprint: str, language: str = "") -> str:
    lang = re.sub(r"[^A-Za-z0-9_-]", "", str(language or "").lower())[:12] or "und"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_id))[:80]
    return f"{safe_id}_audio_{int(track_index)}_{lang}_{fingerprint[:12]}.m4a"


def _upload_audio_cache_drive(local_path: pathlib.Path, filename: str, previous_file_id: Optional[str] = None) -> dict:
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    if not racine_id:
        raise RuntimeError("Dossier racine 'Danatrap Stream' introuvable sur Google Drive")
    parent_cache = _trouver_ou_creer_dossier(service, racine_id, AUDIO_CACHE_PARENT_FOLDER)
    audio_folder = _trouver_ou_creer_dossier(service, parent_cache, AUDIO_CACHE_FOLDER_NAME)
    upload = MediaFileUpload(
        str(local_path), mimetype="audio/mp4", resumable=True, chunksize=8 * 1024 * 1024
    )
    resultat = None
    if previous_file_id:
        try:
            resultat = service.files().update(
                fileId=str(previous_file_id), body={"name": filename}, media_body=upload, fields="id,name,size"
            ).execute()
        except Exception as exc:
            print(f"[DTS] ancien cache audio inutilisable, nouveau fichier créé: {exc}")
            upload = MediaFileUpload(str(local_path), mimetype="audio/mp4", resumable=True, chunksize=8 * 1024 * 1024)
    if not resultat:
        resultat = service.files().create(
            body={"name": filename, "parents": [audio_folder]},
            media_body=upload, fields="id,name,size"
        ).execute()
    return resultat or {}


def _preparer_piste_audio_compatible(video_id: str, track_index: int, titre: str = "", force: bool = False) -> dict:
    track_index = int(track_index)
    lock = _audio_conversion_lock(video_id, track_index)
    with lock, _AUDIO_CONVERSION_SEMAPHORE:
        media = _probe_media(video_id, force=False)
        tracks = media.get("audio") or []
        if track_index < 0 or track_index >= len(tracks):
            raise RuntimeError("Piste audio introuvable")
        selected = tracks[track_index]
        fingerprint = _audio_track_fingerprint(video_id, selected, media)
        with _AUDIO_MAPPING_LOCK:
            mapping = _charger_audio_mapping(force=True)
            existing = mapping.get(_audio_cache_key(video_id, track_index)) if isinstance(mapping, dict) else None
        if not force and isinstance(existing, dict) and existing.get("file_id") and existing.get("fingerprint") == fingerprint:
            return dict(existing)

        filename = _audio_cache_filename(video_id, track_index, fingerprint, selected.get("language") or "")
        local_path = AUDIO_CACHE_LOCAL_DIR / filename
        try:
            local_path.unlink(missing_ok=True)
        except Exception:
            pass
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
            "-headers", _get_drive_auth_header(),
            "-i", _get_drive_media_url(video_id),
            "-map", f"0:{selected.get('stream_index')}",
            "-vn", "-sn", "-dn",
            *_audio_universal_codec_args(_audio_alignment_filter(media, selected)),
            "-map_metadata", "-1",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(local_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg n'est pas installé sur Render") from exc
        if result.returncode != 0 or not local_path.exists() or local_path.stat().st_size <= 1024:
            error = result.stderr.decode("utf-8", errors="replace").strip()[-1600:]
            try:
                local_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(error or "Conversion audio impossible")

        previous_id = existing.get("file_id") if isinstance(existing, dict) else None
        try:
            fichier = _upload_audio_cache_drive(local_path, filename, previous_file_id=previous_id)
            file_id = str(fichier.get("id") or "")
            if not file_id:
                raise RuntimeError("Google Drive n'a pas renvoyé l'identifiant du cache audio")
            entry = {
                "file_id": file_id, "name": str(fichier.get("name") or filename),
                "size": _safe_int(fichier.get("size"), int(local_path.stat().st_size)),
                "fingerprint": fingerprint, "video_id": video_id, "track_index": track_index,
                "title": str(titre or ""), "track_title": str(selected.get("title") or f"Audio {track_index + 1}"),
                "language": str(selected.get("language") or ""), "source_codec": str(selected.get("codec") or ""),
                "source_profile": str(selected.get("profile") or ""),
                "source_channels": _safe_int(selected.get("channels")),
                "source_sample_rate": _safe_int(selected.get("sample_rate")),
                "target": f"AAC-LC stéréo 48 kHz {AUDIO_CACHE_BITRATE}",
                "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }
            with _AUDIO_MAPPING_LOCK:
                mapping = _charger_audio_mapping(force=True)
                mapping[_audio_cache_key(video_id, track_index)] = entry
                _sauvegarder_json_drive(AUDIO_COMPATIBILITY_FILENAME, mapping)
            return entry
        finally:
            try:
                local_path.unlink(missing_ok=True)
            except Exception:
                pass


def _planifier_piste_audio(video_id: str, track_index: int, titre: str = "") -> None:
    def runner():
        try:
            media = _probe_media(video_id)
            tracks = media.get("audio") or []
            if track_index < 0 or track_index >= len(tracks):
                return
            selected = tracks[track_index]
            if _audio_cache_entry(video_id, selected, media):
                return
            _preparer_piste_audio_compatible(video_id, track_index, titre=titre, force=False)
            print(f"[DTS] piste audio universelle prête: {video_id}/{track_index}")
        except Exception as exc:
            print(f"[DTS] préparation audio en arrière-plan impossible {video_id}/{track_index}: {exc}")
    threading.Thread(target=runner, daemon=True).start()


def _audio_job_snapshot() -> dict:
    with _AUDIO_PREPARE_LOCK:
        return json.loads(json.dumps(_AUDIO_PREPARE_JOB, ensure_ascii=False))


def _executer_preparation_audio(force: bool = False, admin: str = "SYSTEM", target_video_ids: Optional[set[str]] = None) -> None:
    global _AUDIO_PREPARE_JOB
    try:
        with _AUDIO_PREPARE_LOCK:
            _AUDIO_PREPARE_JOB.update({"phase": "analyse", "current": "Analyse du catalogue…"})
        bibliotheque = _scanner_bibliotheque(force=False)
        videos = _enumerer_videos_audio(bibliotheque)
        if target_video_ids:
            videos = [v for v in videos if str(v.get("video_id")) in target_video_ids]
        items = []
        pending_tasks = []
        direct = prepared = errors = tracks_total = 0
        mapping = _charger_audio_mapping(force=True)
        with _AUDIO_PREPARE_LOCK:
            _AUDIO_PREPARE_JOB["videos"] = len(videos)
        for video_pos, video in enumerate(videos, start=1):
            titre = video["titre"]
            video_id = video["video_id"]
            with _AUDIO_PREPARE_LOCK:
                _AUDIO_PREPARE_JOB["current"] = f"Analyse {video_pos}/{len(videos)} — {titre}"
            try:
                media = _probe_media(video_id, force=False)
                for track in media.get("audio") or []:
                    tracks_total += 1
                    compat = _audio_compatibility(track)
                    entry = _audio_cache_entry(video_id, track, media, mapping)
                    item = {
                        "video_id": video_id, "track_index": _safe_int(track.get("index")),
                        "titre": titre, "track": str(track.get("title") or "Audio"),
                        "codec": str(track.get("codec") or "inconnu").upper(),
                        "details": compat.get("compatibility_reason"),
                        "status": "compatible", "error": "",
                    }
                    if entry and not force:
                        item["status"] = "prepared"
                        prepared += 1
                    elif compat.get("needs_conversion") or force:
                        item["status"] = "pending"
                        pending_tasks.append((video_id, _safe_int(track.get("index")), titre, item))
                    else:
                        direct += 1
                    if item["status"] != "compatible":
                        items.append(item)
            except Exception as exc:
                errors += 1
                items.append({"video_id": video_id, "track_index": -1, "titre": titre, "track": "Analyse", "codec": "—", "details": "", "status": "error", "error": str(exc)[:800]})
            with _AUDIO_PREPARE_LOCK:
                _AUDIO_PREPARE_JOB.update({
                    "tracks": tracks_total, "direct_compatible": direct, "prepared": prepared,
                    "pending": len(pending_tasks), "errors": errors, "items": list(items[-300:]),
                })

        total = len(pending_tasks)
        with _AUDIO_PREPARE_LOCK:
            _AUDIO_PREPARE_JOB.update({"phase": "conversion", "total": total, "progress": 0, "pending": total})
        for pos, (video_id, track_index, titre, item) in enumerate(pending_tasks, start=1):
            with _AUDIO_PREPARE_LOCK:
                _AUDIO_PREPARE_JOB["current"] = f"Conversion {pos}/{total} — {titre} — {item['track']}"
            try:
                _preparer_piste_audio_compatible(video_id, track_index, titre=titre, force=force)
                item["status"] = "prepared"
                prepared += 1
            except Exception as exc:
                item["status"] = "error"
                item["error"] = str(exc)[:800]
                errors += 1
            with _AUDIO_PREPARE_LOCK:
                _AUDIO_PREPARE_JOB.update({
                    "progress": pos, "prepared": prepared, "pending": max(0, total - pos),
                    "errors": errors, "items": list(items[-300:]),
                })
        with _AUDIO_PREPARE_LOCK:
            _AUDIO_PREPARE_JOB.update({
                "running": False, "phase": "finished", "current": "",
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })
        _journaliser_admin(admin, "Compatibilité audio préparée", {
            "videos": len(videos), "tracks": tracks_total, "prepared": prepared,
            "direct_compatible": direct, "errors": errors,
        })
    except Exception as exc:
        with _AUDIO_PREPARE_LOCK:
            _AUDIO_PREPARE_JOB.update({
                "running": False, "phase": "error", "error": str(exc),
                "finished_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            })


def _demarrer_preparation_audio(admin: str = "SYSTEM", force: bool = False, retry_errors: bool = False) -> dict:
    global _AUDIO_PREPARE_JOB
    with _AUDIO_PREPARE_LOCK:
        if _AUDIO_PREPARE_JOB.get("running"):
            return json.loads(json.dumps(_AUDIO_PREPARE_JOB, ensure_ascii=False))
        target_video_ids = None
        if retry_errors:
            target_video_ids = {
                str(item.get("video_id")) for item in (_AUDIO_PREPARE_JOB.get("items") or [])
                if isinstance(item, dict) and item.get("status") == "error" and item.get("video_id")
            }
            if not target_video_ids:
                retry_errors = False
        _AUDIO_PREPARE_JOB = {
            "running": True, "phase": "starting", "progress": 0, "total": 0,
            "videos": 0, "tracks": 0, "direct_compatible": 0, "prepared": 0,
            "pending": 0, "errors": 0, "current": "Démarrage…", "items": [],
            "started_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "finished_at": None, "error": None,
            "retry_errors": retry_errors,
        }
        snapshot = json.loads(json.dumps(_AUDIO_PREPARE_JOB, ensure_ascii=False))
    threading.Thread(
        target=_executer_preparation_audio,
        args=(force, admin, target_video_ids),
        daemon=True,
    ).start()
    return snapshot


@app.get("/admin/audio-compatibility")
def statut_audio_compatibility(admin: str = Depends(verifier_admin)):
    return _audio_job_snapshot()


@app.post("/admin/audio-compatibility")
def lancer_audio_compatibility(
    force: bool = Query(False),
    retry_errors: bool = Query(False),
    admin: str = Depends(verifier_admin),
):
    job = _demarrer_preparation_audio(admin=admin, force=force, retry_errors=retry_errors)
    _journaliser_admin(admin, "Préparation audio universelle lancée", {
        "force": force, "retry_errors": retry_errors,
    })
    return job


@app.get("/tracks/{video_id}")
def get_tracks(video_id: str, utilisateur: dict = Depends(verifier_token)):
    try:
        # Important : cette route ne lance plus l'extraction complète des pistes.
        # Elle répond dès que ffprobe a identifié les langues disponibles.
        media = _probe_media(video_id)
        return _annoter_pistes_audio(video_id, media)
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
        cache_entry = _audio_cache_entry(video_id, selected, tracks)
        compatibility = _audio_compatibility(selected)
        if not cache_entry and compatibility.get("needs_conversion"):
            # La première lecture reste immédiate grâce à la conversion à la volée.
            # En parallèle, DTS prépare une version persistante pour les suivantes.
            _planifier_piste_audio(video_id, track_index, selected.get("title") or "")

        auth_header = _get_drive_auth_header()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        ]
        if coarse_seek > 0:
            cmd += ["-ss", f"{coarse_seek:.3f}"]
        cmd += ["-headers", auth_header, "-i", _get_drive_media_url(video_id)]

        if cache_entry:
            if coarse_seek > 0:
                cmd += ["-ss", f"{coarse_seek:.3f}"]
            cmd += ["-headers", auth_header, "-i", _get_drive_media_url(str(cache_entry.get("file_id")))]
        if fine_seek > 0:
            cmd += ["-ss", f"{fine_seek:.3f}"]

        cmd += ["-map", f"0:{video_stream_index}"]
        if cache_entry:
            cmd += ["-map", "1:a:0", "-c:v", "copy", "-c:a", "copy"]
        else:
            cmd += [
                "-map", f"0:{audio_stream_index}",
                "-c:v", "copy",
                *_audio_universal_codec_args(),
            ]
        cmd += [
            "-sn", "-dn",
            "-map_metadata", "-1",
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
                "X-DTS-Audio-Mode": "cache-drive" if cache_entry else "conversion-live",
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
        start_sec = max(0.0, float(start or 0.0))
        seek_args = ["-ss", f"{start_sec:.3f}"] if start_sec > 0 else []
        cache_entry = _audio_cache_entry(video_id, selected, tracks)
        if not cache_entry and _audio_compatibility(selected).get("needs_conversion"):
            _planifier_piste_audio(video_id, track_index, selected.get("title") or "")

        source_id = str(cache_entry.get("file_id")) if cache_entry else video_id
        map_audio = "0:a:0" if cache_entry else f"0:{stream_index}"
        codec_args = ["-c:a", "copy"] if cache_entry else _audio_universal_codec_args()
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
            *seek_args,
            "-headers", _get_drive_auth_header(),
            "-i", _get_drive_media_url(source_id),
            "-map", map_audio,
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
                "X-DTS-Audio-Mode": "cache-drive" if cache_entry else "conversion-live",
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
        "login", "bibliotheque", "stream", "poster", "tracks", "mux", "audio", "subtitle", "admin", "api",
        "favicon.ico", "token.pickle", "credentials.json",
        "utilisateurs.json", "static",
    }
    EXTENSIONS_BLOQUEES = {".py", ".json", ".pickle", ".log", ".env", ".txt"}
    EXTENSIONS_COMPRESSIBLES = {".html", ".css", ".js", ".svg", ".webmanifest"}

    def _servir_fichier_statique(fichier: pathlib.Path, request: Request, media_type: str, cache_control: str) -> Response:
        headers = {"Cache-Control": cache_control, "Vary": "Accept-Encoding"}
        ext = fichier.suffix.lower()
        if ext in EXTENSIONS_COMPRESSIBLES and "gzip" in request.headers.get("accept-encoding", "").lower():
            mtime = fichier.stat().st_mtime
            cle = str(fichier)
            cache = _STATIC_GZIP_CACHE.get(cle)
            if not cache or cache[0] != mtime:
                cache = (mtime, gzip.compress(fichier.read_bytes(), compresslevel=5))
                _STATIC_GZIP_CACHE[cle] = cache
            headers["Content-Encoding"] = "gzip"
            return Response(content=cache[1], media_type=media_type, headers=headers)
        return FileResponse(str(fichier), media_type=media_type, headers=headers)

    @app.get("/", include_in_schema=False)
    def _serve_index(request: Request):
        # no-cache autorise la réutilisation avec validation, contrairement à no-store qui retélécharge tout.
        return _servir_fichier_statique(
            _FRONTEND_DIR / "index.html", request, "text/html; charset=utf-8", "no-cache, must-revalidate"
        )

    @app.get("/{filename:path}", include_in_schema=False)
    def _serve_static(filename: str, request: Request):
        if ".." in filename or filename.startswith("/") or "\\" in filename:
            raise HTTPException(status_code=400, detail="Chemin invalide")
        premier = filename.split("/")[0].lower()
        if premier in ROUTES_PROTEGEES:
            raise HTTPException(status_code=404, detail="Route inconnue")
        ext = os.path.splitext(filename)[1].lower()
        if ext in EXTENSIONS_BLOQUEES:
            raise HTTPException(status_code=403, detail="Type de fichier interdit")
        fichier = _FRONTEND_DIR / filename
        if not fichier.is_file():
            raise HTTPException(status_code=404, detail="Fichier introuvable")
        mime, _ = mimetypes.guess_type(str(fichier))
        mime = mime or "application/octet-stream"
        if ext in {".png", ".jpg", ".jpeg", ".webp", ".avif", ".ico"}:
            cache_control = "public, max-age=2592000, stale-while-revalidate=2592000"
        else:
            cache_control = "public, max-age=604800, stale-while-revalidate=86400"
        return _servir_fichier_statique(fichier, request, mime, cache_control)
    print(f"Frontend servi depuis {_FRONTEND_DIR}")
else:
    print(f"ATTENTION: dossier frontend introuvable a {_FRONTEND_DIR}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
