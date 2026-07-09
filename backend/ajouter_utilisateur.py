import bcrypt
import json
import os

FICHIER_UTILISATEURS = "utilisateurs.json"

def charger_utilisateurs():
    if os.path.exists(FICHIER_UTILISATEURS):
        with open(FICHIER_UTILISATEURS, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def sauvegarder_utilisateurs(utilisateurs):
    with open(FICHIER_UTILISATEURS, 'w', encoding='utf-8') as f:
        json.dump(utilisateurs, f, indent=2, ensure_ascii=False)

def ajouter_utilisateur(pseudo, mot_de_passe):
    utilisateurs = charger_utilisateurs()
    mot_de_passe_bytes = mot_de_passe.encode('utf-8')
    hash_genere = bcrypt.hashpw(mot_de_passe_bytes, bcrypt.gensalt())
    utilisateurs[pseudo] = {
        "mot_de_passe_hash": hash_genere.decode('utf-8')
    }
    sauvegarder_utilisateurs(utilisateurs)
    print(f"\n✅ Utilisateur '{pseudo}' ajouté/mis à jour avec succès !")

if __name__ == "__main__":
    print("=== Ajouter ou réinitialiser un utilisateur ===\n")
    pseudo = input("Pseudo : ").strip()
    mot_de_passe = input("Mot de passe : ").strip()
    ajouter_utilisateur(pseudo, mot_de_passe)

    print("\n--- Liste actuelle des utilisateurs ---")
    utilisateurs = charger_utilisateurs()
    for p in utilisateurs:
        print(f"- {p}")