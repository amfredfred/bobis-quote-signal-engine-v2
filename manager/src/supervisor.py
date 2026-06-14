"""
supervisor.py — Spawn and supervise one signal-engine worker per source broker.

The manager injects these env vars into each subprocess so the engine needs
no manager section in its own config.yaml:

  MT5_USE         — broker profile name (matches mt5-credentials.yaml key)
  MANAGER_MODE    — "worker"
  MANAGER_URL     — ws://<engine_host>:<engine_port>
  MANAGER_TOKEN   — shared secret for EngineServer handshake
  MANAGER_SYMBOLS — comma-separated symbols to auto-subscribe
  USE_CONFIG      — absolute path to the shared signal-engine config.yaml

Each broker runs in its own working directory (signal-engine/run/<broker>/) so
logs, sessions, and charts are isolated per broker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_RESTART_DELAYS = [2, 5, 10, 30]  # seconds; last value repeats


class _Worker:
    def __init__(self, broker: str, cmd: list[str], env: dict, cwd: Path) -> None:
        self.broker = broker
        self.cmd = cmd
        self.env = env
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.restart_count = 0


class ProcessSupervisor:
    """
    Spawns one signal-engine subprocess per entry in `sources`.

    Call `await supervisor.start()` before the WS servers so engines are
    already connecting by the time the first gateway client arrives.
    Call `await supervisor.stop()` during shutdown to terminate all workers.
    """

    def __init__(
        self,
        sources: list[str],
        engine_path: Path,
        engine_config_name: str,
        manager_url: str,
        manager_token: str,
        manager_symbols: list[str],
    ) -> None:
        self._sources = sources
        self._engine_path = engine_path.resolve()
        # Absolute path to the shared config.yaml used by all engine workers.
        self._engine_config = (self._engine_path / engine_config_name).resolve()
        self._manager_url = manager_url
        self._manager_token = manager_token
        self._manager_symbols = manager_symbols
        self._workers: dict[str, _Worker] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        for broker in self._sources:
            cwd = self._engine_path / "run" / broker
            cwd.mkdir(parents=True, exist_ok=True)
            self._stop_orphan(cwd)
            worker = _Worker(
                broker=broker,
                # With signal-engine/src on PYTHONPATH, the CLI module is importable.
                cmd=[sys.executable, "-m", "interfaces.cli.main"],
                env=self._build_env(broker, cwd),
                cwd=cwd,
            )
            self._workers[broker] = worker
            task = asyncio.create_task(
                self._supervise(worker), name=f"supervisor-{broker}"
            )
            self._tasks.append(task)
        logger.info(
            "ProcessSupervisor started  sources=%s  engine=%s",
            list(self._sources),
            self._engine_path,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for worker in self._workers.values():
            if worker.process:
                try:
                    worker.process.terminate()
                except Exception:
                    pass
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("ProcessSupervisor stopped")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _build_env(self, broker: str, cwd: Path) -> dict:
        env = os.environ.copy()
        # Add signal-engine/src to PYTHONPATH so bare imports resolve correctly
        # when the subprocess cwd is the per-broker run directory.
        src_path = str(self._engine_path / "src")
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{src_path}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else src_path
        )
        env["USE_CONFIG"] = str(self._engine_config)
        env["MT5_USE"] = broker
        env["MANAGER_MODE"] = "worker"
        env["MANAGER_URL"] = self._manager_url
        env["MANAGER_TOKEN"] = self._manager_token
        if self._manager_symbols:
            env["MANAGER_SYMBOLS"] = ",".join(self._manager_symbols)
        return env

    async def _supervise(self, worker: _Worker) -> None:
        log_dir = worker.cwd / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        spawn_kwargs: dict = {}
        if sys.platform == "win32":
            import subprocess as _sp
            spawn_kwargs["creationflags"] = _sp.CREATE_NO_WINDOW

        while not self._stopping.is_set():
            log_fh = open(log_dir / "engine.log", "a", encoding="utf-8")
            try:
                worker.process = await asyncio.create_subprocess_exec(
                    *worker.cmd,
                    cwd=str(worker.cwd),
                    env=worker.env,
                    stdout=log_fh,
                    stderr=log_fh,
                    **spawn_kwargs,
                )
                logger.info(
                    "ProcessSupervisor: started  broker=%s  pid=%d  log=%s",
                    worker.broker, worker.process.pid, log_dir / "engine.log",
                )
                self._pid_path(worker.cwd).write_text(
                    str(worker.process.pid), encoding="ascii"
                )
                await worker.process.wait()
                rc = worker.process.returncode
            except Exception as exc:
                logger.error(
                    "ProcessSupervisor: failed to start broker=%s: %s",
                    worker.broker, exc,
                )
                rc = -1
            finally:
                self._pid_path(worker.cwd).unlink(missing_ok=True)
                try:
                    log_fh.close()
                except Exception:
                    pass

            if self._stopping.is_set():
                break

            logger.warning(
                "ProcessSupervisor: broker=%s exited rc=%d — restarting",
                worker.broker, rc,
            )
            delay = _RESTART_DELAYS[min(worker.restart_count, len(_RESTART_DELAYS) - 1)]
            worker.restart_count += 1
            logger.info(
                "ProcessSupervisor: restarting broker=%s in %ds (attempt %d)",
                worker.broker, delay, worker.restart_count,
            )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=float(delay))
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def _pid_path(cwd: Path) -> Path:
        return cwd / "manager-worker.pid"

    def _stop_orphan(self, cwd: Path) -> None:
        pid_path = self._pid_path(cwd)
        if not pid_path.exists():
            return
        try:
            pid = int(pid_path.read_text(encoding="ascii").strip())
            os.kill(pid, signal.SIGTERM)
            logger.warning("ProcessSupervisor: stopped orphan worker pid=%d", pid)
        except (OSError, ValueError):
            pass
        finally:
            pid_path.unlink(missing_ok=True)
