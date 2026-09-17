from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
import yaml

log = logging.getLogger("vael-mux.kernel.mihomo")


class MihomoKernel:
    def __init__(self, binary: str, config_dir: str, controller: str, secret: str = ""):
        self.binary = binary
        self.config_dir = Path(config_dir)
        self.controller = controller
        self.secret = secret
        self.process: Optional[subprocess.Popen] = None

    @property
    def _base(self) -> str:
        return f"http://{self.controller}"

    @property
    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"} if self.secret else {}

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def write_config(self, cfg: Dict[str, Any]) -> Path:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        target = self.config_dir / "config.yaml"
        tmp = self.config_dir / "config.yaml.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        tmp.replace(target)
        return target

    def start(self) -> None:
        if self.is_running():
            return
        if not Path(self.binary).exists():
            raise FileNotFoundError(
                f"mihomo binary not found: {self.binary}. " "Place the mihomo binary there or adjust kernel.binary in config.yaml"
            )
        self.config_dir.mkdir(parents=True, exist_ok=True)
        log.info("starting mihomo: %s -d %s", self.binary, self.config_dir)
        self.process = subprocess.Popen(
            [self.binary, "-d", str(self.config_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )

    def stop(self) -> None:
        p = self.process
        if not p:
            return
        log.info("stopping mihomo (pid=%s)", p.pid)
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            else:
                p.terminate()
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass
        self.process = None

    async def reload(self, config_path: Path) -> None:
        async with aiohttp.ClientSession() as s:
            async with s.put(
                f"{self._base}/configs?force=true",
                headers=self._headers,
                json={"path": str(config_path)},
            ) as r:
                r.raise_for_status()

    async def proxies(self) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{self._base}/proxies", headers=self._headers) as r:
                r.raise_for_status()
                return await r.json()

    async def delay(self, name: str, url: str, timeout: int = 5000) -> Dict[str, Any]:
        params = {"url": url, "timeout": timeout}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{self._base}/proxies/{name}/delay",
                headers=self._headers,
                params=params,
            ) as r:
                return await r.json(content_type=None)

    async def switch(self, group: str, name: str) -> None:
        async with aiohttp.ClientSession() as s:
            async with s.put(
                f"{self._base}/proxies/{group}",
                headers=self._headers,
                json={"name": name},
            ) as r:
                r.raise_for_status()

    async def version(self) -> Optional[str]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self._base}/version", headers=self._headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        return data.get("version")
        except Exception:
            return None
        return None
