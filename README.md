# LibreSpeed MQTT

Runs [`librespeed-cli`](https://github.com/librespeed/speedtest-cli) and publishes
the result to MQTT. Home Assistant MQTT Discovery creates sensors for download,
upload, ping, jitter, test server, and last test time automatically.

The full result, including the server metadata reported by LibreSpeed, is kept as
attributes on each sensor. Failed tests publish a retained error payload instead
of overwriting the last successful measurement.

## Install

On a Debian-based host:

```bash
apt install librespeed-cli python3-paho-mqtt
install -d -m 0750 /etc/librespeed-mqtt
install -m 0640 config.toml.example /etc/librespeed-mqtt/config.toml
install -m 0700 librespeed_mqtt.py /usr/local/sbin/librespeed-mqtt
install -m 0644 librespeed-mqtt.service /etc/systemd/system/
install -m 0644 librespeed-mqtt.timer /etc/systemd/system/
install -m 0600 /dev/null /etc/librespeed-mqtt/mqtt-password
```

Set the broker address and MQTT username in `config.toml`, and put only the
password in `/etc/librespeed-mqtt/mqtt-password`. Run one test before scheduling:

```bash
/usr/local/sbin/librespeed-mqtt --config /etc/librespeed-mqtt/config.toml
```

Then enable the timer:

```bash
systemctl daemon-reload
systemctl enable --now librespeed-mqtt.timer
systemctl list-timers librespeed-mqtt.timer
```

The example pins LibreSpeed server 52 (New York) so historical measurements are
comparable. Change the pin deliberately if the endpoint is no longer reliable.

## MQTT topics

The wrapper publishes retained state under:

```
home/proxmox/prox2/librespeed/state
home/proxmox/prox2/librespeed/availability
home/proxmox/prox2/librespeed/error
```

Discovery definitions are published below `homeassistant/sensor/` by default.
An MQTT account should be limited to the state topic tree and its own discovery
topics when the broker supports ACLs.
