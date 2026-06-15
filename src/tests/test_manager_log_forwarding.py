import asyncio
import json
import logging

from interfaces.ws.manager_client import ManagerClient
from manager.src.server.engine_server import EngineServer


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


async def test_worker_log_handler_publishes_structured_log_record() -> None:
    client = ManagerClient("ws://manager", "token", "broker-a")
    client._loop = asyncio.get_running_loop()
    client._ws = _FakeWebSocket()
    client._ready.set()

    handler = client.create_log_handler()
    handler.emit(logging.LogRecord(
        name="signal_engine.scanner",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="scanner delayed",
        args=(),
        exc_info=None,
    ))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    message = json.loads(client._ws.messages[0])
    assert message["type"] == "log"
    assert message["event"] == "log.record"
    assert message["payload"]["level"] == "WARNING"
    assert message["payload"]["logger"] == "signal_engine.scanner"
    assert message["payload"]["message"] == "scanner delayed"


async def test_worker_logs_buffer_until_manager_connection_is_ready() -> None:
    client = ManagerClient("ws://manager", "token", "broker-a")
    client._loop = asyncio.get_running_loop()
    client.publish_log({"level": "INFO", "message": "starting worker"})
    await asyncio.sleep(0)

    websocket = _FakeWebSocket()
    client._ws = websocket
    client._ready.set()
    client._start_log_flush()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert json.loads(websocket.messages[0])["payload"]["message"] == "starting worker"


async def test_manager_tags_worker_log_with_broker_before_forwarding() -> None:
    forwarded: list[tuple[str, dict]] = []
    server = EngineServer(
        host="127.0.0.1",
        port=0,
        token="token",
        on_signal=lambda *_args: None,
        on_event=lambda event, payload: forwarded.append((event, payload)),
    )

    await server._on_message(
        "broker-a",
        _FakeWebSocket(),
        json.dumps({
            "type": "log",
            "event": "log.record",
            "payload": {"level": "INFO", "message": "worker ready"},
        }),
    )

    assert forwarded == [(
        "log.record",
        {"level": "INFO", "message": "worker ready", "broker": "broker-a"},
    )]
