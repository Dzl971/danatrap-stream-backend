import base64

with open('token.pickle', 'rb') as f:
    contenu = f.read()

encode = base64.b64encode(contenu).decode('utf-8')

with open('token_base64.txt', 'w') as f:
    f.write(encode)

print("✅ Conversion terminée ! Le résultat est dans token_base64.txt")
print("Longueur du texte généré :", len(encode), "caractères")