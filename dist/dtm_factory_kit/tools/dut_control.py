"""
DUT (Device Under Test) Ethernet Controller
===========================================

Target: 160.48.249.98 : 20000 (TCP)

Features
--------
1. Run a BT TX test:
       cd /opt/factory/rootfs/usr/bin
       ./bt_tx_test_39ch.sh
2. Reboot the DUT by sending the captured raw hex frame:
       4B 55 00 00 0B 00 01 00 80 50 00 00 00 00 FF 40 7E

Usage
-----
    python dut_control.py test       # run the BT TX 39ch test
    python dut_control.py reboot     # send reboot frame
    python dut_control.py shell "ls -al"   # ad-hoc shell command
    python dut_control.py gui        # tiny Tkinter GUI

Notes
-----
* If the device wraps EVERY command in the same KU-style binary frame
  (not just reboot), replace `_send_shell()` body with a frame builder
  using the same protocol fields.
* Default behaviour assumes a text shell over raw TCP (telnet-like).
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from typing import Iterable
import paramiko

DUT_HOST = "160.48.249.98"
DUT_PORT = 20000
CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 2.0

# Captured binary frames (KU protocol, terminated by 0x7E)
# [TOOL] Tool Start  -> enable service that accepts the reboot command (18 bytes)
SERVICE_ENABLE_FRAME = bytes.fromhex("4B 55 00 00 0C 00 01 00 01 00 00 00 01 00 01 5A 4D 7E".replace(" ", ""))
# [ICON] reboot                                                          (17 bytes)
REBOOT_FRAME         = bytes.fromhex("4B 55 00 00 0B 00 01 00 80 50 00 00 00 00 FF 40 7E".replace(" ", ""))

# Expected ACKs (length / cmd-id fields differ in the response).
# We don't strictly validate the bytes - we just wait for the first 0x7E framed reply.
FRAME_END = 0x7E
REPLY_TIMEOUT = 3.0  # seconds to wait for an ACK after sending a control frame

SSH_USER = "root"  # DUT 사용자명. 환경변수 DUT_SSH_USER 로 덮어쓸 수 있음.
SSH_USER_CANDIDATES = ("root", "ubuntu", "admin", "factory")


def _find_ssh_key() -> str:
    """Return path of an SSH private key next to this script.
    Prefers OpenSSH PEM; .ppk is NOT supported by paramiko."""
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("private_key.pem", "id_ed25519", "id_rsa"):
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    # Detect mis-placed .ppk and give a clearer hint.
    ppk = os.path.join(here, "private_key.ppk")
    if os.path.isfile(ppk):
        raise FileNotFoundError(
            f"Found PuTTY key '{ppk}' but paramiko cannot read .ppk.\n"
            f"Convert it to OpenSSH PEM (e.g. PuTTYgen -> Conversions -> "
            f"Export OpenSSH key) and save next to this script as "
            f"'private_key.pem'.")
    raise FileNotFoundError(
        f"No SSH private key found next to {here}. "
        f"Place an OpenSSH-format key as 'private_key.pem'.")


SSH_KEY_PATH = _find_ssh_key() if any(
    os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), n))
    for n in ("private_key.pem", "id_ed25519", "id_rsa")
) else os.path.join(os.path.dirname(os.path.abspath(__file__)), "private_key.pem")


# ---------------------------------------------------------------------------
# Low-level TCP helpers
# ---------------------------------------------------------------------------
def _connect(host: str = DUT_HOST, port: int = DUT_PORT) -> socket.socket:
    s = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    s.settimeout(RECV_TIMEOUT)
    return s


def _drain(sock: socket.socket, label: str = "RX") -> bytes:
    """Read whatever is in the receive buffer (best-effort)."""
    chunks: list[bytes] = []
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    except socket.timeout:
        pass
    payload = b"".join(chunks)
    if payload:
        try:
            print(f"[{label}] {payload.decode(errors='replace').rstrip()}")
        except Exception:
            print(f"[{label}] {payload.hex(' ')}")
    return payload


def _send_shell(sock: socket.socket, cmd: str) -> None:
    """Send a single shell command terminated by LF (text mode)."""
    line = (cmd.rstrip() + "\n").encode()
    print(f"[TX] {cmd}")
    sock.sendall(line)
    time.sleep(0.3)
    _drain(sock)


def _send_raw(sock: socket.socket, frame: bytes, label: str = "FRAME") -> None:
    print(f"[TX-{label}] {frame.hex(' ').upper()}")
    sock.sendall(frame)
    time.sleep(0.3)
    _drain(sock)


def _recv_frame(sock: socket.socket, label: str = "ACK", timeout: float = REPLY_TIMEOUT) -> bytes:
    """Read bytes until a 0x7E framing byte is seen (or timeout)."""
    deadline = time.time() + timeout
    buf = bytearray()
    old_to = sock.gettimeout()
    try:
        while time.time() < deadline:
            sock.settimeout(max(0.1, deadline - time.time()))
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf.extend(chunk)
            if FRAME_END in chunk:
                break
    finally:
        sock.settimeout(old_to)

    if buf:
        # Split a possible trailing ASCII log line for readability.
        end_idx = buf.find(FRAME_END)
        if end_idx >= 0:
            frame = bytes(buf[: end_idx + 1])
            trailer = bytes(buf[end_idx + 1 :])
            print(f"[RX-{label}] {frame.hex(' ').upper()}")
            if trailer.strip():
                print(f"[RX-{label}-TAIL] {trailer.decode(errors='replace').rstrip()}")
            return frame
        print(f"[RX-{label}] {bytes(buf).hex(' ').upper()}  (no 0x7E within {timeout:.1f}s)")
        return bytes(buf)
    print(f"[RX-{label}] <no response within {timeout:.1f}s>")
    return b""


def _send_and_wait(sock: socket.socket, frame: bytes, label: str) -> bytes:
    _send_raw(sock, frame, label=label)
    return _recv_frame(sock, label=label)


# ---------------------------------------------------------------------------
# High-level actions
# ---------------------------------------------------------------------------
class SSHSession:
    """Reusable paramiko SSH connection to the DUT.

    Designed to be created once by the GUI and reused across multiple
    commands so the TCP/SSH handshake (and DUT-side login) does not have
    to happen for every test iteration.
    """

    def __init__(self, host: str = DUT_HOST):
        self.host = host
        self.user: str | None = None
        self._cli: paramiko.SSHClient | None = None

    def _candidate_users(self) -> list[str]:
        env_user = os.environ.get("DUT_SSH_USER")
        if env_user:
            return [env_user]
        return [SSH_USER, *(u for u in SSH_USER_CANDIDATES if u != SSH_USER)]

    def is_alive(self) -> bool:
        if self._cli is None:
            return False
        tr = self._cli.get_transport()
        return bool(tr and tr.is_active())

    def connect(self, *, retries: int = 4, backoff: float = 2.0) -> None:
        """Open an SSH connection. If the DUT temporarily refuses auth
        (e.g. sshd just restarted after bt_test_off / reboot), retry a
        few times with exponential-ish backoff before giving up.
        """
        if self.is_alive():
            return
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            for user in self._candidate_users():
                print(f"[SSH] Connecting to {self.host} as {user} "
                      f"using key {SSH_KEY_PATH} (try {attempt}/{retries})")
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    cli.connect(self.host, username=user,
                                key_filename=SSH_KEY_PATH,
                                look_for_keys=False, allow_agent=False,
                                timeout=10)
                except paramiko.AuthenticationException as exc:
                    cli.close()
                    print(f"[SSH] auth failed for user '{user}' -> trying next")
                    last_exc = exc
                    continue
                except Exception as exc:
                    cli.close()
                    print(f"[SSH] connect error for '{user}': {exc}")
                    last_exc = exc
                    continue
                self._cli = cli
                self.user = user
                print(f"[SSH] connected as {user}")
                return
            # All users failed for this attempt: wait then retry. DUT sshd
            # often needs ~2-6s after bt_test_off/reboot before it accepts
            # key auth again.
            if attempt < retries:
                delay = backoff * attempt
                print(f"[SSH] all users failed (attempt {attempt}); "
                      f"retrying in {delay:.1f}s ...")
                time.sleep(delay)
        raise paramiko.AuthenticationException(
            f"SSH connect failed after {retries} attempts. Last error: {last_exc}")

    def exec(self, cmd: str, timeout: float = 30.0,
             retries: int = 2) -> tuple[int, str, str]:
        """Run a remote command and return (rc, stdout, stderr).

        Transparently reconnects + retries if the transport has died or
        the DUT temporarily rejected auth.
        """
        last_exc: Exception | None = None
        for attempt in range(1, retries + 2):  # 1 initial + `retries` retries
            try:
                if not self.is_alive():
                    self.connect()
                assert self._cli is not None
                print(f"[SSH] Executing: {cmd}")
                stdin, stdout, stderr = self._cli.exec_command(cmd, timeout=timeout)
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
                rc = stdout.channel.recv_exit_status()
                if out:
                    print(f"[SSH-STDOUT]\n{out.rstrip()}")
                if err:
                    print(f"[SSH-STDERR]\n{err.rstrip()}")
                return rc, out, err
            except Exception as exc:
                last_exc = exc
                print(f"[SSH] exec failed (attempt {attempt}): {exc}")
                # Force-reset the session so the next attempt reconnects.
                self.close()
                if attempt <= retries:
                    time.sleep(1.5 * attempt)
        raise RuntimeError(f"SSH exec failed after retries: {last_exc}")

    def close(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            except Exception:
                pass
        self._cli = None


def run_bt_tx_test(script: str = "bt_tx_test_39ch.sh",
                   session: "SSHSession | None" = None) -> None:
    """Run a BT TX test script via SSH (with private key authentication).

    If a persistent :class:`SSHSession` is supplied it is reused. Otherwise
    a one-shot SSH connection is opened (legacy CLI behaviour).
    """
    cmd = f"cd /opt/factory/rootfs/usr/bin && ./{script}"
    if session is not None:
        session.exec(cmd, timeout=60.0)
        print(f"[DONE] {script} dispatched via persistent SSH (user={session.user}).")
        return

    env_user = os.environ.get("DUT_SSH_USER")
    users = [env_user] if env_user else [SSH_USER, *(
        u for u in SSH_USER_CANDIDATES if u != SSH_USER)]

    last_exc: Exception | None = None
    for user in users:
        print(f"[SSH] Connecting to {DUT_HOST} as {user} using key {SSH_KEY_PATH}")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(DUT_HOST, username=user,
                        key_filename=SSH_KEY_PATH,
                        look_for_keys=False, allow_agent=False,
                        timeout=10)
        except paramiko.AuthenticationException as exc:
            ssh.close()
            print(f"[SSH] auth failed for user '{user}' -> trying next")
            last_exc = exc
            continue
        try:
            print(f"[SSH] Executing: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            out = stdout.read().decode()
            err = stderr.read().decode()
            if out:
                print(f"[SSH-STDOUT]\n{out}")
            if err:
                print(f"[SSH-STDERR]\n{err}")
        finally:
            ssh.close()
        print(f"[DONE] {script} dispatched via SSH (user={user}).")
        return

    raise paramiko.AuthenticationException(
        f"SSH auth failed for all users tried: {users}. "
        f"Set DUT_SSH_USER env var to the correct one. "
        f"Last error: {last_exc}")


def rx_test_start() -> None:
    """RX test start: DUT should TX so dongle can measure RX.
       -> run bt_tx_test_39ch.sh on the DUT (sample provided in /opt/factory/rootfs/usr/bin)."""
    print("[RX-START] Triggering DUT TX so the dongle can run its RX test...")
    run_bt_tx_test("bt_tx_test_39ch.sh")


def rx_test_end() -> None:
    """RX test end: stop DUT TX by rebooting the DUT (clean shutdown of bt_test)."""
    print("[RX-END] Stopping DUT TX by sending the reboot sequence...")
    reboot_dut()


def bt_test_off(session: "SSHSession | None" = None) -> None:
    """Stop the running BT TX test on the DUT by executing bt_test_off.sh
    in /opt/factory/rootfs/usr/bin via SSH. Used by 'End RX Test' so the
    DUT stops transmitting without a full reboot."""
    print("[TX-OFF] Stopping DUT TX via bt_test_off.sh ...")
    run_bt_tx_test("bt_test_off.sh", session=session)


# Remote DUT log files of interest.
DUT_LOG_DIR = "/var/data/btman"
DUT_LOG_FILES = ("bt_test.log", "bt_bootstrap.log")


def fetch_dut_logs(session: "SSHSession",
                   dest_dir: str,
                   files: tuple[str, ...] = DUT_LOG_FILES,
                   remote_dir: str = DUT_LOG_DIR,
                   retries: int = 2) -> list[str]:
    """Download DUT log files via SFTP. Returns the list of saved local paths.

    The SSH session is reconnected (with its own retry/backoff) if it has
    died, so this function can be called right after bt_test_off / reboot
    when sshd may briefly refuse auth.

    Failures for individual files are logged but do not raise, so the
    caller can still analyse whatever was retrieved.
    """
    os.makedirs(dest_dir, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            if not session.is_alive():
                session.connect()
            assert session._cli is not None
            sftp = session._cli.open_sftp()
            saved: list[str] = []
            try:
                for name in files:
                    remote = f"{remote_dir}/{name}"
                    local = os.path.join(dest_dir, name)
                    try:
                        sftp.get(remote, local)
                        print(f"[LOG] downloaded {remote} -> {local}")
                        saved.append(local)
                    except Exception as exc:
                        print(f"[LOG] could not download {remote}: {exc}")
            finally:
                try:
                    sftp.close()
                except Exception:
                    pass
            return saved
        except Exception as exc:
            last_exc = exc
            print(f"[LOG] fetch attempt {attempt} failed: {exc}")
            session.close()
            if attempt <= retries:
                time.sleep(1.5 * attempt)
    print(f"[LOG] giving up after {retries + 1} attempts: {last_exc}")
    return []


def dump_serial_driver(session: "SSHSession") -> str:
    """Run `cat /proc/tty/driver/serial` on the DUT and return its stdout.
    Prints the output to the GUI log."""
    rc, out, err = session.exec("cat /proc/tty/driver/serial", timeout=10.0)
    if rc != 0:
        print(f"[SERIAL] cat /proc/tty/driver/serial returned rc={rc}")
    return out


# Failure patterns we look for in the downloaded logs.
# Both text and binary representations of the firmware-crash pattern are
# included because the log files may be UTF-8 text or contain raw bytes.
CRASH_HEX = bytes.fromhex("FFFD010855 00".replace(" ", ""))
CRASH_PATTERNS = (
    # Hex bytes literally present in a binary log
    CRASH_HEX,
    # Same bytes rendered as ASCII hex with separating spaces (common log fmt)
    b"ff fd 01 08 55 00",
    b"FF FD 01 08 55 00",
)
TEXT_FAIL_PATTERNS = (
    b"[ERROR][BT]",          # bt_test.log error tag
    b"Fail, total boot time", # bt_bootstrap.log boot failure
)


def analyze_dut_logs(paths: list[str]) -> list[str]:
    """Scan downloaded DUT logs for known failure strings.

    Returns a list of human-readable findings (path: pattern: matching line)."""
    findings: list[str] = []
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            findings.append(f"{path}: read error: {exc}")
            continue

        # Text patterns - search per line so we can quote the line.
        lines = data.splitlines()
        for pat in TEXT_FAIL_PATTERNS:
            for ln in lines:
                if pat in ln:
                    try:
                        text = ln.decode(errors="replace").strip()
                    except Exception:
                        text = repr(ln)
                    findings.append(
                        f"{os.path.basename(path)}: '{pat.decode()}' -> {text}")
                    break  # one example per pattern is enough

        # Firmware-crash byte pattern: report once with byte offset.
        for pat in CRASH_PATTERNS:
            idx = data.find(pat)
            if idx >= 0:
                findings.append(
                    f"{os.path.basename(path)}: firmware crash signature "
                    f"({pat!r}) at offset {idx}")
                break
    return findings


def reboot_dut() -> None:
    """2. Enable the control service first, then send the reboot frame.

       Sequence captured from production tool:
         TX SERVICE_ENABLE (Tool Start)  -> wait ACK (4B 55 ... 7E)
         TX REBOOT                       -> wait ACK

       This function is tolerant to the DUT dropping the link mid-reboot:
       any TimeoutError / ConnectionError raised after the reboot frame
       has been written is treated as a successful reboot.
    """
    sent_reboot = False
    s = None
    try:
        s = _connect()
        try:
            _drain(s, "BANNER")
        except (socket.timeout, TimeoutError, OSError):
            pass
        try:
            ack1 = _send_and_wait(s, SERVICE_ENABLE_FRAME, label="SERVICE_ENABLE")
            if not ack1:
                print("[WARN] No ACK for SERVICE_ENABLE - proceeding anyway.")
        except (socket.timeout, TimeoutError, OSError) as exc:
            print(f"[WARN] SERVICE_ENABLE I/O error ({exc}); continuing.")
        try:
            sent_reboot = True
            ack2 = _send_and_wait(s, REBOOT_FRAME, label="REBOOT")
            if not ack2:
                print("[WARN] No ACK for REBOOT - the DUT may already be rebooting.")
        except (socket.timeout, TimeoutError, OSError) as exc:
            print(f"[INFO] Link dropped after REBOOT ({exc}) - this is expected.")
    except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        if sent_reboot:
            print(f"[INFO] Connection error after reboot frame ({exc}) - treating as success.")
        else:
            print(f"[ERR] Could not connect to DUT to send reboot: {exc}")
            raise
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    print("[DONE] reboot sequence sent. The DUT should restart now.")


def run_shell(cmds: Iterable[str]) -> None:
    """Run arbitrary shell commands in order."""
    with _connect() as s:
        _drain(s, "BANNER")
        for cmd in cmds:
            _send_shell(s, cmd)
        time.sleep(0.5)
        _drain(s, "TAIL")


# ---------------------------------------------------------------------------
# Optional GUI (small Tkinter window with two buttons)
# ---------------------------------------------------------------------------
def launch_gui() -> None:
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title(f"DUT Control - {DUT_HOST}:{DUT_PORT}")
    root.geometry("520x360")

    log = scrolledtext.ScrolledText(root, height=15)
    log.pack(fill="both", expand=True, padx=8, pady=8)

    def write(line: str) -> None:
        log.insert("end", line + "\n")
        log.see("end")
        root.update_idletasks()

    def safe(fn, label):
        try:
            write(f"--- {label} START ---")
            fn()
            write(f"--- {label} OK ---")
        except Exception as exc:  # pylint: disable=broad-except
            write(f"!!! {label} FAILED: {exc}")

    btn_frame = tk.Frame(root)
    btn_frame.pack(fill="x", padx=8, pady=4)
    tk.Button(btn_frame, text="RX Test START\n(DUT TX on)",
              command=lambda: safe(rx_test_start, "RX TEST START"),
              width=18, height=2).pack(side="left", padx=4)
    tk.Button(btn_frame, text="RX Test END\n(DUT reboot)",
              command=lambda: safe(rx_test_end, "RX TEST END"),
              width=18, height=2).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Run BT TX 39ch",
              command=lambda: safe(run_bt_tx_test, "BT TX TEST"),
              width=16).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Reboot DUT",
              command=lambda: safe(reboot_dut, "REBOOT"),
              width=12).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Quit",
              command=root.destroy, width=6).pack(side="right", padx=4)

    # Redirect prints to the log box.
    class _LogRedirector:
        def write(self, txt: str) -> int:
            if txt.strip():
                write(txt.rstrip())
            return len(txt)

        def flush(self) -> None:  # pragma: no cover
            pass

    sys.stdout = _LogRedirector()  # type: ignore[assignment]
    write(f"Connected target: {DUT_HOST}:{DUT_PORT}")
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DUT Ethernet Controller")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test", help="Run BT TX 39ch test on DUT.")
    sub.add_parser("reboot", help="Send the reboot frame to the DUT.")
    sub.add_parser("tx_off", help="Run bt_test_off.sh on DUT to stop BT TX.")
    sub.add_parser("rx_start", help="RX test START: run bt_tx_test_39ch.sh on DUT (DUT starts TX).")
    sub.add_parser("rx_end",   help="RX test END: reboot the DUT to stop TX.")
    p_shell = sub.add_parser("shell", help="Run an ad-hoc shell command.")
    p_shell.add_argument("command", nargs="+", help="Shell command(s) to execute.")
    sub.add_parser("gui", help="Launch a tiny Tk GUI with two buttons.")

    args = parser.parse_args(argv)

    if args.cmd == "test":
        run_bt_tx_test()
    elif args.cmd == "reboot":
        reboot_dut()
    elif args.cmd == "tx_off":
        bt_test_off()
    elif args.cmd == "rx_start":
        rx_test_start()
    elif args.cmd == "rx_end":
        rx_test_end()
    elif args.cmd == "shell":
        run_shell(args.command)
    elif args.cmd == "gui":
        launch_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
