# Guide de réglage des gateways BLETrack

## Comprendre le positionnement BLE

Le système calcule la position d'un tag en mesurant la force du signal (RSSI) reçu par chaque gateway.
La formule de base est :

```
distance (m) = 10 ^ ( (rssi_1m - rssi_mesuré) / (10 × path_loss) )
```

Deux paramètres sont à calibrer par gateway :

| Paramètre | Rôle | Valeur typique |
|-----------|------|----------------|
| **rssi_1m** | RSSI mesuré à exactement 1m du tag | -60 à -70 dBm |
| **path_loss** | Vitesse d'atténuation du signal avec la distance | 2.0 à 3.5 |

---

## rssi_1m — Calibration à 1 mètre

### Pourquoi ?
Chaque gateway a une antenne légèrement différente, et l'environnement proche (murs, métal)
influence le signal. Le rssi_1m ancre le calcul de distance sur une mesure réelle.

### Valeurs de référence
- **-40 à -50 dBm** : signal très fort — le tag était probablement collé à la gateway pendant la mesure
- **-55 à -65 dBm** : valeur normale pour un beacon de qualité à 1m en espace ouvert
- **-65 à -75 dBm** : normal si la gateway est derrière un obstacle léger

### Comment mesurer correctement
1. Placer le tag à **exactement 1 mètre** de la gateway, en ligne directe (pas de mur entre les deux)
2. Garder le tag **immobile** pendant toute la durée de la mesure
3. Dans Admin → Gateways → ouvrir "Calibration & config"
4. Sélectionner le tag dans la liste déroulante
5. Régler la durée à **10 secondes** (plus de mesures = moyenne plus fiable)
6. Cliquer "Mesurer" et ne pas bouger le tag
7. Cliquer "Appliquer" sur le résultat, puis "Enregistrer"

### Erreurs fréquentes
- Tag tenu à la main (le corps absorbe le signal → rssi_1m trop faible)
- Tag posé sur une surface métallique (réflexions → valeur erratique)
- Tag trop proche (< 0.5m) → rssi_1m trop élevé, distances sous-estimées par la suite

---

## path_loss — Atténuation du signal

### Pourquoi ?
Le signal BLE s'atténue différemment selon l'environnement. En espace ouvert le signal
reste fort à longue distance ; en béton il chute vite.

### Valeurs de référence

| Environnement | path_loss |
|---------------|-----------|
| Espace ouvert, vue directe | 2.0 |
| Couloir dégagé | 2.0 – 2.5 |
| Bureau open space | 2.5 – 3.0 |
| Pièce avec cloisons | 3.0 – 3.5 |
| Béton épais, murs porteurs | 3.5 – 4.5 |

### Comment calibrer automatiquement
Pendant la mesure à 1m sur une gateway, si l'**autre gateway voit aussi le tag**,
le système calcule automatiquement le path_loss de cette autre gateway.
Un encadré jaune apparaît avec la suggestion — cliquer "Appliquer".

Cela fonctionne parce que la distance entre les deux gateways est connue, et la distance
approximative du tag à l'autre gateway est donc connue (= distance entre gateways − 1m).

### Réglage manuel
Si la suggestion automatique n'est pas disponible (une seule gateway voit le tag) :
1. Placer le tag à une **distance connue** de la gateway (ex : 5 mètres)
2. Lire le RSSI dans "Tags visibles"
3. Calculer manuellement :
   ```
   path_loss = (rssi_1m - rssi_à_5m) / (10 × log10(5))
   ```
   Exemple : rssi_1m = -62, rssi_à_5m = -75
   ```
   path_loss = (-62 - (-75)) / (10 × 0.699) = 13 / 6.99 ≈ 1.86
   ```

---

## Positions des gateways (x, y)

Les coordonnées x et y sont en **mètres réels** dans le bâtiment.
Elles sont normalement définies depuis le plan dans Admin → Plan.

Si tu les saisis manuellement dans l'onglet Gateways :
- **x** = distance depuis le mur gauche du bâtiment
- **y** = distance depuis le mur du bas (ou de référence)
- Les deux gateways doivent être le plus éloignées possible l'une de l'autre
  pour avoir une bonne triangulation

---

## Procédure complète de calibration

### Étape 1 — Vérifier les positions
Dans Admin → Gateways, confirmer que les x/y correspondent aux positions réelles
des Raspberry Pi dans le bâtiment.

### Étape 2 — Calibrer gw1
1. Se placer à **1m de gw1** avec le tag
2. Mesurer le rssi_1m (10 secondes)
3. Appliquer et enregistrer
4. Si gw2 est visible, appliquer aussi le path_loss suggéré pour gw2

### Étape 3 — Calibrer gw2
1. Se placer à **1m de gw2** avec le même tag
2. Mesurer le rssi_1m pour gw2
3. Appliquer et enregistrer
4. Si gw1 est visible, appliquer aussi le path_loss suggéré pour gw1

### Étape 4 — Vider les buffers
Cliquer "Vider tous les buffers" pour que les nouvelles valeurs s'appliquent
immédiatement sans résidu des anciennes mesures.

### Étape 5 — Vérifier
Placer le tag à une position connue et vérifier sur la carte /combat
que le point s'affiche au bon endroit (±1-2m acceptable avec 2 gateways).

---

## Lecture des "Tags visibles"

Dans chaque carte gateway, la section "Tags visibles" affiche en temps réel :

```
AA:BB:CC   -65 dBm   ≈1.2m
```

| Couleur du RSSI | Signification |
|-----------------|---------------|
| Vert | Signal fort (> -70 dBm) — tag proche ou beacon puissant |
| Orange | Signal moyen (-70 à -85 dBm) — tag à distance moyenne |
| Rouge | Signal faible (< -85 dBm) — tag loin ou signal atténué |

La **distance estimée** (≈1.2m) est calculée avec le rssi_1m et path_loss actuels.
Si la distance affichée ici est juste, la position sur la carte sera juste.

---

## Dépannage

### Le tag s'affiche toujours à la même position
→ Cliquer "Vider tous les buffers" dans Calibration & config

### La distance estimée est 10× trop grande
→ Le rssi_1m est probablement trop élevé (ex : -40 au lieu de -65)
→ Refaire la calibration à 1m avec le tag correctement positionné

### Le tag saute entre deux positions
→ Le path_loss est trop faible — augmenter légèrement (ex : 2.5 → 3.0)
→ Ou ajouter plus de gateways pour améliorer la triangulation

### Avec une seule gateway visible
La position ne peut pas être calculée avec précision (direction inconnue).
Le tag s'affiche à une distance estimée dans l'axe x de la gateway.
Il faut que les **deux gateways voient le tag** pour avoir une position fiable.

---

## Matériel recommandé pour les tags

Les smartphones ne conviennent pas (MAC aléatoire, signal instable).

| Type | Avantages |
|------|-----------|
| Tile / Chipolo | Prêt à l'emploi, MAC fixe, longue autonomie |
| ESP32 en mode iBeacon | Configurable, puissance réglable, économique |
| Balise iBeacon industrie | Robuste, IP67, autonomie 2-5 ans |
| Bracelet BLE (Xiaomi Mi Band) | Discret, MAC fixe |
