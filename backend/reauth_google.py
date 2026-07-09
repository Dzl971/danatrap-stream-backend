import os
import base64
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/drive.file',
]

for ancien in ('token.pickle', 'token_base64.txt'):
    if os.path.exists(ancien):
        os.remove(ancien)
        print(f"Ancien fichier supprime : {ancien}")

if not os.path.exists('credentials.json'):
    raise SystemExit("credentials.json introuvable. Place ce script dans le meme dossier que credentials.json.")

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0, open_browser=True)

service = build('drive', 'v3', credentials=creds)
resultats = service.files().list(pageSize=5, fields="files(id, name)").execute()
fichiers = resultats.get('files', [])
print(f"\nConnexion reussie ! {len(fichiers)} fichier(s) accessible(s) :")
for f in fichiers:
    print(f"   - {f['name']}")

with open('token.pickle', 'wb') as token:
    pickle.dump(creds, token)

contenu = open('token.pickle', 'rb').read()
encode = base64.b64encode(contenu).decode('utf-8')

with open('token_base64.txt', 'w', encoding='utf-8') as f:
    f.write(encode)

print(f"\nToken base64 genere ({len(encode)} caracteres)")
print("=" * 60)
print("Copie la ligne ci-dessous dans TOKEN_BASE64 sur Render :")
print("=" * 60)
print(encode)
print("=" * 60)
