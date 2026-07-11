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

@app.post("/login")
def login(data: LoginRequest):
    import traceback
    try:
        utilisateurs = charger_utilisateurs()
        utilisateur = utilisateurs.get(data.pseudo)
        if not utilisateur or not verifier_mot_de_passe(data.mot_de_passe, utilisateur["mot_de_passe_hash"]):
            raise HTTPException(status_code=401, detail="Pseudo ou mot de passe incorrect")
        token = creer_token(data.pseudo)
        est_admin = (data.pseudo == ADMIN_PSEUDO)
        return {"access_token": token, "pseudo": data.pseudo, "est_admin": est_admin}
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc()
        with open("server.log", "a", encoding="utf-8") as f:
            f.write("\n\n=== " + datetime.now().isoformat() + " ===\n" + tb)
        raise HTTPException(status_code=500, detail=f"Erreur: {type(e).__name__}: {e}")

@app.get("/me")
def me(utilisateur: dict = Depends(verifier_token)):
    return {"pseudo": utilisateur["pseudo"], "est_admin": utilisateur.get("est_admin", False)}

class NouvelUtilisateur(BaseModel):
    pseudo: str
    mot_de_passe: str

@app.post("/admin/ajouter-utilisateur")
def ajouter_utilisateur(data: NouvelUtilisateur, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs()
    mot_de_passe_hash = bcrypt.hashpw(data.mot_de_passe.encode('utf-8'), bcrypt.gensalt())
    utilisateurs[data.pseudo] = {"mot_de_passe_hash": mot_de_passe_hash.decode('utf-8')}
    sauvegarder_utilisateurs(utilisateurs)
    return {"message": f"Utilisateur '{data.pseudo}' ajoute avec succes"}

@app.get("/admin/liste-utilisateurs")
def liste_utilisateurs(admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs()
    return {"utilisateurs": list(utilisateurs.keys())}

@app.delete("/admin/supprimer-utilisateur/{pseudo}")
def supprimer_utilisateur(pseudo: str, admin: str = Depends(verifier_admin)):
    utilisateurs = charger_utilisateurs()
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
    resultats = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    return resultats.get('files', [])

def rechercher_tmdb(titre, type_contenu="film"):
    cle_cache = f"{type_contenu}:{titre.lower()}"
    if cle_cache in cache_tmdb:
        return cache_tmdb[cle_cache]
    if not TMDB_API_KEY:
        return {}
    endpoint = "movie" if type_contenu == "film" else "tv"
    url_recherche = f"https://api.themoviedb.org/3/search/{endpoint}"
    params = {"api_key": TMDB_API_KEY, "query": titre, "language": "fr-FR"}
    try:
        reponse = requests.get(url_recherche, params=params, timeout=5)
        resultats = reponse.json().get('results', [])
        if not resultats:
            cache_tmdb[cle_cache] = {}
            return {}
        premier = resultats[0]
        tmdb_id = premier['id']
        url_details = f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
        reponse_details = requests.get(url_details, params={"api_key": TMDB_API_KEY, "language": "fr-FR"}, timeout=5)
        details = reponse_details.json()
        genres = [g['name'] for g in details.get('genres', [])]
        if type_contenu == "film":
            duree_minutes = details.get('runtime', 0)
            date_sortie = details.get('release_date', '')
        else:
            durees = details.get('episode_run_time', [])
            duree_minutes = durees[0] if durees else 0
            date_sortie = details.get('first_air_date', '')
        annee = date_sortie[:4] if date_sortie else None
        heures = duree_minutes // 60
        minutes = duree_minutes % 60
        duree_texte = f"{heures}h{minutes:02d}" if duree_minutes else None
        resultat = {
            "genres": genres,
            "duree": duree_texte,
            "annee": annee,
            "note": round(details.get('vote_average', 0), 1),
            "synopsis": details.get('overview', ''),
            "backdrop_url": f"https://image.tmdb.org/t/p/w1280{details.get('backdrop_path')}" if details.get('backdrop_path') else None
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
            poster = next((f for f in contenu if f['name'].lower() == 'icon.jpg'), None)
            infos_tmdb = rechercher_tmdb(dossier['name'], "film")
            films.append({
                "titre": dossier['name'],
                "video_id": video['id'] if video else None,
                "poster_id": poster['id'] if poster else None,
                **infos_tmdb
            })
    return films

def scanner_series(service, series_id):
    series = []
    for dossier_serie in lister_contenu(service, series_id):
        if dossier_serie['mimeType'] == 'application/vnd.google-apps.folder':
            contenu_serie = lister_contenu(service, dossier_serie['id'])
            poster = next((f for f in contenu_serie if f['name'].lower() == 'icon.jpg'), None)
            saisons = []
            for dossier_saison in contenu_serie:
                if dossier_saison['mimeType'] == 'application/vnd.google-apps.folder':
                    episodes_bruts = lister_contenu(service, dossier_saison['id'])
                    episodes = [
                        {"nom": ep['name'], "video_id": ep['id']}
                        for ep in episodes_bruts if ep['mimeType'].startswith('video/')
                    ]
                    saisons.append({"nom_saison": dossier_saison['name'], "episodes": episodes})
            infos_tmdb = rechercher_tmdb(dossier_serie['name'], "tv")
            series.append({
                "titre": dossier_serie['name'],
                "poster_id": poster['id'] if poster else None,
                "saisons": saisons,
                **infos_tmdb
            })
    return series

# ==================== ROUTES PROTEGEES ====================
@app.get("/bibliotheque")
def get_bibliotheque(utilisateur: dict = Depends(verifier_token)):
    pseudo = utilisateur["pseudo"]
    _charger_si_manquant()
    service = build('drive', 'v3', credentials=CREDENTIALS)
    racine_id = get_id_dossier(service, "Danatrap Stream")
    films_id = trouver_dossier_flexible(service, ["Films", "FILMS", "films"], racine_id)
    series_id = trouver_dossier_flexible(service, ["Séries", "Series", "SÉRIES", "SERIES", "séries", "series"], racine_id)
    anime_id = trouver_dossier_flexible(service, ["Animes", "Anime", "ANIMES", "ANIME", "animes", "anime"], racine_id)
    return {
        "films": scanner_films(service, films_id) if films_id else [],
        "series": scanner_series(service, series_id) if series_id else [],
        "anime": scanner_series(service, anime_id) if anime_id else []
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
    return {"status": "ok", "drive_configured": CREDENTIALS is not None}



# ==================== PISTES AUDIO / SOUS-TITRES ====================
def _get_drive_media_url(video_id: str) -> str:
    return f"https://www.googleapis.com/drive/v3/files/{video_id}?alt=media"

def _get_drive_auth_header() -> str:
    access_token = get_access_token()
    return f"Authorization: Bearer {access_token}"

def _executer_ffprobe(video_id: str, stream_type: str) -> list:
    url = _get_drive_media_url(video_id)
    headers = _get_drive_auth_header()
    cmd = [
        "ffprobe", "-headers", headers, "-v", "error",
        "-select_streams", stream_type,
        "-show_entries", "stream=index,codec_name,codec_type,disposition:stream_tags=language,title,handler_name",
        "-of", "json", url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"ffprobe error ({stream_type}): {result.stderr[:500]}")
            return []
        data = json.loads(result.stdout)
        return data.get("streams", [])
    except Exception as e:
        print(f"ffprobe exception ({stream_type}): {e}")
        return []

def _normaliser_langue(tags: dict) -> str:
    for key in ("language", "LANGUAGE", "lang", "LANG"):
        if key in tags and tags[key]:
            return tags[key]
    return "inconnu"

@app.get("/tracks/{video_id}")
def get_tracks(video_id: str, utilisateur: dict = Depends(verifier_token)):
    try:
        audio_streams = _executer_ffprobe(video_id, "a")
        subtitle_streams = _executer_ffprobe(video_id, "s")
        audio = []
        for i, s in enumerate(audio_streams):
            tags = s.get("tags", {})
            audio.append({
                "index": i,
                "stream_index": s.get("index"),
                "language": _normaliser_langue(tags),
                "title": tags.get("title") or tags.get("name") or tags.get("handler_name") or "",
                "codec": s.get("codec_name", ""),
                "default": bool(s.get("disposition", {}).get("default", 0))
            })
        subtitles = []
        for i, s in enumerate(subtitle_streams):
            tags = s.get("tags", {})
            subtitles.append({
                "index": i,
                "stream_index": s.get("index"),
                "language": _normaliser_langue(tags),
                "title": tags.get("title") or tags.get("name") or tags.get("handler_name") or "",
                "codec": s.get("codec_name", ""),
                "default": bool(s.get("disposition", {}).get("default", 0))
            })
        return {"audio": audio, "subtitles": subtitles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur analyse des pistes: {e}")

@app.get("/subtitle/{video_id}/{track_index}")
def get_subtitle(video_id: str, track_index: int, utilisateur: dict = Depends(verifier_token)):
    try:
        url = _get_drive_media_url(video_id)
        headers = _get_drive_auth_header()
        subtitle_streams = _executer_ffprobe(video_id, "s")
        if track_index < 0 or track_index >= len(subtitle_streams):
            raise HTTPException(status_code=400, detail="Index de sous-titre invalide")
        stream_index = subtitle_streams[track_index].get("index")
        tmp_path = tempfile.mktemp(suffix=".vtt")
        cmd = [
            "ffmpeg", "-y", "-headers", headers, "-i", url,
            "-map", f"0:{stream_index}",
            "-c:s", "webvtt", "-f", "webvtt", tmp_path
        ]
        subprocess.run(cmd, check=True, timeout=120)
        with open(tmp_path, "r", encoding="utf-8") as f:
            data = f.read()
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return StreamingResponse(iter([data.encode("utf-8")]), media_type="text/vtt")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Erreur extraction sous-titres: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur extraction sous-titres: {e}")

@app.get("/audio/{video_id}/{track_index}")
def get_audio(video_id: str, track_index: int, utilisateur: dict = Depends(verifier_token)):
    try:
        url = _get_drive_media_url(video_id)
        headers = _get_drive_auth_header()
        audio_streams = _executer_ffprobe(video_id, "a")
        if track_index < 0 or track_index >= len(audio_streams):
            raise HTTPException(status_code=400, detail="Index audio invalide")
        stream_index = audio_streams[track_index].get("index")
        process = subprocess.Popen(
            [
                "ffmpeg", "-headers", headers, "-i", url,
                "-map", f"0:{stream_index}",
                "-c:a", "aac", "-b:a", "192k",
                "-f", "adts", "pipe:1"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        def generate():
            try:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        return StreamingResponse(generate(), media_type="audio/aac")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur extraction audio: {e}")

# ==================== FRONTEND STATIQUE (DOIT ETRE EN DERNIER !) ====================
_FRONTEND_DIR = (pathlib.Path(__file__).parent.parent / "frontend").resolve()
if (_FRONTEND_DIR / "index.html").exists():
    ROUTES_PROTEGEES = {
        "login", "bibliotheque", "stream", "admin", "api",
        "favicon.ico", "token.pickle", "credentials.json",
        "utilisateurs.json", "static",
    }
    EXTENSIONS_BLOQUEES = {".py", ".json", ".pickle", ".log", ".env", ".txt"}

    @app.get("/", include_in_schema=False)
    def _serve_index():
        return FileResponse(str(_FRONTEND_DIR / "index.html"), media_type="text/html")

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
