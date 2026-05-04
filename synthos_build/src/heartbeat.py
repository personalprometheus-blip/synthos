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


# ─────────────────────────────────────────────────────────────────────────
# One-line agent integration — quick_start / quick_stop
# ─────────────────────────────────────────────────────────────────────────


def quick_start(agent_name: str, long_running: bool = True,
                extra_provider: Callable[[], dict[str, Any]] | None = None,
                interval_s: int = DEFAULT_INTERVAL_S):
    """Connect to broker + publish heartbeat with minimum boilerplate.

    For long-running agents (portals, daemons, listeners): pass
    long_running=True (default) to start a background HeartbeatPublisher
    that re-publishes every interval_s.

    For subprocess agents (trader, news, sentiment, screener — anything
    that exits after one cycle): pass long_running=False to send a single
    heartbeat. The mqtt connection is still returned in case the caller
    wants to publish more (e.g. price_poller doing a price publish in
    the same connection).

    Returns (mqtt_client, hb_publisher_or_None). Both may be None if the
    broker is unreachable — telemetry is strictly additive, so callers
    just continue without it. Pair with quick_stop() at shutdown.

    Usage:

        # long-running agent
        from heartbeat import quick_start, quick_stop
        mqtt, hb = quick_start("portal")
        try:
            run_portal()
        finally:
            quick_stop(mqtt, hb)

        # subprocess agent
        mqtt, _ = quick_start("news_agent", long_running=False)
        try:
            run_news_cycle()
        finally:
            quick_stop(mqtt, None)
    """
    try:
        from mqtt_client import MqttClient
    except ImportError:
        log.debug("[HB] mqtt_client unavailable — telemetry disabled")
        return (None, None)

    will_topic = f"process/heartbeat/{NODE_ID}/{agent_name}"
    mqtt = MqttClient(
        client_id=f"{NODE_ID}-{agent_name}-{os.getpid()}",
        last_will_topic=will_topic,
        last_will_payload="offline",
    )
    if not mqtt.connect():
        # Broker unreachable. Continue without telemetry.
        return (None, None)

    if long_running:
        hb = HeartbeatPublisher(
            mqtt, agent=agent_name, interval_s=interval_s,
            extra_provider=extra_provider,
        )
        hb.start()
        return (mqtt, hb)
    else:
        publish_one_shot(mqtt, agent_name)
        return (mqtt, None)


def quick_stop(mqtt_client, hb_publisher) -> None:
    """Companion to quick_start. Idempotent — safe to call even if either
    handle is None (the broker-unreachable case)."""
    try:
        if hb_publisher is not None:
            hb_publisher.stop()
    except Exception as e:
        log.debug(f"[HB] hb stop noise: {e}")
    try:
        if mqtt_client is not None:
            mqtt_client.disconnect()
    except Exception as e:
        log.debug(f"[HB] mqtt disconnect noise: {e}")


def register_telemetry(agent_name: str, long_running: bool = True,
                       extra_provider: Callable[[], dict[str, Any]] | None = None,
                       interval_s: int = DEFAULT_INTERVAL_S):
    """One-line agent telemetry. Connects at call time and registers an
    atexit handler that cleans up on normal interpreter shutdown.

    Returns the (mqtt, hb) tuple — most callers can ignore it. Agents
    that ALSO want to publish other topics (e.g. price_poller publishing
    quotes, regime_agent broadcasting regime) can use the returned mqtt
    to share the same connection.

    Usage in any agent:

        from heartbeat import register_telemetry
        register_telemetry("news_agent", long_running=False)

    or, if the agent wants the connection handle:

        _mqtt, _hb = register_telemetry("price_poller", long_running=True)

    Safe to call from anywhere — if the broker is down, returns
    (None, None) and the atexit hook is a no-op. Telemetry is strictly
    additive; agents continue working without it.
    """
    import atexit
    mqtt, hb = quick_start(agent_name, long_running=long_running,
                           extra_provider=extra_provider,
                           interval_s=interval_s)
    atexit.register(lambda: quick_stop(mqtt, hb))
    return (mqtt, hb)
