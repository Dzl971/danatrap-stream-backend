# Déploiement Render - DanaTrap Stream

## 1. Créer le service via Blueprint

1. Va sur https://dashboard.render.com
2. Clique sur **New +** → **Blueprint**
3. Connecte le repo GitHub : `Dzl971/danatrap-stream-backend`
4. Render va lire `render.yaml` et créer automatiquement le service web

## 2. Configurer les variables d'environnement

Dans le service `danatrap-stream-backend`, onglet **Environment** :

| Clé | Valeur |
|---|---|
| `TOKEN_BASE64` | Copie le contenu de `backend/token_base64.txt` (sur UNE seule ligne) |
| `CLE_SECRETE_JWT` | `P239yNuNzPdsWAe1R1qwDfDLlNnhzEljd0_0VXw6WMDkxlqDfD-ThIlVX2h0zeDl` |

⚠️ Ne committe JAMAIS `token_base64.txt` ou `CLE_SECRETE_JWT` dans Git.

## 3. Redémarrer le service

1. Onglet **Manual Deploy** → **Deploy latest commit**
2. Attends que le build soit terminé (logs verts)
3. L'URL Render sera affichée en haut (ex: `https://danatrap-stream-backend.onrender.com`)

## 4. Vérifier le déploiement

Ouvre l'URL Render dans Chrome :
- Page login Danatrap → OK
- Login avec `Dzl 971` → bibliothèque OK
- Section Séries → OK
- Bouton Admin → OK

## 5. Domaine personnalisé

1. Dans Render, onglet **Settings** → **Custom Domains**
2. Ajoute ton domaine (ex: `stream.danatrap.fr`)
3. Render te donne un enregistrement CNAME à ajouter chez ton registrar
4. Exemple DNS chez OVH/Gandi/Cloudflare :
   - Type : `CNAME`
   - Nom : `stream` (ou `@` pour root)
   - Valeur : `danatrap-stream-backend.onrender.com` (remplace par la valeur Render)
5. Attends la propagation DNS (5 min à 24h)
6. Render validera automatiquement le domaine et génèrera le certificat SSL

---
Token base64 length: 1956 chars
