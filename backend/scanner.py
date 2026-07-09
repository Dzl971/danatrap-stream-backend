from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pickle
import os
import json

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']

def connexion():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('drive', 'v3', credentials=creds)

service = connexion()

def get_id_dossier(nom, parent_id=None):
    """Trouve l'ID d'un dossier par son nom, dans un parent donné (ou à la racine)"""
    query = f"name = '{nom}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    resultats = service.files().list(q=query, fields="files(id, name)").execute()
    fichiers = resultats.get('files', [])
    return fichiers[0]['id'] if fichiers else None

def lister_contenu(parent_id):
    """Liste tout le contenu (fichiers + dossiers) d'un dossier donné"""
    query = f"'{parent_id}' in parents and trashed = false"
    resultats = service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    return resultats.get('files', [])

def scanner_films(films_id):
    """Chaque sous-dossier de FILMS = un film"""
    films = []
    for dossier in lister_contenu(films_id):
        if dossier['mimeType'] == 'application/vnd.google-apps.folder':
            contenu = lister_contenu(dossier['id'])
            video = next((f for f in contenu if f['mimeType'].startswith('video/')), None)
            poster = next((f for f in contenu if f['name'].lower() == 'icon.jpg'), None)
            films.append({
                "titre": dossier['name'],
                "video_id": video['id'] if video else None,
                "poster_id": poster['id'] if poster else None
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

# === EXECUTION ===
racine_id = get_id_dossier("Danatrap Stream")
films_id = get_id_dossier("FILMS", racine_id)
series_id = get_id_dossier("SÉRIES", racine_id)
anime_id = get_id_dossier("ANIME", racine_id)

resultat = {
    "films": scanner_films(films_id) if films_id else [],
    "series": scanner_series(series_id) if series_id else [],
    "anime": scanner_series(anime_id) if anime_id else []  # même structure que séries
}

print(json.dumps(resultat, indent=2, ensure_ascii=False))

# On sauvegarde aussi dans un fichier pour l'étape suivante
with open('bibliotheque.json', 'w', encoding='utf-8') as f:
    json.dump(resultat, f, indent=2, ensure_ascii=False)

print("\n✅ Scan terminé, résultat sauvegardé dans bibliotheque.json")