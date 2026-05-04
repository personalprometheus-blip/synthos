"""
mqtt_client.py — Thin wrapper around paho-mqtt for the Synthos telemetry plane.

Created 2026-05-03. This file deliberately does NOT import paho at module
load time — paho is an optional runtime dependency, only required on nodes
that actually publish or subscribe. Import is deferred to connect().

Usage pattern:

    from mqtt_client import MqttClient

    client = MqttClient(
        host="10.0.0.11",
        port=1883,
        username=os.environ.get("MQTT_USER"),
        password=os.environ.get("MQTT_PASS"),
        client_id="price_poller",
    )
    client.connect()
    client.publish("process/prices/AAPL", {"bid": 200.0, "ask": 200.10}, qos=0, retain=True)
    client.disconnect()

Topic conventions (locked in orchestration_master_plan.md):
    process/prices/{ticker}              QoS 0, retained
    process/regime                       QoS 0, retained
    process/heartbeat/{node}/{agent}     QoS 0, LWT enabled
    _SYS/auditor/...                     auditor read-only

Deliberate non-features:
    - No automatic reconnect logic — paho's loop_start handles this. We
      surface connection failures so callers can degrade gracefully (e.g.
      price_poller falls back to writing only to the SQLite live_prices
      table).
    - No request/reply abstraction — that's HTTP's job. MQTT here is
      strictly fire-and-forget telemetry.
    - No QoS 2. The cost (4-leg handshake) outweighs the benefit for
      telemetry. Use QoS 1 for must-deliver, QoS 0 for everything else.
"""

from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# Re-exported defaults so callers can opt into the conventions easily.
DEFAULT_HOST = os.environ.get("MQTT_HOST", "10.0.0.11")
DEFAULT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DEFAULT_KEEPALIVE = 60   # seconds; broker pings every minute


class MqttClient:
    """Lazy paho wrapper. Safe to instantiate even on hosts without paho
    installed — failure surfaces only on connect()."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        username: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        keepalive: int = DEFAULT_KEEPALIVE,
        last_will_topic: str | None = None,
        last_will_payload: str = "offline",
    ):
        self.host = host
        self.port = port
        self.username = username or os.environ.get("MQTT_USER")
        self.password = password or os.environ.get("MQTT_PASS")
        self.client_id = client_id or f"synthos-{os.getpid()}"
        self.keepalive = keepalive
        self.last_will_topic = last_will_topic
        self.last_will_payload = last_will_payload
        self._client = None
        self._connected = False

    # ── Connection lifecycle ────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to broker. Returns True on success, False on any failure
        (including paho not installed). Logs the reason; never raises."""
        try:
            import paho.mqtt.client as paho_mqtt
        except ImportError:
            log.warning("[MQTT] paho-mqtt not installed — MQTT publish disabled")
            return False

        try:
            self._client = paho_mqtt.Client(client_id=self.client_id, clean_session=True)
            if self.username:
                self._client.username_pw_set(self.username, self.password)
            if self.last_will_topic:
                self._client.will_set(
                    self.last_will_topic, self.last_will_payload,
                    qos=0, retain=True,
                )
            self._client.on_connect    = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.connect(self.host, self.port, keepalive=self.keepalive)
            self._client.loop_start()
            # Brief wait for the connect callback to fire
            for _ in range(20):
                if self._connected:
                    return True
                time.sleep(0.05)
            log.warning("[MQTT] connect() timeout — broker did not ack within 1s")
            return False
        except Exception as e:
            log.warning(f"[MQTT] connect failed ({self.host}:{self.port}): {e}")
            self._client = None
            return False

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as e:
            log.debug(f"[MQTT] disconnect noise: {e}")
        finally:
            self._client = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    # ── Publish / subscribe ─────────────────────────────────────────────

    def publish(
        self,
        topic: str,
        payload: Any,
        qos: int = 0,
        retain: bool = False,
    ) -> bool:
        """Publish payload to topic. Payload may be:
          - str: sent as-is
          - bytes: sent as-is
          - anything else: JSON-serialized
        Returns True if paho accepted the publish, False otherwise. Note
        that QoS 0 always returns True even if the broker never receives
        the message — that's the trade-off."""
        if not self.is_connected:
            return False
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"))
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=retain)
            return info.rc == 0
        except Exception as e:
            log.warning(f"[MQTT] publish to {topic} failed: {e}")
            return False

    def subscribe(
        self,
        topic: str,
        callback: Callable[[str, bytes], None],
        qos: int = 0,
    ) -> bool:
        """Subscribe to topic (wildcards allowed). callback(topic, payload)
        is invoked for each message. Returns True on subscription accept."""
        if not self.is_connected:
            return False
        def _on_message(client, userdata, msg):
            try:
                callback(msg.topic, msg.payload)
            except Exception as e:
                log.warning(f"[MQTT] subscriber callback raised on {msg.topic}: {e}")
        self._client.message_callback_add(topic, _on_message)
        result, _mid = self._client.subscribe(topic, qos=qos)
        return result == 0

    # ── Internal callbacks ──────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            log.info(f"[MQTT] connected to {self.host}:{self.port} as {self.client_id}")
        else:
            self._connected = False
            log.warning(f"[MQTT] connect refused: rc={rc} — {self.host}:{self.port}")

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            log.warning(f"[MQTT] unexpected disconnect rc={rc}; paho will auto-retry")
        else:
            log.info("[MQTT] disconnected cleanly")


# ─────────────────────────────────────────────────────────────────────────
# Lazy module-level singleton publisher
# ─────────────────────────────────────────────────────────────────────────
# Used by agents that need to publish telemetry topics ALONGSIDE their
# normal DB writes (regime broadcasts, price updates, etc.) — they call
# get_publisher() at the dual-write site instead of opening a fresh
# connection each time. One connection per process, opened on first use.
#
# Sentinel values for _PUBLISHER:
#   None        — never tried; lazy-connect on first call
#   <client>    — connected, ready to publish
#   False       — tried and failed; subsequent calls return None silently
#                 (avoids per-call reconnect storms when broker is down)

_PUBLISHER: object = None


def get_publisher(client_id: str | None = None) -> "MqttClient | None":
    """Return a process-singleton MqttClient connected to the broker.
    Returns None if the broker is unreachable (silent — caller continues
    without telemetry; the dual-write SQLite path remains source of truth).
    Idempotent: subsequent calls reuse the same connection.

    Usage at a dual-write site:

        from mqtt_client import get_publisher
        m = get_publisher()
        if m is not None:
            m.publish('process/regime', {...}, qos=0, retain=True)
    """
    global _PUBLISHER
    if _PUBLISHER is False:
        # Already tried and failed this process — don't keep retrying
        return None
    if _PUBLISHER is not None:
        return _PUBLISHER  # type: ignore[return-value]
    cid = client_id or f"synthos-pub-{os.getpid()}"
    client = MqttClient(client_id=cid)
    if client.connect():
        _PUBLISHER = client
        # atexit so the connection gets a clean DISCONNECT on normal exit
        # rather than triggering LWT (which is reserved for genuine crashes)
        import atexit
        atexit.register(lambda: client.disconnect())
        return client
    else:
        _PUBLISHER = False
        return None
