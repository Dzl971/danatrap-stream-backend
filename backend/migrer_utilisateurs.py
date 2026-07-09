from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pickle
import os
import io

SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file'
]

def connexion():
    with open('token.pickle', 'rb') as token:
        return pickle.load(token)

creds = connexion()
service = build('drive', 'v3', credentials=creds)

# Trouver le dossier "Danatrap Stream"
resultats = service.files().list(
    q="name = 'Danatrap Stream' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
    fields="files(id, name)"
).execute()
racine_id = resultats['files'][0]['id']

# Vérifier si utilisateurs.json existe déjà sur Drive
resultats = service.files().list(
    q=f"name = 'utilisateurs.json' and '{racine_id}' in parents and trashed = false",
    fields="files(id, name)"
).execute()
fichiers_existants = resultats.get('files', [])

media = MediaFileUpload('utilisateurs.json', mimetype='application/json')

if fichiers_existants:
    fichier_id = fichiers_existants[0]['id']
    service.files().update(fileId=fichier_id, media_body=media).execute()
    print(f"✅ utilisateurs.json mis à jour sur Drive (ID: {fichier_id})")
else:
    metadata = {'name': 'utilisateurs.json', 'parents': [racine_id]}
    fichier = service.files().create(body=metadata, media_body=media, fields='id').execute()
    print(f"✅ utilisateurs.json créé sur Drive (ID: {fichier['id']})")