"""
BLETrack — Scanner BLE (tourne sur chaque Raspberry Pi gateway)

Usage :
    python ble_scanner.py --gateway gw1 --config /path/to/config.yaml

Dépendances (requirements-gateway.txt) :
    bleak, pyyaml, aiomqtt
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import time
from collections import defaultdict
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)


def setup_file_logging(gateway_id: str, log_dir: Path):
    """Ajoute un fichier log rotatif lisible avec tail -f."""
    log_dir.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        log_dir / f"scanner-{gateway_id}.log",
        maxBytes=5 * 1024 * 1024,   # 5 Mo
        backupCount=3,
        encoding="utf-8",
    )
    h.setFormatter(logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(h)
    logger.info(f"Logs écrits dans {log_dir / f'scanner-{gateway_id}.log'}")

# Préfixe des beacons H2/H2A Navigation à filtrer (optionnel)
# Laisser vide pour scanner tous les appareils BLE
BEACON_COMPANY_ID = None  # ex: 0x0059 pour Nordic Semiconductor


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


class BLEScanner:
    def __init__(self, gateway_id: str, config: dict):
        self.gateway_id = gateway_id
        self.mqtt_broker = config["mqtt"]["broker"]
        self.mqtt_port = config["mqtt"]["port"]
        self.topic = f"{config['mqtt']['topic_prefix']}/rssi"
        self.batch_interval = 0.5  # secondes entre chaque envoi MQTT

        # Buffer : {mac: [rssi, ...]} pour le batch courant
        self._buffer: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def run(self):
        logger.info(f"Gateway {self.gateway_id} — démarrage du scanner BLE")
        await asyncio.gather(
            self._scan_loop(),
            self._publish_loop(),
        )

    async def _scan_loop(self):
        """Scan BLE continu via Bleak."""
        from bleak import BleakScanner

        def callback(device, advertisement_data):
            mac = device.address.upper()
            rssi = advertisement_data.rssi

            # Filtrage optionnel par fabricant
            if BEACON_COMPANY_ID is not None:
                mfr_data = advertisement_data.manufacturer_data
                if BEACON_COMPANY_ID not in mfr_data:
                    return

            asyncio.get_event_loop().call_soon_threadsafe(
                self._buffer[mac].append, rssi
            )

        async with BleakScanner(callback) as scanner:
            logger.info("Scanner BLE actif")
            while True:
                await asyncio.sleep(60)  # Bleak gère le scan en continu

    async def _publish_loop(self):
        """Envoie les RSSI accumulés par batch toutes les 500ms via MQTT."""
        try:
            import aiomqtt
        except ImportError:
            logger.error("aiomqtt non installé. pip install aiomqtt")
            return

        while True:
            await asyncio.sleep(self.batch_interval)

            async with self._lock:
                if not self._buffer:
                    continue

                readings = []
                for mac, rssi_list in self._buffer.items():
                    if rssi_list:
                        avg = sum(rssi_list) / len(rssi_list)
                        readings.append({"tag_mac": mac, "rssi": round(avg, 1)})
                self._buffer.clear()

            if not readings:
                continue

            payload = json.dumps({
                "gateway_id": self.gateway_id,
                "timestamp": time.time(),
                "readings": readings,
            })

            try:
                async with aiomqtt.Client(self.mqtt_broker, self.mqtt_port) as client:
                    await client.publish(self.topic, payload=payload, qos=0)
                logger.debug(f"Publié {len(readings)} tag(s) sur {self.topic}")
            except Exception as e:
                logger.warning(f"Erreur MQTT publish: {e}")


async def main():
    parser = argparse.ArgumentParser(description="BLETrack Gateway Scanner")
    parser.add_argument("--gateway", required=True, help="ID du gateway (ex: gw1)")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent.parent / "config" / "config.yaml"),
        help="Chemin vers config.yaml",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # Logs fichier dans /opt/bletrack/logs/ (ou à côté du script en dev)
    log_dir = Path(args.config).parent.parent / "logs"
    setup_file_logging(args.gateway, log_dir)

    scanner = BLEScanner(gateway_id=args.gateway, config=config)
    await scanner.run()


if __name__ == "__main__":
    asyncio.run(main())
