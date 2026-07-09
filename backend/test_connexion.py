from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pickle
import os

# Les droits qu'on demande : lecture seule sur ton Drive
SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file'
]
def connexion():
    creds = None
    # Si on a déjà un token sauvegardé, on le réutilise
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)

    # Sinon, on lance la connexion via le navigateur
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            'credentials.json', SCOPES)
        creds = flow.run_local_server(port=0)
        # On sauvegarde le token pour ne pas avoir à se reconnecter à chaque fois
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)

    return build('drive', 'v3', credentials=creds)

# Test : lister les 10 premiers fichiers/dossiers de ton Drive
service = connexion()
resultats = service.files().list(pageSize=10, fields="files(id, name, mimeType)").execute()
fichiers = resultats.get('files', [])

print("\n=== Connexion réussie ! Voici tes 10 premiers fichiers/dossiers ===\n")
for f in fichiers:
    print(f"- {f['name']} ({f['mimeType']})")