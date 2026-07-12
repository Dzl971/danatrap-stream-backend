from fastapi import FastAPI, HTTPException, Depends, Request, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from pydantic import BaseModel
from jose import jwt, JWTError
from datetime import datetime, timedelta
from typing import Optional
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

SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file'
]
CLE_SECRETE_JWT = os.environ.get('CLE_SECRETE_JWT', 'change-moi-en-production')
ALGORITHME = "HS256"
DUREE_TOKEN_JOURS = 30
ADMIN_PSEUDO = "Dzl 971"

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

def charger_utilisateurs():
    if CREDENTIALS is None:
        _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    fichier_id = trouver_fichier_utilisateurs(service, racine_id)
    if not fichier_id:
        return {}
    access_token = get_access_token()
    url = f"https://www.googleapis.com/drive/v3/files/{fichier_id}?alt=media"
    reponse = requests.get(url, headers={'Authorization': f'Bearer {access_token}'})
    return reponse.json()

def sauvegarder_utilisateurs(utilisateurs):
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = trouver_dossier_racine(service)
    fichier_id = trouver_fichier_utilisateurs(service, racine_id)
    contenu_json = json.dumps(utilisateurs, indent=2, ensure_ascii=False)
    media = MediaIoBaseUpload(io.BytesIO(contenu_json.encode('utf-8')), mimetype='application/json')
    if fichier_id:
        service.files().update(fileId=fichier_id, media_body=media).execute()
    else:
        metadata = {'name': 'utilisateurs.json', 'parents': [racine_id]}
        service.files().create(body=metadata, media_body=media, fields='id').execute()

def verifier_mot_de_passe(mot_de_passe_clair, mot_de_passe_hash):
    return bcrypt.checkpw(mot_de_passe_clair.encode('utf-8'), mot_de_passe_hash.encode('utf-8'))

def creer_token(pseudo):
    expiration = datetime.utcnow() + timedelta(days=DUREE_TOKEN_JOURS)
    donnees = {"sub": pseudo, "exp": expiration, "est_admin": pseudo == ADMIN_PSEUDO}
    return jwt.encode(donnees, CLE_SECRETE_JWT, algorithm=ALGORITHME)

security = HTTPBearer()

def verifier_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, CLE_SECRETE_JWT, algorithms=[ALGORITHME])
        pseudo = payload.get("sub")
        if pseudo is None:
            raise HTTPException(status_code=401, detail="Token invalide")
        return {"pseudo": pseudo, "est_admin": payload.get("est_admin", False)}
    except JWTError:
        raise HTTPException(status_code=401, detail="Token invalide ou expire")

def verifier_admin(utilisateur: dict = Depends(verifier_token)):
    if not utilisateur.get("est_admin") and utilisateur.get("pseudo") != ADMIN_PSEUDO:
        raise HTTPException(status_code=403, detail="Acces reserve a l'administrateur")
    return utilisateur["pseudo"]

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

@app.post("/logi
