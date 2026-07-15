# Correction des pistes audio et des sous-titres

## Problème corrigé

La route `/tracks/{video_id}` lançait l’extraction complète de la première piste audio avant de répondre. Sur un film long, la requête pouvait expirer sur Render : le lecteur ne recevait alors aucune langue et aucun sous-titre.

Le lecteur gérait également mal le son lors d’un changement de piste : la piste originale pouvait se réactiver, le volume/muet n’était pas toujours appliqué à l’audio externe, et les erreurs de sous-titres étaient ignorées.

## Fichiers modifiés

- `backend/serveur.py`
- `frontend/index.html`
- `android-app/www/index.html`
- `RENDER_DEPLOY.md`

## Fonctionnement après correction

- La liste des pistes est renvoyée dès que `ffprobe` a identifié les flux.
- La piste choisie est extraite à la demande et diffusée en AAC/MP4 compatible navigateur.
- L’audio externe reste synchronisé après une avance, un retour ou un déplacement dans la barre de lecture.
- Le volume et le mode muet fonctionnent avec toutes les pistes.
- Les sous-titres texte sont convertis en WebVTT puis affichés dans le lecteur.
- Une erreur visible est affichée lorsque la piste est incompatible au lieu de ne rien faire.

## Redéploiement sur Render

1. Remplace les fichiers modifiés dans ton dépôt GitHub.
2. Fais un commit puis pousse les changements.
3. Dans Render, lance **Manual Deploy → Deploy latest commit** si le redéploiement automatique ne démarre pas.
4. Ouvre `/api/health` sur ton domaine et vérifie que `ffmpeg` et `ffprobe` valent `true`.
5. Recharge le site avec `Ctrl + F5` pour éviter une ancienne version en cache.

## Formats de sous-titres

Les pistes texte comme SRT, ASS/SSA, WebVTT et `mov_text` peuvent être converties. Les pistes PGS/DVD sont des images : le navigateur ne peut pas les afficher comme texte avec cette méthode. Le lecteur indique désormais clairement ce cas.

## Sécurité

L’archive d’origine contient des fichiers d’identification Google et des jetons. Ils ont été exclus de l’archive corrigée. Conserve-les uniquement dans les variables d’environnement Render ou dans un emplacement local non suivi par Git. Si ces fichiers ou une ancienne clé JWT ont déjà été publiés, génère de nouveaux identifiants et une nouvelle clé.
