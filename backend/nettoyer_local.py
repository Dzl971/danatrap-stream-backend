import os
import time
import pickle
from googleapiclient.discovery import build

DOSSIER_LOCAL_BASE = r"E:\Mon Drive\Danatrap Stream"
NOM_DOSSIER_RACINE_DRIVE = "Danatrap Stream"
DELAI_SECURITE_MINUTES = 10  # Ignore les fichiers modifiés il y a moins de X minutes
TAILLE_MIN_MO = 20  # Ignore les tout petits fichiers (comme icon.jpg)

def connexion():
    with open('token.pickle', 'rb') as token:
        creds = pickle.load(token)
    return build('drive', 'v3', credentials=creds)

service = connexion()
cache_dossiers = {}

def get_id_dossier(nom, parent_id):
    cle = f"{parent_id}/{nom}"
    if cle in cache_dossiers:
        return cache_dossiers[cle]

    query = f"name = '{nom}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false and '{parent_id}' in parents"
    resultats = service.files().list(q=query, fields="files(id, name)").execute()
    fichiers = resultats.get('files', [])
    id_trouve = fichiers[0]['id'] if fichiers else None
    cache_dossiers[cle] = id_trouve
    return id_trouve

def resoudre_dossier_drive(chemin_relatif_dossiers):
    """Remonte l'arborescence de dossiers locaux pour trouver l'ID du dossier Drive correspondant"""
    query_racine = f"name = '{NOM_DOSSIER_RACINE_DRIVE}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    resultats = service.files().list(q=query_racine, fields="files(id, name)").execute()
    fichiers = resultats.get('files', [])
    if not fichiers:
        return None
    id_courant = fichiers[0]['id']

    for nom_dossier in chemin_relatif_dossiers:
        id_courant = get_id_dossier(nom_dossier, id_courant)
        if id_courant is None:
            return None
    return id_courant

def taille_fichier_drive(nom_fichier, dossier_id):
    query = f"name = '{nom_fichier}' and '{dossier_id}' in parents and trashed = false"
    resultats = service.files().list(q=query, fields="files(id, name, size)").execute()
    fichiers = resultats.get('files', [])
    if not fichiers:
        return None
    return int(fichiers[0].get('size', 0))

def nettoyer():
    print(f"\n=== Vérification à {time.strftime('%H:%M:%S')} ===")
    maintenant = time.time()
    fichiers_supprimes = 0

    for dossier_actuel, sous_dossiers, fichiers in os.walk(DOSSIER_LOCAL_BASE):
        for nom_fichier in fichiers:
            chemin_complet = os.path.join(dossier_actuel, nom_fichier)

            taille_locale = os.path.getsize(chemin_complet)
            if taille_locale < TAILLE_MIN_MO * 1024 * 1024:
                continue  # Trop petit, on ignore (ex: icon.jpg)

            derniere_modif = os.path.getmtime(chemin_complet)
            if (maintenant - derniere_modif) < (DELAI_SECURITE_MINUTES * 60):
                print(f"⏳ Ignoré (trop récent) : {nom_fichier}")
                continue

            # Reconstruire le chemin relatif de dossiers pour retrouver le bon dossier Drive
            chemin_relatif = os.path.relpath(dossier_actuel, DOSSIER_LOCAL_BASE)
            dossiers_intermediaires = [] if chemin_relatif == '.' else chemin_relatif.split(os.sep)

            dossier_drive_id = resoudre_dossier_drive(dossiers_intermediaires)
            if dossier_drive_id is None:
                print(f"⚠️  Dossier Drive introuvable pour : {chemin_complet}")
                continue

            taille_drive = taille_fichier_drive(nom_fichier, dossier_drive_id)

            if taille_drive is not None and taille_drive == taille_locale:
                os.remove(chemin_complet)
                fichiers_supprimes += 1
                print(f"✅ Supprimé (synchronisé) : {nom_fichier} ({taille_locale / (1024*1024):.0f} Mo libérés)")
            else:
                print(f"⏸️  Pas encore synchronisé : {nom_fichier}")

    print(f"\n{fichiers_supprimes} fichier(s) nettoyé(s).\n")

# === BOUCLE PRINCIPALE ===
if __name__ == "__main__":
    print("🧹 Nettoyeur DanaTrap Stream démarré.")
    print(f"Surveillance de : {DOSSIER_LOCAL_BASE}")
    print("Appuie sur Ctrl+C pour arrêter.\n")

    while True:
        nettoyer()
        time.sleep(15 * 60)  # Vérifie toutes les 15 minutes