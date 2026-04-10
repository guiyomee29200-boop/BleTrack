# Plan d'adressage IP — Réseau BLETrack (RJ45 isolé)

## Réseau dédié

| Paramètre | Valeur |
|-----------|--------|
| Sous-réseau | 10.0.1.0 / 24 |
| Plage utilisable | 10.0.1.1 → 10.0.1.254 |
| Masque | 255.255.255.0 |
| Passerelle | aucune (réseau isolé) |
| DNS | aucun |

---

## Attribution des adresses fixes

| IP | Hostname | Matériel | Rôle |
|----|----------|----------|------|
| 10.0.1.1 | — | Switch / routeur | Infrastructure réseau |
| **10.0.1.10** | rpi-master | Raspberry Pi 4 | Serveur BLETrack + Gateway gw1 + Broker MQTT |
| **10.0.1.20** | rpi-gw2 | Raspberry Pi 500 | Gateway gw2 |
| 10.0.1.21 | rpi-gw3 | (futur) | Gateway gw3 |
| 10.0.1.22 | rpi-gw4 | (futur) | Gateway gw4 |
| **10.0.1.100** | pc-admin | PC / Laptop | Poste d'administration |

### Plages réservées
| Plage | Usage |
|-------|-------|
| .1 | Infrastructure (switch) |
| .10 | Serveur maître |
| .20 – .29 | Gateways BLE |
| .100 – .110 | Postes admin |

---

## Configuration à appliquer sur les Raspberry Pi

Les deux Pi utilisent **NetworkManager** (nmcli), pas dhcpcd.

### Pi 4 — Maître (eth0 → 10.0.1.10)

```bash
sudo nmcli connection modify 'netplan-eth0' \
  ipv4.method manual ipv4.addresses '10.0.1.10/24' ipv4.gateway ''
sudo nmcli connection up 'netplan-eth0'
```

### Pi 500 — Gateway gw2 (eth0 → 10.0.1.20)

```bash
sudo nmcli connection modify 'netplan-eth0' \
  ipv4.method manual ipv4.addresses '10.0.1.20/24' ipv4.gateway ''
sudo nmcli connection up 'netplan-eth0'
```

---

## Fichiers BLETrack à mettre à jour après changement d'IP

### config.yaml (sur Pi 4 — maître)
```yaml
mqtt:
  broker: "10.0.1.10"   # IP fixe du maître

gateways:
  - id: "gw1"
    ip: "10.0.1.10"
  - id: "gw2"
    ip: "10.0.1.20"
```

### config.yaml (sur Pi 500 — gw2)
```yaml
mqtt:
  broker: "10.0.1.10"   # Toujours pointer vers le maître
```

---

## Transition WiFi → RJ45

> Le WiFi reste actif sur les deux Pi — il sert pour l'accès admin et SSH.
> Le RJ45 est dédié au trafic MQTT entre gateways.

1. Brancher le câble RJ45 entre les Pi
2. Appliquer la config IP fixe sur `eth0` (voir ci-dessus)
3. Vérifier la connectivité : `ping 10.0.1.20` depuis le Pi maître
4. Mettre à jour les `config.yaml` avec les nouvelles IPs
5. Redémarrer les services : `sudo systemctl restart bletrack-server bletrack-gateway`
