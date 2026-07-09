import os
import subprocess
import json

DOSSIER_LOCAL_BASE = r"E:\Mon Drive\Danatrap Stream"
EXTENSIONS_VIDEO = ('.mp4', '.mkv', '.avi', '.mov')

def obtenir_codec_audio(chemin_fichier):
    """Utilise ffprobe pour détecter le codec audio d'un fichier vidéo"""
    commande = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'a:0',
        '-show_entries', 'stream=codec_name',
        '-of', 'json',
        chemin_fichier
    ]
    try:
        resultat = subprocess.run(commande, capture_output=True, text=True, timeout=30)
        donnees = json.loads(resultat.stdout)
        streams = donnees.get('streams', [])
        if not streams:
            return None  # Pas de piste audio du tout
        return streams[0].get('codec_name')
    except Exception as e:
        print(f"⚠️  Erreur lecture codec pour {chemin_fichier}: {e}")
        return None

def corriger_audio(chemin_fichier):
    """Crée une version corrigée avec audio en AAC, à côté de l'original"""
    dossier = os.path.dirname(chemin_fichier)
    nom_fichier = os.path.basename(chemin_fichier)
    nom_sans_ext, ext = os.path.splitext(nom_fichier)
    chemin_corrige = os.path.join(dossier, f"{nom_sans_ext}_AUDIO_CORRIGE{ext}")

    commande = [
        'ffmpeg', '-y', '-i', chemin_fichier,
        '-c:v', 'copy',       # Vidéo intacte, pas de réencodage
        '-c:a', 'aac',        # Audio converti en AAC
        '-b:a', '192k',
        chemin_corrige
    ]

    print(f"   🔄 Conversion en cours...")
    resultat = subprocess.run(commande, capture_output=True, text=True)

    if resultat.returncode == 0 and os.path.exists(chemin_corrige):
        taille_originale = os.path.getsize(chemin_fichier) / (1024*1024)
        taille_corrigee = os.path.getsize(chemin_corrige) / (1024*1024)
        print(f"   ✅ Créé : {os.path.basename(chemin_corrige)} ({taille_corrigee:.0f} Mo, original: {taille_originale:.0f} Mo)")
        return chemin_corrige
    else:
        print(f"   ❌ Échec de la conversion")
        return None

def scanner_et_corriger():
    print(f"=== Scan de {DOSSIER_LOCAL_BASE} ===\n")
    fichiers_traites = 0
    fichiers_corriges = 0

    for dossier_actuel, sous_dossiers, fichiers in os.walk(DOSSIER_LOCAL_BASE):
        for nom_fichier in fichiers:
            if not nom_fichier.lower().endswith(EXTENSIONS_VIDEO):
                continue
            if '_AUDIO_CORRIGE' in nom_fichier:
                continue  # On ignore les fichiers déjà corrigés

            chemin_complet = os.path.join(dossier_actuel, nom_fichier)
            fichiers_traites += 1

            print(f"📹 {nom_fichier}")
            codec = obtenir_codec_audio(chemin_complet)

            if codec is None:
                print(f"   ⚠️  Pas de piste audio détectée ou erreur de lecture")
            elif codec == 'aac':
                print(f"   ✅ Déjà en AAC, rien à faire")
            else:
                print(f"   🔴 Codec audio actuel : {codec} → correction nécessaire")
                if corriger_audio(chemin_complet):
                    fichiers_corriges += 1
            print()

    print(f"=== Terminé : {fichiers_traites} fichier(s) analysé(s), {fichiers_corriges} corrigé(s) ===")
    print("\n⚠️  IMPORTANT : vérifie chaque fichier '_AUDIO_CORRIGE' avant de remplacer l'original.")
    print("Rien n'a été supprimé automatiquement.")

if __name__ == "__main__":
    scanner_et_corriger()