"""
heartbeat.py — Common heartbeat publisher for MQTT telemetry plane.

Created 2026-05-03. Used by every long-running agent to publish liveness
to the broker. Supplements (does NOT replace) the existing
retail_heartbeat.py + node_heartbeat.py mechanisms — those still write
to the monitor DB for the dashboard. MQTT heartbeats are additive and
serve the auditor's wildcard subscription pattern.

Topic shape: process/heartbeat/{node_type}/{agent_name}
  e.g. process/heartbeat/process/news_agent
       process/heartbeat/retail-1/trader_server

Payload (JSON): {
    "agent": "news_agent",
    "node": "process",
    "ts": "2026-05-03T14:30:00Z",
    "uptime_s": 12345,
    "pid": 1234,
    "extra": {...}            // agent-specific health hints
}

Last Will & Testament: each heartbeat publisher sets a will message on
its own topic so the broker auto-publishes "offline" if the client dies
without disconnecting cleanly. Auditor subscribes and treats sustained
"offline" as an alert condition.

Cadence: 30s default (configurable). Lightweight — JSON payload <200B.
"""

from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

NODE_TYPE = os.environ.get("NODE_TYPE", "unknown")        # process / retail-1 / company / ...
NODE_ID   = os.environ.get("NODE_ID",   NODE_TYPE)
DEFAULT_INTERVAL_S = 30


class HeartbeatPublisher:
    """Background thread that publishes a heartbeat every interval_s.
    Stops on stop() or when the process exits.

    Usage:
        from mqtt_client import MqttClient
        from heartbeat import HeartbeatPublisher

        mqtt = MqttClient(client_id="news_agent")
        if mqtt.connect():
            hb = HeartbeatPublisher(mqtt, agent="news_agent")
            hb.start()
            ...
            hb.stop()
            mqtt.disconnect()
    """

    def __init__(
        self,
        mqtt_client,
        agent: str,
        node: str = NODE_ID,
        interval_s: int = DEFAULT_INTERVAL_S,
        extra_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.mqtt = mqtt_client
        self.agent = agent
        self.node = node
        self.interval_s = interval_s
        self.extra_provider = extra_provider
        self.topic = f"process/heartbeat/{node}/{agent}"
        self._started_at = time.time()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background heartbeat thread. Daemon so it won't
        block process exit."""
        if self._thread is not None:
            return
        # Pre-publish immediately so subscribers see us right away,
        # then enter the periodic loop.
        self._publish_one()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.agent}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and publish a final 'offline' marker
        so subscribers don't have to wait for LWT to fire."""
        self._stop.set()
        try:
            self.mqtt.publish(self.topic, "offline", qos=0, retain=True)
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wait first so the immediate publish in start() doesn't
            # double-fire. Honors stop() promptly via Event.wait().
            if self._stop.wait(self.interval_s):
                return
            self._publish_one()

    def _publish_one(self) -> None:
        payload: dict[str, Any] = {
            "agent": self.agent,
            "node": self.node,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_s": int(time.time() - self._started_at),
            "pid": os.getpid(),
        }
        if self.extra_provider is not None:
            try:
                payload["extra"] = self.extra_provider() or {}
            except Exception as e:
                log.debug(f"[HB] extra_provider raised: {e}")
        try:
            self.mqtt.publish(self.topic, payload, qos=0, retain=True)
        except Exception as e:
            log.debug(f"[HB] publish raised: {e}")


def publish_one_shot(mqtt_client, agent: str, extra: dict | None = None) -> bool:
    """Convenience: send a single heartbeat without starting a background
    thread. Useful for short-lived scripts (cron jobs, one-shots)."""
    topic = f"process/heartbeat/{NODE_ID}/{agent}"
    payload = {
        "agent": agent,
        "node": NODE_ID,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pid": os.getpid(),
    }
    if extra:
        payload["extra"] = extra
    return mqtt_client.publish(topic, payload, qos=0, retain=True)
