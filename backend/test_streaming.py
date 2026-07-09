from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pickle
import os
import io

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

# L'ID vidéo de Transformers qu'on a récupéré juste avant
VIDEO_ID = "1bhnfdCclVxPZwmtZ4IJvoaodp3xAqgJA"

# On récupère juste les infos du fichier (taille, etc.) pour vérifier l'accès
fichier_info = service.files().get(fileId=VIDEO_ID, fields="name, size, mimeType").execute()
print(f"Nom : {fichier_info['name']}")
print(f"Taille : {int(fichier_info['size']) / (1024*1024):.2f} Mo")
print(f"Type : {fichier_info['mimeType']}")

# On télécharge les 5 premiers Mo seulement pour tester le streaming par morceaux
request = service.files().get_media(fileId=VIDEO_ID)
request.headers['Range'] = 'bytes=0-5242880'  # 5 Mo

fh = io.FileIO('extrait_test.mp4', 'wb')
downloader = MediaIoBaseDownload(fh, request)
done = False
while not done:
    status, done = downloader.next_chunk()
    print(f"Téléchargement : {int(status.progress() * 100)}%")

print("\n✅ Extrait téléchargé dans extrait_test.mp4 — essaie de le lire avec VLC pour vérifier que ça marche")