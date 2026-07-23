#!/usr/bin/env python3
"""Run librespeed-cli and publish its result to Home Assistant through MQTT."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ModuleNotFoundError:
    mqtt = None  # type: ignore[assignment]


LOG = logging.getLogger("librespeed-mqtt")


def read_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def required(section: dict[str, Any], key: str) -> Any:
    value = section.get(key)
    if value is None or value == "":
        raise ValueError(f"missing required setting: {key}")
    return value


def run_speedtest(config: dict[str, Any]) -> dict[str, Any]:
    test = config["test"]
    command = [str(test.get("command", "/usr/bin/librespeed-cli")), "--json"]
    if test.get("secure", True):
        command.append("--secure")
    if test.get("no_icmp", True):
        command.append("--no-icmp")
    if server_id := test.get("server_id"):
        command.extend(["--server", str(server_id)])
    if interface := test.get("interface"):
        command.extend(["--interface", str(interface)])
    for option in ("timeout", "duration", "concurrent", "chunks", "upload_size"):
        if option in test:
            command.extend([f"--{option.replace('_', '-')}", str(test[option])])

    timeout = int(test.get("process_timeout", 120))
    LOG.info("running LibreSpeed: %s", " ".join(command))
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=True
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"librespeed-cli returned invalid JSON: {exc}") from exc
    if isinstance(payload, list):
        if not payload:
            raise RuntimeError("librespeed-cli returned an empty result list")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError("librespeed-cli returned an unexpected JSON structure")
    for key in ("download", "upload", "ping", "jitter", "server"):
        if key not in payload:
            raise RuntimeError(f"librespeed-cli result is missing {key!r}")
    return payload


def mqtt_client(config: dict[str, Any]) -> mqtt.Client:
    if mqtt is None:
        raise RuntimeError("paho-mqtt is not installed; install the python3-paho-mqtt package")
    broker = config["mqtt"]
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=str(required(broker, "client_id")),
        protocol=mqtt.MQTTv311,
    )
    username = broker.get("username")
    password_file = broker.get("password_file")
    if username and password_file:
        client.username_pw_set(str(username), Path(password_file).read_text().strip())
    elif username or password_file:
        raise ValueError("mqtt username and password_file must be configured together")
    if cafile := broker.get("cafile"):
        client.tls_set(ca_certs=str(cafile))
    client.connect(str(required(broker, "host")), int(broker.get("port", 1883)), 30)
    client.loop_start()
    return client


def publish(client: mqtt.Client, topic: str, payload: Any, retain: bool = True) -> None:
    encoded = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    info = client.publish(topic, encoded, qos=1, retain=retain)
    info.wait_for_publish()
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish to {topic} failed: {info.rc}")


def discovery_payloads(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mqtt_config = config["mqtt"]
    topic_base = str(required(mqtt_config, "topic_base")).rstrip("/")
    object_base = str(mqtt_config.get("object_id_base", "librespeed_prox2"))
    device = {
        "identifiers": [object_base],
        "name": str(mqtt_config.get("device_name", "LibreSpeed prox2")),
        "manufacturer": "LibreSpeed",
        "model": "librespeed-cli MQTT wrapper",
    }
    common = {
        "state_topic": f"{topic_base}/state",
        "json_attributes_topic": f"{topic_base}/state",
        "availability_topic": f"{topic_base}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }
    entities = {
        "download": {
            "name": "Download",
            "unit_of_measurement": "Mbit/s",
            "device_class": "data_rate",
            "state_class": "measurement",
            "value_template": "{{ value_json.download }}",
        },
        "upload": {
            "name": "Upload",
            "unit_of_measurement": "Mbit/s",
            "device_class": "data_rate",
            "state_class": "measurement",
            "value_template": "{{ value_json.upload }}",
        },
        "ping": {
            "name": "Ping",
            "unit_of_measurement": "ms",
            "device_class": "duration",
            "state_class": "measurement",
            "value_template": "{{ value_json.ping }}",
        },
        "jitter": {
            "name": "Jitter",
            "unit_of_measurement": "ms",
            "device_class": "duration",
            "state_class": "measurement",
            "value_template": "{{ value_json.jitter }}",
        },
        "server": {
            "name": "Server",
            "icon": "mdi:server-network",
            "value_template": "{{ value_json.server.name }}",
        },
        "last_test": {
            "name": "Last Test",
            "device_class": "timestamp",
            "value_template": "{{ value_json.reported_at }}",
        },
    }
    discovery_base = str(mqtt_config.get("discovery_prefix", "homeassistant")).rstrip("/")
    return {
        f"{discovery_base}/sensor/{object_base}_{key}/config": {
            **common,
            **entity,
            "unique_id": f"{object_base}_{key}",
        }
        for key, entity in entities.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/librespeed-mqtt/config.toml"))
    args = parser.parse_args()
    config = read_config(args.config)
    for section in ("test", "mqtt"):
        if section not in config:
            raise ValueError(f"missing [{section}] section")

    client = mqtt_client(config)
    topic_base = str(required(config["mqtt"], "topic_base")).rstrip("/")
    try:
        for topic, payload in discovery_payloads(config).items():
            publish(client, topic, payload)
        publish(client, f"{topic_base}/availability", "offline")
        try:
            result = run_speedtest(config)
            result["reported_at"] = datetime.now(UTC).isoformat()
            result["ok"] = True
        except Exception as exc:
            LOG.exception("LibreSpeed test failed")
            publish(client, f"{topic_base}/error", {"reported_at": datetime.now(UTC).isoformat(), "error": str(exc)})
            return 1
        publish(client, f"{topic_base}/state", result)
        publish(client, f"{topic_base}/error", "")
        publish(client, f"{topic_base}/availability", "online")
        LOG.info("published %.2f down / %.2f up / %.2f ms ping", result["download"], result["upload"], result["ping"])
        return 0
    finally:
        time.sleep(0.2)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOG.error("fatal error: %s", exc)
        raise SystemExit(2)
