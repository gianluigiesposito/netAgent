# tools/connection.py
from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Sequence

import telnetlib3
from scrapli.driver.core import AsyncIOSXEDriver

logger = logging.getLogger(__name__)

REAL_DEVICES = os.getenv("REAL_DEVICES", "false").lower() == "true"

# ── Vendor enum ───────────────────────────────────────────────────────────────

class Vendor(Enum):
    CISCO_IOS    = auto()   # Router IOS / IOU
    CISCO_SWITCH = auto()   # Switch IOS / IOU
    FRROUTING    = auto()   # FRRouting Linux container
    VPCS         = auto()   # GNS3 VPCS terminal
    UNKNOWN      = auto()

    @classmethod
    def from_str(cls, s: str) -> "Vendor":
        mapping = {
            "cisco_ios":    cls.CISCO_IOS,
            "cisco_switch": cls.CISCO_SWITCH,
            "frrouting":    cls.FRROUTING,
            "vpcs":         cls.VPCS,
        }
        return mapping.get(s.lower().strip(), cls.UNKNOWN)

    @property
    def is_cisco(self) -> bool:
        return self in (Vendor.CISCO_IOS, Vendor.CISCO_SWITCH)

    @property
    def is_linux(self) -> bool:
        return self == Vendor.FRROUTING


# ── Error detection ───────────────────────────────────────────────────────────

_ERROR_MARKERS: frozenset[str] = frozenset([
    "% unknown command",
    "% invalid input",
    "% incomplete command",
    "% ambiguous command",
    "error:",
    "command unknown",
])

def _contains_error(output: str) -> bool:
    lo = output.lower()
    return any(m in lo for m in _ERROR_MARKERS)


# ── Prompt patterns ───────────────────────────────────────────────────────────
#
# Cisco IOS prompt examples:
#   Switch>           Switch#           Switch(config)#
#   Router>           Router#           Router(config-if)#
#   IOU1>             IOU1#
#
# FRRouting vtysh examples:
#   R1#               R1(config)#       R1(config-router)#
#
# Linux shell examples:
#   root@r1:~#        admin@host:~$
#
# VPCS:
#   PC1>

# Matches any CLI prompt (>, #, $) that is NOT an auth prompt.
# Uses a lookahead to ensure we have at least one word-char before the symbol,
# and allows for optional IOS config context like (config-if).
_CLI_PROMPT_RE = re.compile(
    r'[\w\-]+(?:\([\w\-]+\))?\s*[>#\$]\s*$',
    re.MULTILINE,
)

# Auth prompts
_AUTH_USERNAME_RE = re.compile(r'(?:login|username)\s*[:\-]\s*$', re.IGNORECASE | re.MULTILINE)
_AUTH_PASSWORD_RE = re.compile(r'password\s*[:\-]\s*$',           re.IGNORECASE | re.MULTILINE)
_AUTH_PROMPT_RE   = re.compile(
    r'(?:login|username|password)\s*[:\-]\s*$',
    re.IGNORECASE | re.MULTILINE,
)

# IOS "Press RETURN to get started"
_PRESS_RETURN_RE = re.compile(r'press\s+return', re.IGNORECASE)

# GNS3 console log prompt
_GNS3_LOG_RE = re.compile(r"overwrite,\s*append,.*quit", re.IGNORECASE)

# IOS privileged-mode check
_PRIVILEGED_RE = re.compile(r'[\w\-]+(?:\([\w\-]+\))?\s*#\s*$', re.MULTILINE)

# IOS "confirm" prompt (write memory, reload, etc.)
_CONFIRM_RE = re.compile(r'\[confirm\]|\(y/n\)|proceed\?', re.IGNORECASE)


def _has_cli_prompt(buf: str) -> bool:
    """True if buf ends with a CLI prompt that is not an auth prompt."""
    return bool(_CLI_PROMPT_RE.search(buf)) and not bool(_AUTH_PROMPT_RE.search(buf))


# ── Device lock registry for GNS3/Telnet session serialization ────────────────

class DeviceLockRegistry:
    _locks: dict[str, asyncio.Lock] = {}
    _global_lock = asyncio.Lock()

    @classmethod
    async def get_lock(cls, device_name: str) -> asyncio.Lock:
        async with cls._global_lock:
            if device_name not in cls._locks:
                cls._locks[device_name] = asyncio.Lock()
            return cls._locks[device_name]


# ── Base Connection Contract ──────────────────────────────────────────────────

class BaseConnection(ABC):
    device_name: str | None = None
    _acquired_lock: asyncio.Lock | None = None

    @abstractmethod
    async def open(self) -> None: ...
    @abstractmethod
    async def close(self) -> None: ...
    @abstractmethod
    async def send_command(self, cmd: str, timeout: float = 1.5) -> str: ...
    @abstractmethod
    async def save_config(self) -> bool: ...

    @property
    def current_prompt(self) -> str:
        return getattr(self, "_last_prompt", "")

    async def __aenter__(self):
        if getattr(self, "device_name", None):
            lock = await DeviceLockRegistry.get_lock(self.device_name)
            await lock.acquire()
            self._acquired_lock = lock
        try:
            await self.open()
        except Exception:
            if self._acquired_lock:
                self._acquired_lock.release()
                self._acquired_lock = None
            raise
        return self

    async def __aexit__(self, *_):
        try:
            await self.close()
        finally:
            if getattr(self, "_acquired_lock", None):
                self._acquired_lock.release()
                self._acquired_lock = None


# ── SSH via Scrapli ───────────────────────────────────────────────────────────

class AsyncSSHConnection(BaseConnection):
    def __init__(self, host: str, username: str, password: str, port: int = 22):
        self._host = host
        self._last_prompt = ""
        self._conn = AsyncIOSXEDriver(
            host=host,
            port=port,
            auth_username=username,
            auth_password=password,
            auth_strict_key=False,
            timeout_ops=30,
            timeout_transport=30,
        )

    async def open(self) -> None:
        await self._conn.open()

    async def close(self) -> None:
        await self._conn.close()

    async def send_command(self, cmd: str, timeout: float = 1.5) -> str:
        response = await self._conn.send_command(f'vtysh -c "{cmd}"')
        output = response.result
        if _contains_error(output):
            logger.warning("[%s] CLI error on '%s': %s", self._host, cmd, output.strip())
        return output

    async def save_config(self) -> bool:
        logger.info("[%s] Saving configuration to NVRAM...", self._host)
        out = await self.send_command("write memory")
        lo  = out.lower()
        return any(s in lo for s in ("ok", "building", "bytes copied", "integrated"))


# ── Async Telnet Connection ───────────────────────────────────────────────────

class AsyncTelnetConnection(BaseConnection):
    """
    Vendor-aware Telnet driver for GNS3/IOU consoles.

    Behaviours per vendor:
      CISCO_IOS / CISCO_SWITCH:
        - Sends a single \r to wake the console before any auth loop.
        - Reads and reacts to "Press RETURN to get started".
        - Enters privileged exec (enable) if we land on '>'.
        - Disables terminal pagination (terminal length 0).
      FRROUTING:
        - After SSH-shell prompt, injects `vtysh`.
      VPCS:
        - No auth, just waits for the '>' prompt.
    """

    def __init__(
        self,
        host:     str,
        port:     int,
        vendor:   Vendor        = Vendor.UNKNOWN,
        username: str           = "admin",
        password: str           = "cisco",
        enable_password: str    = "",
    ):
        self._host            = host
        self._port            = port
        self._vendor          = vendor
        self._username        = username
        self._password        = password
        self._enable_password = enable_password or password  # fallback to login password
        self._reader: asyncio.StreamReader | None = None
        self._writer = None
        self._tag    = f"{host}:{port}"
        self._last_prompt     = ""
        self._last_cmd        = ""

    # ── Low-level I/O ─────────────────────────────────────────────────────────

    async def _drain_buffer(self, window: float = 0.3) -> str:
        """Read everything available in the next `window` seconds; don't block."""
        buf = ""
        deadline = asyncio.get_running_loop().time() + window
        while asyncio.get_running_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=0.1)
                if not chunk:
                    break
                buf += chunk
            except asyncio.TimeoutError:
                break
        return buf

    async def _read_until_prompt(self, timeout: float = 5.0) -> str:
        """
        Read until a CLI or auth prompt is detected, or timeout expires.
        Returns whatever was buffered.
        """
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        buf      = ""

        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=min(0.4, remaining)
                )
                if not chunk:
                    break
                buf += chunk
                # Stop as soon as we see any known prompt shape
                if _CLI_PROMPT_RE.search(buf) or _AUTH_PROMPT_RE.search(buf):
                    # Give the terminal a tiny window to flush the rest of the line
                    await asyncio.sleep(0.05)
                    extra = await self._drain_buffer(0.1)
                    buf += extra
                    return buf
            except asyncio.TimeoutError:
                # Even on timeout, return what we have if it looks like a prompt
                if _CLI_PROMPT_RE.search(buf) or _AUTH_PROMPT_RE.search(buf):
                    return buf

        return buf

    def _write(self, data: str) -> None:
        if self._writer:
            self._writer.write(data)

    async def _drain(self) -> None:
        if self._writer:
            try:
                await self._writer.drain()
            except Exception:
                pass

    async def _writeline(self, data: str, delay: float = 0.3) -> None:
        self._write(data + "\r\n")
        await self._drain()
        await asyncio.sleep(delay)

    # ── Auth state machine ────────────────────────────────────────────────────

    async def _handle_prompt(self, buf: str) -> bool:
        """
        React to whatever is in `buf`.  Returns True if we handled something
        (caller should read again), False if nothing more to do.
        """
        # GNS3 console log prompt
        if _GNS3_LOG_RE.search(buf):
            logger.debug("[%s] GNS3 log prompt detected — sending 'D'", self._tag)
            await self._writeline("D")
            return True

        # "Press RETURN to get started" (IOS post-boot banner)
        if _PRESS_RETURN_RE.search(buf):
            logger.debug("[%s] 'Press RETURN' banner detected — sending \\r", self._tag)
            self._write("\r\n")
            await self._drain()
            await asyncio.sleep(0.5)
            return True

        # Username prompt
        if _AUTH_USERNAME_RE.search(buf):
            logger.info("[%s] Username prompt — sending '%s'", self._tag, self._username)
            await self._writeline(self._username)
            return True

        # Password prompt
        if _AUTH_PASSWORD_RE.search(buf):
            logger.info("[%s] Password prompt — sending credentials", self._tag)
            await self._writeline(self._password)
            return True

        return False

    # ── Boot synchronisation ──────────────────────────────────────────────────

    async def _boot_wait(self, max_cycles: int = 40) -> None:
        """
        Synchronise with the device console after TCP connect.

        Strategy:
          1. Drain whatever the terminal already sent (banner, MOTD, …).
          2. Send a single wake character (\r — preferred over \n for Cisco).
          3. Loop, handling auth prompts, until we see a real CLI prompt.
        """
        logger.debug("[%s] Waiting for console to be ready...", self._tag)

        # Step 1 — drain initial banner / MOTD / garbage
        initial = await self._drain_buffer(window=1.5)
        logger.debug("[%s] Initial buffer: %r", self._tag, initial[:200])

        # Step 2 — send a single wake-up character without a newline first,
        #           then read back to see where we are.
        self._write("\r")
        await self._drain()
        await asyncio.sleep(0.3)

        login_attempts_count = 0
        for cycle in range(max_cycles):
            out = await self._read_until_prompt(timeout=3.0)
            logger.debug("[%s] boot_wait cycle %d — got: %r", self._tag, cycle, out[-120:])

            if login_attempts_count > 0 and (
                "login invalid" in out.lower()
                or "access denied" in out.lower()
                or "login incorrect" in out.lower()
            ):
                raise PermissionError(
                    f"[{self._tag}] Accesso negato: credenziali Telnet non valide (Username/Password errati)."
                )

            # Se stiamo per inviare la password, incrementiamo il contatore dei tentativi
            if _AUTH_PASSWORD_RE.search(out):
                login_attempts_count += 1

            # Handle any auth / banner prompts iteratively
            handled = await self._handle_prompt(out)
            if handled:
                continue

            # We have a real CLI prompt — done
            if _has_cli_prompt(out):
                m = _CLI_PROMPT_RE.search(out)
                if m:
                    self._last_prompt = m.group(0).strip()
                logger.debug("[%s] Console synchronised at cycle %d.", self._tag, cycle)
                return

            # Nothing useful yet — nudge the terminal
            if cycle % 5 == 0:
                logger.debug("[%s] No prompt yet, nudging terminal...", self._tag)
                self._write("\r")
                await self._drain()
                await asyncio.sleep(0.5)
            elif cycle % 15 == 0:
                # Harder reset if really stuck (e.g., IOS hung on --More--)
                logger.debug("[%s] Sending Ctrl-C to break any stuck output...", self._tag)
                self._write("\x03")
                await self._drain()
                await asyncio.sleep(0.3)

        raise TimeoutError(
            f"[{self._tag}] Console prompt not detected after {max_cycles} cycles. "
            "Possible cause: AAA lockout, boot loop, or wrong port."
        )

    # ── Post-login vendor setup ───────────────────────────────────────────────

    async def _vendor_setup(self) -> None:
        """Apply vendor-specific post-login initialisation."""
        if self._vendor.is_cisco:
            await self._cisco_setup()
        elif self._vendor.is_linux:
            await self._frr_setup()
        # VPCS needs nothing

    async def _cisco_setup(self) -> None:
        # Probe the current prompt to check privilege level
        self._write("\r")
        await self._drain()
        probe = await self._read_until_prompt(timeout=2.0)
        m = _CLI_PROMPT_RE.search(probe)
        if m:
            self._last_prompt = m.group(0).strip()

        # Se siamo in modalità di configurazione (es. sessione precedente non chiusa pulitamente), torniamo in EXEC mode
        if "(config" in probe:
            logger.info("[%s] Config mode detected in setup — exiting to privileged EXEC mode (end)...", self._tag)
            self._write("end\r\n")
            await self._drain()
            await asyncio.sleep(0.5)
            # Rinfresca il prompt
            self._write("\r")
            await self._drain()
            probe = await self._read_until_prompt(timeout=2.0)
            m = _CLI_PROMPT_RE.search(probe)
            if m:
                self._last_prompt = m.group(0).strip()

        # If we're in user exec ('>'), escalate to privileged exec
        if re.search(r'[\w\-]+\s*>\s*$', probe, re.MULTILINE):
            logger.info("[%s] User exec detected — entering privileged mode (enable)...", self._tag)
            await self._writeline("enable")
            enable_out = await self._read_until_prompt(timeout=3.0)
            m = _CLI_PROMPT_RE.search(enable_out)
            if m:
                self._last_prompt = m.group(0).strip()

            if _AUTH_PASSWORD_RE.search(enable_out):
                logger.debug("[%s] Enable password required.", self._tag)
                await self._writeline(self._enable_password)
                out2 = await self._read_until_prompt(timeout=3.0)
                m = _CLI_PROMPT_RE.search(out2)
                if m:
                    self._last_prompt = m.group(0).strip()

        # Disable --More-- pagination so long outputs don't stall
        logger.debug("[%s] Disabling terminal pagination.", self._tag)
        await self._raw_send("terminal length 0", timeout=2.0)

    async def _frr_setup(self) -> None:
        # Probe for Linux shell prompt; inject vtysh if needed
        self._write("\r")
        await self._drain()
        probe = await self._read_until_prompt(timeout=2.0)
        m = _CLI_PROMPT_RE.search(probe)
        if m:
            self._last_prompt = m.group(0).strip()

        # Se siamo in configurazione vtysh (es. R1(config)#), torniamo in EXEC mode
        if "(config" in probe:
            logger.info("[%s] FRR Config mode detected in setup — exiting to vtysh EXEC mode (end)...", self._tag)
            await self._writeline("end")
            probe = await self._read_until_prompt(timeout=2.0)
            m = _CLI_PROMPT_RE.search(probe)
            if m:
                self._last_prompt = m.group(0).strip()

        if "~" in probe or "frr:" in probe.lower() or re.search(r'\$\s*$', probe):
            logger.info("[%s] Linux shell detected — injecting 'vtysh' con pager disabilitato...", self._tag)
            await self._writeline("export VTYSH_PAGER=cat && vtysh")
            out2 = await self._read_until_prompt(timeout=3.0)
            m = _CLI_PROMPT_RE.search(out2)
            if m:
                self._last_prompt = m.group(0).strip()

    # ── Public interface ──────────────────────────────────────────────────────

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                telnetlib3.open_connection(self._host, self._port),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"[{self._tag}] TCP connect timeout after 15s — "
                f"host {self._host}:{self._port} unreachable."
            )
        await self._boot_wait()
        await self._vendor_setup()

    async def close(self) -> None:
        if self._writer:
            try:
                # Se è FRR o Cisco, cerchiamo di uscire in modo pulito per liberare il terminale ed evitare processi zombie
                if self._vendor == Vendor.FRROUTING:
                    # Invia exit per uscire da vtysh, e poi da bash
                    self._writer.write(b"\nend\nexit\nexit\n")
                    await self._writer.drain()
                    await asyncio.sleep(0.05)
                elif self._vendor.is_cisco:
                    # Invia end ed exit per chiudere la sessione di console
                    self._writer.write(b"\nend\nexit\n")
                    await self._writer.drain()
                    await asyncio.sleep(0.05)
            except Exception:
                pass
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

    async def _raw_send(self, cmd: str, timeout: float = 1.5) -> str:
        """
        Send a single command and collect the response.
        Handles IOS --More-- pagination and [confirm] prompts automatically.
        """
        if not self._writer:
            raise ConnectionError(f"[{self._tag}] Connection not open.")

        # Pacing delay: sleep 50ms if the previous command was "exit"
        if getattr(self, "_last_cmd", "") in ("exit", "end"):
            await asyncio.sleep(0.05)

        # Flush stale data from the receive buffer
        await self._drain_buffer(window=0.15)

        clean_cmd = cmd.strip()
        self._last_cmd = clean_cmd.lower()
        self._write(clean_cmd + "\r\n")
        await self._drain()

        # Selective pacing delay for RSA key generation
        is_rsa = "crypto key generate rsa" in clean_cmd.lower()
        if is_rsa:
            logger.info("[%s] Rilevato 'crypto key generate rsa' — attesa 5 secondi per la generazione delle chiavi...", self._tag)
            await asyncio.sleep(5.0)

        loop     = asyncio.get_running_loop()
        effective_timeout = timeout + 5.0 if is_rsa else timeout
        deadline = loop.time() + effective_timeout
        raw      = ""

        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                chunk = await asyncio.wait_for(
                    self._reader.read(4096), timeout=min(0.3, remaining)
                )
                if not chunk:
                    break
                raw += chunk

                # Auto-page: send space to dismiss --More--
                m = re.search(r'--\s*[Mm]ore\s*--', raw)
                if m:
                    raw = raw[:m.start()] + raw[m.end():]
                    self._write(" ")
                    await self._drain()
                    await asyncio.sleep(0.1)
                    continue

                # Auto-confirm: hit Enter on [confirm] or (y/n) prompts
                if _CONFIRM_RE.search(raw):
                    logger.debug("[%s] Confirm prompt detected for '%s' — sending \\r", self._tag, clean_cmd)
                    self._write("\r\n")
                    await self._drain()
                    await asyncio.sleep(0.2)
                    continue

                if _has_cli_prompt(raw):
                    m = _CLI_PROMPT_RE.search(raw)
                    if m:
                        self._last_prompt = m.group(0).strip()
                    break
            except asyncio.TimeoutError:
                if _has_cli_prompt(raw):
                    m = _CLI_PROMPT_RE.search(raw)
                    if m:
                        self._last_prompt = m.group(0).strip()
                    break

        # Strip the echoed command and trailing prompt from the output
        lines = raw.splitlines()
        clean_lines = [
            l for l in lines
            if clean_cmd not in l and not _CLI_PROMPT_RE.search(l)
        ]
        clean = "\n".join(clean_lines).strip()

        if _contains_error(clean):
            logger.debug("[%s] CLI error for '%s': %s", self._tag, clean_cmd, clean)

        return clean

    async def send_command(self, cmd: str, timeout: float = 1.5) -> str:
        return await self._raw_send(cmd, timeout=timeout)

    async def save_config(self) -> bool:
        logger.info("[%s] Saving configuration...", self._tag)

        # Exit any config sub-mode first
        await self._raw_send("end", timeout=2.0)
        await asyncio.sleep(0.2)

        out = await self._raw_send("write memory", timeout=20.0)
        lo  = out.lower()

        success_markers = (
            "ok",
            "building configuration",
            "integrated configuration saved",
            "bytes copied",
            "[ok]",
        )
        success = any(m in lo for m in success_markers)

        # IOS sometimes returns nothing on success — treat no error as success
        if not success and not _contains_error(out):
            success = True

        if success:
            logger.info("[%s] Configuration saved successfully.", self._tag)
        else:
            logger.warning("[%s] save_config uncertain — output: %s", self._tag, out.strip())

        return success


# ── Factory ───────────────────────────────────────────────────────────────────

def get_connection(device_config: dict) -> BaseConnection:
    """
    Build a connection object from a device inventory entry.

    Expected keys (all read from devices.yaml):
      connection_type : ssh | telnet | cisco_telnet | vpcs_telnet
      host            : str
      port            : int
      vendor          : frrouting | cisco_ios | cisco_switch | vpcs
      username        : str  (optional)
      password        : str  (optional)
      enable_password : str  (optional, falls back to password)
    """
    # ── Risoluzione dinamica delle credenziali a runtime (SecOps) ──
    device_name = None
    try:
        from pathlib import Path
        import yaml
        path = Path("config/devices.yaml")
        if path.exists():
            with open(path) as f:
                inv = yaml.safe_load(f) or {}
            for name, cfg in inv.items():
                if cfg.get("host") == device_config.get("host") and int(cfg.get("port", 0)) == int(device_config.get("port", 0)):
                    device_name = name
                    break
    except Exception:
        pass

    # Risoluzione password principale
    password = None
    if device_name:
        password = os.getenv(f"NETAGENT_DEV_PASSWORD_{device_name.upper()}")
    if not password:
        password = os.getenv("NETAGENT_DEV_PASSWORD_DEFAULT")
    if not password:
        password = device_config.get("password")

    # Risoluzione enable_password
    enable_password = None
    if device_name:
        enable_password = os.getenv(f"NETAGENT_DEV_ENABLE_PASSWORD_{device_name.upper()}")
    if not enable_password:
        enable_password = os.getenv("NETAGENT_DEV_ENABLE_PASSWORD_DEFAULT")
    if not enable_password:
        enable_password = device_config.get("enable_password")

    conn_type = device_config.get("connection_type", "ssh").lower()
    vendor    = Vendor.from_str(device_config.get("vendor", ""))

    conn = None
    if conn_type == "ssh":
        conn = AsyncSSHConnection(
            host     = device_config["host"],
            username = device_config.get("username", "frr"),
            password = password or "frr",
            port     = int(device_config.get("port", 22)),
        )

    elif conn_type in ("telnet", "vpcs_telnet", "cisco_telnet"):
        # Derive sensible defaults from the vendor when credentials are absent
        if vendor.is_cisco:
            default_user, default_pass = "admin", "cisco"
        elif vendor == Vendor.FRROUTING:
            default_user, default_pass = "frr", "frr"
        else:
            default_user, default_pass = "", ""

        conn = AsyncTelnetConnection(
            host            = device_config["host"],
            port            = int(device_config["port"]),
            vendor          = vendor,
            username        = device_config.get("username", default_user),
            password        = password or default_pass,
            enable_password = enable_password or "",
        )

    if conn:
        conn.device_name = device_name
        return conn

    raise ValueError(f"Unsupported connection_type: '{conn_type}'")
