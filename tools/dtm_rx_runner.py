"""
DTM RX Runner - All-in-one tester
=================================

This GUI replaces the nRF Connect "Direct Test Mode" desktop app for the
RX measurement workflow. It talks to the nRF52840 dongle directly over
its CDC-ACM port using the Bluetooth DTM 2-wire protocol, and at the
same time drives the DUT through `dut_control.py`:

  [START RX TEST] button:
    1) Run `python dut_control.py test` (SSH -> bt_tx_test_39ch.sh on DUT)
    2) Send DTM "Receiver Test" command to the dongle

  [END RX TEST] button:
    1) Send DTM "Test End" command, read packet-count event from dongle
    2) Save result to D:\factory\YY-MM-DD\rx_result.csv
    3) Run `python dut_control.py reboot` (TCP reboot frame to DUT)

DTM 2-wire protocol summary (Bluetooth Core, Vol 6, Part F):
  - 2-byte command from tester -> DUT
      byte0[7:6] = CMD  (00=Reset, 01=RX, 10=TX, 11=End)
      byte0[5:0] = freq (channel 0..39, f = 2402 + 2*N MHz)
      byte1[7:2] = length, byte1[1:0] = PKT
  - 2-byte event from DUT  -> tester
      byte0[7]=0 -> status event (byte0[0]=0 success / 1 error)
      byte0[7]=1 -> packet-reporting event, count = ((b0 & 0x7F)<<8) | b1
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import subprocess
import sys
import threading
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from tkinter import messagebox, scrolledtext, ttk

import serial
import serial.tools.list_ports

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DUT_CONTROL = os.path.join(SCRIPT_DIR, "dut_control.py")
RESULT_BASE = r"D:\factory"

# Import DUT helpers as a library so we can share one persistent SSH session
# across all GUI actions instead of spawning a subprocess every time.
sys.path.insert(0, SCRIPT_DIR)
import dut_control  # noqa: E402  (after sys.path tweak)

# ----- DTM 2-wire encoding ----------------------------------------------------
CMD_RESET, CMD_RECEIVER, CMD_TRANSMITTER, CMD_END = 0b00, 0b01, 0b10, 0b11
PKT_PRBS9 = 0b00


def build_cmd(cmd: int, freq: int = 0, length: int = 0, pkt: int = 0) -> bytes:
    b0 = ((cmd & 0x3) << 6) | (freq & 0x3F)
    b1 = ((length & 0x3F) << 2) | (pkt & 0x3)
    return bytes([b0, b1])


def parse_event(resp: bytes):
    if len(resp) < 2:
        return None
    b0, b1 = resp[0], resp[1]
    if b0 & 0x80:
        return ("packet_count", ((b0 & 0x7F) << 8) | b1)
    return ("status", b0 & 0x01)


# ----- GUI --------------------------------------------------------------------
class DtmRxRunner:
    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.test_index = 0
        self.ssh = dut_control.SSHSession()

        self.root = tk.Tk()
        self.root.title("DTM RX Runner (DUT + Dongle)")
        self.root.geometry("760x600")
        self._build_ui()

    # ----- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        top = tk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=4)

        tk.Label(top, text="Dongle COM:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=10)
        self.port_cb.pack(side="left", padx=4)
        tk.Button(top, text="Refresh", command=self.refresh_ports).pack(side="left", padx=2)
        tk.Button(top, text="Open",    command=self.open_port).pack(side="left", padx=2)

        tk.Label(top, text="   Channel:").pack(side="left")
        self.ch_var = tk.IntVar(value=19)
        tk.Spinbox(top, from_=0, to=39, textvariable=self.ch_var, width=4).pack(side="left")

        tk.Label(top, text="   Length:").pack(side="left")
        self.len_var = tk.IntVar(value=37)
        tk.Spinbox(top, from_=0, to=37, textvariable=self.len_var, width=4).pack(side="left")

        # Standalone (dongle-only) mode: skip ALL DUT / SSH / Ethernet calls.
        # Rendered as an ON/OFF "switch" style button.
        self.standalone_var = tk.BooleanVar(value=False)
        tk.Label(top, text="   Mode:").pack(side="left", padx=(12, 0))
        self.mode_switch = tk.Button(top, width=18, relief="raised", bd=2,
                                     command=self._toggle_standalone)
        self.mode_switch.pack(side="left", padx=2)
        self._render_mode_switch()

        btns = tk.Frame(self.root)
        btns.pack(fill="x", padx=8, pady=6)
        tk.Button(btns, text="START RX TEST", bg="#5dbf60", fg="white",
                  font=("Arial", 11, "bold"), width=18, height=2,
                  command=self.on_start).pack(side="left", padx=4)
        tk.Button(btns, text="END RX TEST", bg="#e07070", fg="white",
                  font=("Arial", 11, "bold"), width=18, height=2,
                  command=self.on_end).pack(side="left", padx=4)
        self.reboot_btn = tk.Button(btns, text="REBOOT DUT", bg="#d28a3a",
                                    fg="white", font=("Arial", 10, "bold"),
                                    width=12, height=2,
                                    command=self.on_reboot)
        self.reboot_btn.pack(side="left", padx=4)
        tk.Button(btns, text="DTM Reset", width=10,
                  command=self.on_reset).pack(side="left", padx=4)
        tk.Button(btns, text="Open CSV folder", width=16,
                  command=self._open_today_folder).pack(side="left", padx=4)

        # Auto loop row
        auto_row = tk.Frame(self.root)
        auto_row.pack(fill="x", padx=8, pady=2)
        tk.Label(auto_row, text="Iterations:").pack(side="left")
        self.iter_var = tk.IntVar(value=10)
        tk.Spinbox(auto_row, from_=1, to=10000, textvariable=self.iter_var,
                   width=6).pack(side="left", padx=2)
        tk.Label(auto_row, text="RX duration (s):").pack(side="left", padx=(8, 0))
        self.dur_var = tk.IntVar(value=10)
        tk.Spinbox(auto_row, from_=1, to=600, textvariable=self.dur_var,
                   width=5).pack(side="left", padx=2)
        tk.Label(auto_row, text="Cooldown (s):").pack(side="left", padx=(8, 0))
        self.cool_var = tk.IntVar(value=30)
        tk.Spinbox(auto_row, from_=0, to=600, textvariable=self.cool_var,
                   width=5).pack(side="left", padx=2)
        self.auto_btn = tk.Button(auto_row, text="AUTO RUN", bg="#3a7bd5",
                                  fg="white", font=("Arial", 10, "bold"),
                                  width=12, command=self.on_auto)
        self.auto_btn.pack(side="left", padx=8)
        self.auto_nr_btn = tk.Button(auto_row, text="AUTO RUN\n(no reboot)",
                                     bg="#1f8f5d", fg="white",
                                     font=("Arial", 9, "bold"),
                                     width=14, command=self.on_auto_no_reboot)
        self.auto_nr_btn.pack(side="left", padx=2)
        self.stop_btn = tk.Button(auto_row, text="STOP", bg="#888", fg="white",
                                  width=6, state="disabled",
                                  command=self.on_stop)
        self.stop_btn.pack(side="left", padx=2)
        self.plot_btn = tk.Button(auto_row, text="Plot CSV", width=10,
                                  command=self.on_plot)
        self.plot_btn.pack(side="left", padx=8)

        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None

        self.log = scrolledtext.ScrolledText(self.root, height=24)
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

        self.refresh_ports()
        # Apply initial mode state (DUT-linked by default).
        self._on_mode_change()

    def write(self, msg: str) -> None:
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.root.update_idletasks()

    # ----- COM port management ----------------------------------------------
    def refresh_ports(self) -> None:
        ports = list(serial.tools.list_ports.comports())
        self.port_cb["values"] = [p.device for p in ports]
        # try to auto-select the Nordic DTM USB
        for p in ports:
            desc = (p.description or "") + " " + (p.manufacturer or "")
            if "Nordic" in desc or "DTM" in desc or "1915" in (p.hwid or ""):
                self.port_var.set(p.device)
                self.write(f"[INFO] Auto-selected {p.device} ({p.description})")
                return
        if ports:
            self.port_var.set(ports[0].device)

    def open_port(self) -> None:
        try:
            if self.ser:
                self.ser.close()
            self.ser = serial.Serial(self.port_var.get(), 19200,
                                     bytesize=8, parity="N", stopbits=1,
                                     timeout=2.0)
            self.write(f"[OK] Opened {self.port_var.get()} @ 19200 8N1")
            # Send a Reset to align the dongle state machine.
            self._send_cmd(CMD_RESET, label="initial-reset")
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))

    # ----- DTM helpers -------------------------------------------------------
    def _send_cmd(self, cmd: int, freq: int = 0, length: int = 0, pkt: int = 0,
                  label: str = ""):
        if not self.ser:
            self.write("[ERR] COM port is not opened.")
            return None
        frame = build_cmd(cmd, freq, length, pkt)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.write(f"[TX-DTM] {label}: {frame.hex(' ').upper()}")
        self.ser.write(frame)
        resp = self.ser.read(2)
        if len(resp) < 2:
            self.write(f"[RX-DTM] <timeout> partial={resp.hex(' ')}")
            return None
        ev = parse_event(resp)
        self.write(f"[RX-DTM] {resp.hex(' ').upper()}  -> {ev}")
        return ev

    # ----- Mode (standalone vs. with DUT) -----------------------------------
    def is_standalone(self) -> bool:
        try:
            return bool(self.standalone_var.get())
        except Exception:
            return False

    def _toggle_standalone(self) -> None:
        self.standalone_var.set(not self.standalone_var.get())
        self._render_mode_switch()
        self._on_mode_change()

    def _render_mode_switch(self) -> None:
        if self.is_standalone():
            self.mode_switch.config(
                text="STANDALONE  [ON]",
                bg="#1f8f5d", fg="white", activebackground="#1f8f5d")
        else:
            self.mode_switch.config(
                text="DUT-LINK  [OFF]",
                bg="#cccccc", fg="black", activebackground="#bbbbbb")

    def _on_mode_change(self) -> None:
        if self.is_standalone():
            self.write("[MODE] Standalone (dongle only). "
                       "DUT/SSH/Ethernet calls are disabled.")
            try:
                self.ssh.close()
            except Exception:
                pass
            self._apply_mode_widgets(standalone=True)
        else:
            self.write("[MODE] DUT-linked mode (SSH + Ethernet enabled).")
            self._apply_mode_widgets(standalone=False)

    def _apply_mode_widgets(self, standalone: bool) -> None:
        # REBOOT DUT button: only meaningful with DUT link.
        if hasattr(self, "reboot_btn"):
            self.reboot_btn.config(state=("disabled" if standalone else "normal"))
        # AUTO RUN / AUTO RUN (no reboot): hidden entirely in standalone.
        for attr in ("auto_btn", "auto_nr_btn", "stop_btn"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            if standalone:
                try:
                    w.pack_forget()
                except Exception:
                    pass
            else:
                # Re-pack in original order before the Plot CSV button.
                try:
                    w.pack(side="left", padx=(8 if attr == "auto_btn" else 2),
                           before=self.plot_btn)
                except Exception:
                    pass

    # ----- DUT helpers (persistent SSH session) ------------------------------
    def _dut_call(self, fn, *args, **kwargs) -> bool:
        """Run a dut_control function with its stdout/stderr piped into the GUI log."""
        buf = io.StringIO()
        ok = True
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                fn(*args, **kwargs)
        except Exception as exc:
            ok = False
            buf.write(f"\n[DUT-EXC] {exc}\n")
        text = buf.getvalue().rstrip()
        if text:
            self.write(text)
        return ok

    def _dut_call_async(self, fn, *args, **kwargs) -> None:
        threading.Thread(target=self._dut_call, args=(fn, *args),
                         kwargs=kwargs, daemon=True).start()

    def _dut_test_async(self) -> None:
        """Start DUT TX (bt_tx_test_39ch.sh) without blocking the GUI.
        Reuses the persistent SSH session so we don't re-handshake every time."""
        if self.is_standalone():
            self.write("[STANDALONE] Skip DUT TX start (no SSH).")
            return
        self.write("[DUT] bt_tx_test_39ch.sh (persistent SSH)")
        self._dut_call_async(dut_control.run_bt_tx_test,
                             "bt_tx_test_39ch.sh", session=self.ssh)

    def _dut_tx_off(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] Skip bt_test_off.sh (no SSH).")
            return
        self.write("[DUT] bt_test_off.sh (persistent SSH)")
        self._dut_call(dut_control.bt_test_off, session=self.ssh)

    def _dut_reboot(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] Skip DUT reboot (no Ethernet).")
            return
        self.write("[DUT] reboot (drops SSH on purpose)")
        # The reboot frame is a TCP control message, not SSH, so it does
        # not affect our paramiko session by itself. However the DUT will
        # tear down the network shortly after, so close & re-open the SSH
        # session on the next use.
        self._dut_call(dut_control.reboot_dut)
        try:
            self.ssh.close()
        except Exception:
            pass

    # ----- Button handlers ---------------------------------------------------
    def on_start(self) -> None:
        ch = self.ch_var.get()
        length = self.len_var.get()
        self.test_index += 1
        mode = "STANDALONE" if self.is_standalone() else "DUT-LINK"
        self.write(f"\n=== Test #{self.test_index}  START  "
                   f"({mode}, ch={ch}, len={length}) ===")
        # 1) DUT TX on (async, reuses persistent SSH).  Skipped in standalone.
        self._dut_test_async()
        # 2) Dongle RX on
        ev = self._send_cmd(CMD_RECEIVER, freq=ch, length=length,
                            pkt=PKT_PRBS9, label="Receiver Test")
        if ev and ev[0] == "status" and ev[1] == 0:
            self.write("[OK] Dongle accepted Receiver Test")
        else:
            self.write(f"[WARN] Unexpected response from dongle: {ev}")
        if self.is_standalone():
            self.write("[STANDALONE] Now drive the external DUT/TX source "
                       "manually, then press END RX TEST.")

    def on_end(self) -> None:
        # 1) End -> packet count
        ev = self._send_cmd(CMD_END, label="Test End")
        rx_count = -1
        if ev and ev[0] == "packet_count":
            rx_count = ev[1]
            self.write(f"[RESULT] RX packet count = {rx_count}")
        else:
            self.write(f"[WARN] No packet-count event; got {ev}")
        # 2) CSV
        self._save_csv(rx_count)
        # 3) Stop DUT TX cleanly via bt_test_off.sh on the SAME SSH session
        #    (skipped in standalone mode).
        self._dut_tx_off()
        self.write(f"=== Test #{self.test_index}  END ===\n")
        if not self.is_standalone():
            self.write("[HINT] Press REBOOT DUT if you also want to reboot the DUT.")

    def on_reboot(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] REBOOT DUT is disabled in standalone mode.")
            return
        self.write("[REBOOT] Sending reboot frame to DUT...")
        self._dut_reboot()

    def on_reset(self) -> None:
        self._send_cmd(CMD_RESET, label="Reset")

    # ----- Auto loop ---------------------------------------------------------
    def on_auto(self) -> None:
        self._start_auto(reboot_between=True)

    def on_auto_no_reboot(self) -> None:
        self._start_auto(reboot_between=False)

    def _start_auto(self, reboot_between: bool) -> None:
        if self._auto_thread and self._auto_thread.is_alive():
            self.write("[AUTO] already running")
            return
        if not self.ser:
            messagebox.showerror("AUTO", "Open the dongle COM port first.")
            return
        # In standalone mode there is no DUT link, so reboot is meaningless.
        if self.is_standalone() and reboot_between:
            self.write("[AUTO] Standalone mode: forcing 'no reboot' loop.")
            reboot_between = False
        iterations = max(1, int(self.iter_var.get()))
        duration = max(1, int(self.dur_var.get()))
        cooldown = max(0, int(self.cool_var.get()))
        self._auto_stop.clear()
        self.auto_btn.config(state="disabled")
        if hasattr(self, "auto_nr_btn"):
            self.auto_nr_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._auto_thread = threading.Thread(
            target=self._auto_worker,
            args=(iterations, duration, cooldown, reboot_between),
            daemon=True)
        self._auto_thread.start()

    def on_stop(self) -> None:
        self._auto_stop.set()
        self.write("[AUTO] stop requested")

    def _auto_worker(self, iterations: int, duration: int, cooldown: int,
                     reboot_between: bool) -> None:
        ch = self.ch_var.get()
        length = self.len_var.get()
        # How long to wait for the DUT TX to actually start before we open RX.
        startup_delay = 3
        mode = "REBOOT" if reboot_between else "NO-REBOOT"
        self.write(f"\n=== AUTO RUN ({mode}) start: iterations={iterations}, "
                   f"rx_duration={duration}s, "
                   f"{'cooldown' if reboot_between else 'gap'}={cooldown}s ===")
        try:
            for i in range(1, iterations + 1):
                if self._auto_stop.is_set():
                    break
                self.test_index += 1
                self.write(f"\n--- AUTO {i}/{iterations}  Test #{self.test_index} "
                           f"(ch={ch}, len={length}) ---")
                # Start DUT TX asynchronously (bt_tx_test_39ch.sh blocks
                # SSH stdout until the process exits, but it transmits
                # forever - so we MUST NOT wait for it).
                self._dut_test_async()
                # Give the DUT a moment to actually start transmitting.
                if self._auto_stop.wait(startup_delay):
                    self.write("[AUTO] interrupted during startup delay")
                    break
                # Clean dongle state, then start RX.
                self._send_cmd(CMD_RESET, label="pre-RX reset")
                ev = self._send_cmd(CMD_RECEIVER, freq=ch, length=length,
                                    pkt=PKT_PRBS9, label="Receiver Test")
                if not (ev and ev[0] == "status" and ev[1] == 0):
                    self.write(f"[AUTO] Dongle did not accept RX: {ev}")
                # Wait RX duration (interruptible)
                if self._auto_stop.wait(duration):
                    self.write("[AUTO] interrupted during RX window")
                # End RX -> packet count
                ev = self._send_cmd(CMD_END, label="Test End")
                rx_count = ev[1] if ev and ev[0] == "packet_count" else -1
                self.write(f"[RESULT] iter={i} rx_count={rx_count}")
                self._save_csv(rx_count)

                # ---- Failure-stop check ------------------------------------
                # In DUT-linked mode, rx_count == 0 means the DUT TX did not
                # produce packets (firmware crash / link broken). Stop the
                # loop and gather logs for analysis.
                if not self.is_standalone() and rx_count == 0:
                    self.write(f"[AUTO] !!! rx_count=0 at iter {i} - "
                               f"stopping and collecting DUT logs.")
                    self._collect_failure_artifacts(iter_idx=i,
                                                   reason="rx_count==0")
                    break

                if reboot_between:
                    # Reboot DUT to fully reset state (stops bt_tx_test_39ch.sh).
                    self._dut_reboot()
                else:
                    # Stop DUT TX without rebooting (reuse SSH session).
                    self._dut_tx_off()

                # After-iteration log scan: detect failure strings created
                # during this run, even when packets were received.
                if not self.is_standalone():
                    findings = self._scan_logs_quick()
                    if findings:
                        self.write("[AUTO] failure pattern detected in DUT logs:")
                        for f in findings:
                            self.write(f"   - {f}")
                        self._collect_failure_artifacts(iter_idx=i,
                                                       reason="log_pattern",
                                                       findings=findings)
                        break

                # Pause before next iteration
                if i < iterations and cooldown > 0:
                    label = "cooldown" if reboot_between else "gap"
                    self.write(f"[AUTO] {label} {cooldown}s ...")
                    if self._auto_stop.wait(cooldown):
                        self.write(f"[AUTO] interrupted during {label}")
                        break
        finally:
            self.write("=== AUTO RUN finished ===\n")
            self.root.after(0, self._auto_done_ui)

    def _auto_done_ui(self) -> None:
        self.auto_btn.config(state="normal")
        if hasattr(self, "auto_nr_btn"):
            self.auto_nr_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    # ----- Plot --------------------------------------------------------------
    def on_plot(self) -> None:
        path = os.path.join(self._today_folder(), "rx_result.csv")
        if not os.path.exists(path):
            messagebox.showinfo("Plot", f"No CSV yet:\n{path}")
            return
        try:
            indices: list[int] = []
            counts: list[int] = []
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        indices.append(int(row["test_index"]))
                        counts.append(int(row["rx_count"]))
                    except (KeyError, ValueError):
                        continue
        except Exception as exc:
            messagebox.showerror("Plot", f"Failed to read CSV:\n{exc}")
            return
        if not counts:
            messagebox.showinfo("Plot", "CSV has no data points yet.")
            return
        try:
            import matplotlib.pyplot as plt  # type: ignore
        except ImportError:
            # Fallback: pure-Tk bar chart
            self._plot_tk(indices, counts)
            return
        plt.figure(f"DTM RX counts ({len(counts)} runs)")
        plt.plot(indices, counts, marker="o", linestyle="-")
        plt.xlabel("Test index")
        plt.ylabel("RX packet count")
        plt.title(f"DTM RX results - {datetime.date.today():%Y-%m-%d}")
        plt.grid(True, alpha=0.3)
        if counts:
            avg = sum(counts) / len(counts)
            plt.axhline(avg, color="r", linestyle="--",
                        label=f"avg={avg:.1f}")
            plt.legend()
        plt.tight_layout()
        plt.show()

    def _plot_tk(self, indices: list[int], counts: list[int]) -> None:
        win = tk.Toplevel(self.root)
        win.title("DTM RX counts")
        W, H, PAD = 700, 360, 40
        cv = tk.Canvas(win, width=W, height=H, bg="white")
        cv.pack(fill="both", expand=True)
        if not counts:
            return
        cmax = max(counts) or 1
        n = len(counts)
        bw = max(2, (W - 2 * PAD) / n)
        for i, c in enumerate(counts):
            x0 = PAD + i * bw
            x1 = x0 + bw * 0.8
            y1 = H - PAD
            y0 = y1 - (c / cmax) * (H - 2 * PAD)
            cv.create_rectangle(x0, y0, x1, y1, fill="#3a7bd5", outline="")
        cv.create_line(PAD, H - PAD, W - PAD, H - PAD, fill="black")
        cv.create_line(PAD, PAD, PAD, H - PAD, fill="black")
        cv.create_text(W / 2, 12, text=f"RX counts (max={cmax}, n={n})",
                       font=("Arial", 11, "bold"))

    # ----- CSV ---------------------------------------------------------------
    def _today_folder(self) -> str:
        today = datetime.date.today().strftime("%y-%m-%d")
        folder = os.path.join(RESULT_BASE, today)
        os.makedirs(folder, exist_ok=True)
        return folder

    def _logs_folder(self, tag: str = "") -> str:
        """Folder for per-failure DUT logs:
        D:\\factory\\YY-MM-DD\\logs\\<timestamp>[_tag]/"""
        stamp = datetime.datetime.now().strftime("%H%M%S")
        name = f"{stamp}_{tag}" if tag else stamp
        folder = os.path.join(self._today_folder(), "logs", name)
        os.makedirs(folder, exist_ok=True)
        return folder

    def _collect_failure_artifacts(self,
                                   iter_idx: int,
                                   reason: str,
                                   findings: list[str] | None = None) -> None:
        """Download DUT logs, analyze them, and dump the serial-driver state.
        Only meaningful in DUT-linked mode (caller guards this)."""
        folder = self._logs_folder(tag=f"iter{iter_idx}_{reason}")
        self.write(f"[FAIL] collecting artifacts into {folder}")

        # 1) Download bt_test.log / bt_bootstrap.log via SFTP.
        try:
            saved = dut_control.fetch_dut_logs(self.ssh, folder)
        except Exception as exc:
            saved = []
            self.write(f"[FAIL] log download failed: {exc}")

        # 2) Analyze for known failure patterns.
        all_findings: list[str] = list(findings or [])
        if saved:
            all_findings.extend(dut_control.analyze_dut_logs(saved))
        if all_findings:
            self.write("[FAIL] analysis result:")
            for f in all_findings:
                self.write(f"   - {f}")
        else:
            self.write("[FAIL] no known failure signature found in logs.")

        # 3) Dump /proc/tty/driver/serial for serial-state inspection.
        try:
            serial_dump = dut_control.dump_serial_driver(self.ssh)
        except Exception as exc:
            serial_dump = f"<error: {exc}>"
            self.write(f"[FAIL] could not read /proc/tty/driver/serial: {exc}")

        # 4) Save a summary file alongside the downloaded logs.
        summary = os.path.join(folder, "FAILURE_SUMMARY.txt")
        try:
            with open(summary, "w", encoding="utf-8") as f:
                f.write(f"Failure summary\n")
                f.write(f"timestamp     : {datetime.datetime.now()}\n")
                f.write(f"test_index    : {self.test_index}\n")
                f.write(f"iteration     : {iter_idx}\n")
                f.write(f"reason        : {reason}\n")
                f.write(f"channel       : {self.ch_var.get()}\n")
                f.write(f"length        : {self.len_var.get()}\n")
                f.write("\n--- downloaded logs ---\n")
                for p in saved:
                    f.write(p + "\n")
                f.write("\n--- findings ---\n")
                for line in all_findings or ["(none)"]:
                    f.write(line + "\n")
                f.write("\n--- /proc/tty/driver/serial ---\n")
                f.write(serial_dump or "(empty)\n")
            self.write(f"[FAIL] summary -> {summary}")
        except Exception as exc:
            self.write(f"[FAIL] could not write summary: {exc}")

    def _scan_logs_quick(self) -> list[str]:
        """Lightweight per-iteration scan: download logs into a temp folder
        and look for known failure patterns. Returns the list of findings."""
        try:
            folder = self._logs_folder(tag="scan")
            saved = dut_control.fetch_dut_logs(self.ssh, folder)
            findings = dut_control.analyze_dut_logs(saved) if saved else []
            if not findings:
                # Keep the workspace tidy when nothing interesting was found.
                try:
                    for p in saved:
                        os.remove(p)
                    os.rmdir(folder)
                except Exception:
                    pass
            return findings
        except Exception as exc:
            self.write(f"[SCAN] log scan failed: {exc}")
            return []

    def _save_csv(self, rx_count: int) -> None:
        folder = self._today_folder()
        path = os.path.join(folder, "rx_result.csv")
        is_new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["test_index", "timestamp", "channel",
                            "length", "rx_count"])
            w.writerow([self.test_index,
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        self.ch_var.get(),
                        self.len_var.get(),
                        rx_count])
        self.write(f"[CSV] saved -> {path}")

    def _open_today_folder(self) -> None:
        folder = self._today_folder()
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception as exc:
            self.write(f"[ERR] cannot open folder: {exc}")

    # ----- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self) -> None:
        try:
            self._auto_stop.set()
        except Exception:
            pass
        try:
            self.ssh.close()
        except Exception:
            pass
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    DtmRxRunner().run()
