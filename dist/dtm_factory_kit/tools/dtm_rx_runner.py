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

# Where to store CSV / logs.
# Priority:
#   1) $DTM_RX_RESULT_BASE  (explicit override)
#   2) <current working directory>/results
#      -> when launched via run_gui.bat, CWD == the launcher's directory,
#         so the operator sees a "results" folder right next to run_gui.bat.
RESULT_BASE = os.environ.get(
    "DTM_RX_RESULT_BASE",
    os.path.join(os.getcwd(), "results"),
)

# Import DUT helpers as a library so we can share one persistent SSH session
# across all GUI actions instead of spawning a subprocess every time.
sys.path.insert(0, SCRIPT_DIR)
import dut_control  # noqa: E402  (after sys.path tweak)
import notify  # noqa: E402  (Teams webhook helper; optional/no-op if unset)

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
        self.root.title("DTM RX Runner")
        self.root.geometry("920x680")
        self.root.minsize(820, 600)
        # ----- Modern color palette ------------------------------------------
        self.COLORS = {
            "bg":        "#0f172a",  # app background (slate-900)
            "panel":     "#1e293b",  # card background (slate-800)
            "panel2":    "#334155",  # subtle inner block
            "text":      "#e2e8f0",  # primary text
            "muted":     "#94a3b8",  # secondary text
            "accent":    "#38bdf8",  # primary accent (sky-400)
            "ok":        "#22c55e",  # green-500
            "warn":      "#f59e0b",  # amber-500
            "err":       "#ef4444",  # red-500
            "blue":      "#3b82f6",  # blue-500
            "violet":    "#8b5cf6",  # violet-500
            "border":    "#475569",
        }
        self.root.configure(bg=self.COLORS["bg"])
        self._init_style()
        self._build_ui()

    # ----- UI ----------------------------------------------------------------
    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        C = self.COLORS
        style.configure(".", background=C["bg"], foreground=C["text"],
                        fieldbackground=C["panel2"], bordercolor=C["border"])
        style.configure("Card.TFrame", background=C["panel"])
        style.configure("App.TFrame", background=C["bg"])
        style.configure("Card.TLabel", background=C["panel"],
                        foreground=C["text"], font=("Segoe UI", 10))
        style.configure("CardTitle.TLabel", background=C["panel"],
                        foreground=C["accent"],
                        font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", background=C["panel"],
                        foreground=C["muted"], font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=C["bg"],
                        foreground=C["text"],
                        font=("Segoe UI Semibold", 16))
        style.configure("SubHeader.TLabel", background=C["bg"],
                        foreground=C["muted"], font=("Segoe UI", 9))
        style.configure("TSpinbox", fieldbackground=C["panel2"],
                        background=C["panel2"], foreground=C["text"],
                        arrowcolor=C["text"])
        style.configure("TCombobox", fieldbackground=C["panel2"],
                        background=C["panel2"], foreground=C["text"],
                        arrowcolor=C["text"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", C["panel2"])],
                  foreground=[("readonly", C["text"])])

    def _mk_btn(self, parent, text, command, *, bg, fg="white",
                width=14, height=2, font=("Segoe UI Semibold", 10)) -> tk.Button:
        b = tk.Button(parent, text=text, command=command,
                      bg=bg, fg=fg, activebackground=bg,
                      activeforeground=fg, relief="flat", bd=0,
                      width=width, height=height, font=font,
                      cursor="hand2")
        # Remember the design colors so highlight() can restore them later.
        b._dtm_base_bg = bg  # type: ignore[attr-defined]
        b._dtm_base_fg = fg  # type: ignore[attr-defined]
        return b

    # ----- Stage / active-action highlight ----------------------------------
    def _set_stage(self, text: str, *, color: str | None = None,
                   active_buttons: "list[tk.Button] | None" = None) -> None:
        """Update the STAGE indicator and visually highlight the button(s)
        that correspond to the currently running action.

        - ``text``         : short label like "RX running", "cooldown 30s", ...
        - ``color``        : indicator dot color (defaults to accent).
        - ``active_buttons``: buttons to draw a bright ring around.
                              Pass ``[]`` to clear all highlights.
        """
        C = self.COLORS
        col = color or C["accent"]
        try:
            self.stage_var.set(f"● {text}")
            self.stage_lbl.config(fg=col)
        except Exception:
            pass
        # Reset previous highlights
        for b in getattr(self, "_highlighted_btns", []):
            try:
                b.config(bg=b._dtm_base_bg,  # type: ignore[attr-defined]
                         fg=b._dtm_base_fg,  # type: ignore[attr-defined]
                         relief="flat", bd=0,
                         highlightthickness=0)
            except Exception:
                pass
        self._highlighted_btns = list(active_buttons or [])
        for b in self._highlighted_btns:
            try:
                b.config(highlightthickness=3,
                         highlightbackground=col,
                         highlightcolor=col,
                         relief="solid", bd=0)
            except Exception:
                pass
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def _stage_idle(self) -> None:
        self._set_stage("idle", color=self.COLORS["muted"], active_buttons=[])

    def _stage_async(self, text: str, *, color: str | None = None,
                     active_buttons: "list[tk.Button] | None" = None) -> None:
        """Thread-safe variant: schedule _set_stage on the Tk main loop."""
        try:
            self.root.after(0, lambda: self._set_stage(
                text, color=color, active_buttons=active_buttons))
        except Exception:
            pass

    def _build_ui(self) -> None:
        C = self.COLORS
        # ----- Header --------------------------------------------------------
        header = tk.Frame(self.root, bg=C["bg"])
        header.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(header, text="DTM RX Runner",
                  style="Header.TLabel").pack(side="left")
        ttk.Label(header,
                  text="  Bluetooth Direct Test Mode  •  Dongle + DUT",
                  style="SubHeader.TLabel").pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="● Dongle: searching…")
        self.status_lbl = tk.Label(header, textvariable=self.status_var,
                                   bg=C["bg"], fg=C["muted"],
                                   font=("Segoe UI", 10))
        self.status_lbl.pack(side="right")

        # ----- Config card ---------------------------------------------------
        cfg = ttk.Frame(self.root, style="Card.TFrame", padding=14)
        cfg.pack(fill="x", padx=16, pady=6)
        # Header row: title on the left, MANUAL-config "DUT…" button on the
        # right (option B - opens a modal dialog with all SSH/Ethernet/
        # reboot-frame fields, instead of cramming them into the card).
        cfg.grid_columnconfigure(7, weight=1)
        header_row = tk.Frame(cfg, bg=C["panel"])
        header_row.grid(row=0, column=0, columnspan=8, sticky="we",
                        pady=(0, 8))
        ttk.Label(header_row, text="TEST CONFIG",
                  style="CardTitle.TLabel").pack(side="left")
        self.manual_status_summary_var = tk.StringVar(value="")
        self.manual_status_summary_lbl = tk.Label(
            header_row, textvariable=self.manual_status_summary_var,
            bg=C["panel"], fg=C["muted"], font=("Segoe UI", 9))
        self.manual_status_summary_lbl.pack(side="left", padx=(12, 0))
        self.dut_dlg_btn = self._mk_btn(
            header_row, "DUT…", self._open_manual_dialog,
            bg=C["panel2"], width=14, height=1, font=("Segoe UI", 9))
        self.dut_dlg_btn.pack(side="right")

        ttk.Label(cfg, text="Channel", style="Card.TLabel"
                  ).grid(row=1, column=0, sticky="w")
        self.ch_var = tk.IntVar(value=19)
        ttk.Spinbox(cfg, from_=0, to=39, textvariable=self.ch_var,
                    width=6).grid(row=1, column=1, padx=(6, 18), sticky="w")

        ttk.Label(cfg, text="Length", style="Card.TLabel"
                  ).grid(row=1, column=2, sticky="w")
        self.len_var = tk.IntVar(value=37)
        ttk.Spinbox(cfg, from_=0, to=37, textvariable=self.len_var,
                    width=6).grid(row=1, column=3, padx=(6, 18), sticky="w")

        ttk.Label(cfg, text="Iterations", style="Card.TLabel"
                  ).grid(row=1, column=4, sticky="w")
        self.iter_var = tk.IntVar(value=10)
        ttk.Spinbox(cfg, from_=1, to=10000, textvariable=self.iter_var,
                    width=8).grid(row=1, column=5, padx=(6, 18), sticky="w")

        ttk.Label(cfg, text="RX dur (s)", style="Card.TLabel"
                  ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.dur_var = tk.IntVar(value=10)
        ttk.Spinbox(cfg, from_=1, to=600, textvariable=self.dur_var,
                    width=6).grid(row=2, column=1, padx=(6, 18),
                                  sticky="w", pady=(8, 0))

        ttk.Label(cfg, text="Cooldown (s)", style="Card.TLabel"
                  ).grid(row=2, column=2, sticky="w", pady=(8, 0))
        self.cool_var = tk.IntVar(value=30)
        ttk.Spinbox(cfg, from_=0, to=600, textvariable=self.cool_var,
                    width=6).grid(row=2, column=3, padx=(6, 18),
                                  sticky="w", pady=(8, 0))

        # Mode switch: 3-state cycle (DUT-LINK -> MANUAL -> STANDALONE).
        # ``standalone_var`` is kept for backwards-compatible helpers but is
        # derived from ``mode_var``.
        self.MODE_DUT_LINK = "DUT-LINK"
        self.MODE_MANUAL = "MANUAL"
        self.MODE_STANDALONE = "STANDALONE"
        self.mode_var = tk.StringVar(value=self.MODE_DUT_LINK)
        self.standalone_var = tk.BooleanVar(value=False)
        ttk.Label(cfg, text="Mode", style="Card.TLabel"
                  ).grid(row=2, column=4, sticky="w", pady=(8, 0))
        self.mode_switch = tk.Button(cfg, width=18, relief="flat", bd=0,
                                     cursor="hand2",
                                     font=("Segoe UI Semibold", 10),
                                     command=self._cycle_mode)
        self.mode_switch.grid(row=2, column=5, columnspan=2, sticky="w",
                              pady=(8, 0))

        # ----- Manual-port fallback card (hidden by default) -----------------
        self.port_card = ttk.Frame(self.root, style="Card.TFrame", padding=12)
        # not packed yet; only shown when auto-detect fails
        ttk.Label(self.port_card, text="DONGLE PORT (manual)",
                  style="CardTitle.TLabel").pack(side="left")
        ttk.Label(self.port_card,
                  text="  auto-detect failed - pick the COM port:",
                  style="Muted.TLabel").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(self.port_card, textvariable=self.port_var,
                                    width=14, state="readonly")
        self.port_cb.pack(side="left", padx=8)
        self._mk_btn(self.port_card, "Refresh",
                     self.refresh_ports, bg=C["panel2"],
                     width=10, height=1,
                     font=("Segoe UI", 9)).pack(side="left", padx=2)
        self._mk_btn(self.port_card, "Open",
                     self.open_port, bg=C["accent"], fg="#0f172a",
                     width=10, height=1,
                     font=("Segoe UI Semibold", 9)).pack(side="left", padx=2)

        # ----- MANUAL-mode state (option B) ----------------------------------
        # The MANUAL configuration UI lives in a modal dialog opened via the
        # "DUT…" header button. The StringVars below are created here so the
        # rest of the app (helpers, gating logic) can read them at any time
        # even when the dialog is closed.
        self.manual_ssh_host = tk.StringVar(value=dut_control.DUT_HOST)
        self.manual_ssh_user = tk.StringVar(
            value=os.environ.get("DUT_SSH_USER", "root"))
        try:
            _default_key = dut_control._find_ssh_key()
        except Exception:
            _default_key = ""
        self.manual_ssh_key = tk.StringVar(value=_default_key)
        self.manual_eth_host = tk.StringVar(value=dut_control.DUT_HOST)
        self.manual_eth_port = tk.IntVar(value=dut_control.DUT_PORT)
        self.manual_script_dir = tk.StringVar(
            value=dut_control.DEFAULT_SCRIPT_DIR)
        self.manual_tx_script = tk.StringVar(value="bt_tx_test_39ch.sh")
        self.manual_off_script = tk.StringVar(value="bt_test_off.sh")
        self.manual_reboot_frame = tk.StringVar(
            value=dut_control.REBOOT_FRAME.hex(" ").upper())
        self.manual_service_frame = tk.StringVar(
            value=dut_control.SERVICE_ENABLE_FRAME.hex(" ").upper())
        self.manual_status_var = tk.StringVar(value="● SSH: disconnected")
        # Custom DUT log paths (one per line) and custom failure-pattern
        # regexes (one per line, '|' = OR alternatives, dlt/logcat friendly).
        # These are honoured only in MANUAL mode; in DUT-LINK we keep the
        # built-in defaults so behaviour does not regress.
        self.manual_extra_log_paths = tk.StringVar(value="")
        self.manual_extra_fail_patterns = tk.StringVar(value="")
        # Dialog handle (None when closed).
        self._manual_dlg: tk.Toplevel | None = None
        # Widget handles populated when the dialog is open; checked with
        # ``hasattr`` everywhere else.
        self.manual_tx_cb = None
        self.manual_off_cb = None
        self.manual_tx_browse_btn = None
        self.manual_off_browse_btn = None
        self.manual_connect_btn = None
        self.manual_disconnect_btn = None
        self.manual_apply_btn = None
        self.manual_status_lbl = None
        self.manual_reboot_entry = None
        # Re-evaluate AUTO/REBOOT button gating whenever the reboot frame
        # fields change (the trace fires even when the dialog is closed,
        # which is fine - the StringVars persist for the app lifetime).
        self.manual_reboot_frame.trace_add(
            "write", lambda *_a: self._update_reboot_gating())
        self.manual_service_frame.trace_add(
            "write", lambda *_a: self._update_reboot_gating())

        # ----- Action card ---------------------------------------------------
        act = ttk.Frame(self.root, style="Card.TFrame", padding=14)
        act.pack(fill="x", padx=16, pady=6)
        ttk.Label(act, text="ACTIONS", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 8))

        primary = tk.Frame(act, bg=C["panel"])
        primary.pack(fill="x")
        self.start_btn = self._mk_btn(primary, "▶  START RX", self.on_start,
                                      bg=C["ok"], width=16, height=2)
        self.start_btn.pack(side="left", padx=4)
        self.end_btn = self._mk_btn(primary, "■  END RX", self.on_end,
                                    bg=C["err"], width=16, height=2)
        self.end_btn.pack(side="left", padx=4)
        self.auto_btn = self._mk_btn(primary, "⟳  AUTO RUN", self.on_auto,
                                     bg=C["blue"], width=16, height=2)
        self.auto_btn.pack(side="left", padx=4)
        self.auto_nr_btn = self._mk_btn(primary, "⟳  AUTO (no reboot)",
                                        self.on_auto_no_reboot,
                                        bg=C["violet"], width=18, height=2)
        self.auto_nr_btn.pack(side="left", padx=4)
        # STOP is only relevant while AUTO RUN / AUTO (no reboot) is in
        # progress. Keep it hidden when idle so the toolbar isn't cluttered
        # with a permanently-disabled grey button; it is re-packed in
        # on_auto() and pack_forget()'d in _auto_done_ui().
        self.stop_btn = self._mk_btn(primary, "STOP", self.on_stop,
                                     bg="#64748b", width=8, height=2)
        self.stop_btn.config(state="disabled")

        secondary = tk.Frame(act, bg=C["panel"])
        secondary.pack(fill="x", pady=(10, 0))
        self.reboot_btn = self._mk_btn(secondary, "REBOOT DUT",
                                       self.on_reboot, bg=C["warn"],
                                       width=14, height=1,
                                       font=("Segoe UI Semibold", 9))
        self.reboot_btn.pack(side="left", padx=4)
        self.reset_btn = self._mk_btn(secondary, "DTM Reset", self.on_reset,
                                      bg=C["panel2"], width=12, height=1,
                                      font=("Segoe UI", 9))
        self.reset_btn.pack(side="left", padx=4)
        self._mk_btn(secondary, "Plot CSV", self.on_plot,
                     bg=C["panel2"], width=12, height=1,
                     font=("Segoe UI", 9)).pack(side="left", padx=4)
        self._mk_btn(secondary, "Open results folder",
                     self._open_today_folder,
                     bg=C["panel2"], width=20, height=1,
                     font=("Segoe UI", 9)).pack(side="left", padx=4)

        # Stage indicator (shown above the LOG card).
        stage_row = tk.Frame(act, bg=C["panel"])
        stage_row.pack(fill="x", pady=(10, 0))
        ttk.Label(stage_row, text="STAGE", style="CardTitle.TLabel"
                  ).pack(side="left")
        self.stage_var = tk.StringVar(value="● idle")
        self.stage_lbl = tk.Label(stage_row, textvariable=self.stage_var,
                                  bg=C["panel"], fg=C["muted"],
                                  font=("Segoe UI Semibold", 10))
        self.stage_lbl.pack(side="left", padx=(10, 0))

        # ----- Log card ------------------------------------------------------
        logcard = ttk.Frame(self.root, style="Card.TFrame", padding=10)
        logcard.pack(fill="both", expand=True, padx=16, pady=(6, 14))
        ttk.Label(logcard, text="LOG", style="CardTitle.TLabel").pack(
            anchor="w", pady=(0, 6))
        self.log = scrolledtext.ScrolledText(
            logcard, height=16, bg="#0b1220", fg=C["text"],
            insertbackground=C["text"], relief="flat", bd=0,
            font=("Cascadia Mono", 9))
        self.log.pack(fill="both", expand=True)
        # Color tags for log lines
        self.log.tag_configure("ok",   foreground=C["ok"])
        self.log.tag_configure("err",  foreground=C["err"])
        self.log.tag_configure("warn", foreground=C["warn"])
        self.log.tag_configure("info", foreground=C["accent"])
        self.log.tag_configure("muted", foreground=C["muted"])

        self._auto_stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._highlighted_btns: list[tk.Button] = []

        self._render_mode_switch()
        self._on_mode_change()
        self._stage_idle()
        # Try to auto-detect & open the dongle. If it fails, fall back to
        # the manual picker card.
        self.root.after(100, self._auto_open_dongle)

    def write(self, msg: str) -> None:
        # Pick a color tag based on the leading marker.
        tag = None
        s = msg.lstrip()
        if s.startswith(("[OK]", "[RESULT]", "[CSV]")):
            tag = "ok"
        elif s.startswith(("[ERR]", "[FAIL]")):
            tag = "err"
        elif s.startswith(("[WARN]", "[HINT]")):
            tag = "warn"
        elif s.startswith(("[INFO]", "[MODE]", "[DUT]", "[AUTO]",
                            "[REBOOT]", "[STANDALONE]")):
            tag = "info"
        elif s.startswith(("[TX-DTM]", "[RX-DTM]", "[SCAN]")):
            tag = "muted"
        if tag:
            self.log.insert("end", msg + "\n", tag)
        else:
            self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.root.update_idletasks()

    # ----- COM port management ----------------------------------------------
    def _find_dongle_port(self) -> str | None:
        """Look for the Nordic DTM USB CDC-ACM device."""
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "") + " " + (p.manufacturer or "")
            hwid = (p.hwid or "").upper()
            if ("NORDIC" in desc.upper()
                    or "DTM" in desc.upper()
                    or "VID:PID=1915" in hwid
                    or "1915:" in hwid):
                return p.device
        return None

    def _auto_open_dongle(self) -> None:
        """Auto-detect and open the dongle. Show manual picker only on failure."""
        dev = self._find_dongle_port()
        if dev:
            try:
                self.ser = serial.Serial(dev, 19200, bytesize=8, parity="N",
                                         stopbits=1, timeout=2.0)
                self.write(f"[OK] Auto-opened dongle on {dev} @ 19200 8N1")
                self._send_cmd(CMD_RESET, label="initial-reset")
                self._set_status(ok=True, text=f"● Dongle: {dev}")
                # Hide the manual picker if it was shown earlier.
                try:
                    self.port_card.pack_forget()
                except Exception:
                    pass
                return
            except Exception as exc:
                self.write(f"[WARN] Found {dev} but could not open it: {exc}")
        # Fallback: reveal manual picker card.
        self._set_status(ok=False, text="● Dongle: not found")
        self.write("[WARN] Dongle auto-detect failed. "
                   "Use the manual port picker that just appeared.")
        try:
            # Insert the manual card right under the header config card.
            self.port_card.pack(fill="x", padx=16, pady=4, after=None)
        except Exception:
            self.port_card.pack(fill="x", padx=16, pady=4)
        self.refresh_ports()

    def _set_status(self, *, ok: bool, text: str) -> None:
        self.status_var.set(text)
        self.status_lbl.config(fg=self.COLORS["ok"] if ok
                               else self.COLORS["err"])

    def refresh_ports(self) -> None:
        ports = list(serial.tools.list_ports.comports())
        self.port_cb["values"] = [p.device for p in ports]
        dev = self._find_dongle_port()
        if dev:
            self.port_var.set(dev)
            self.write(f"[INFO] Auto-selected {dev}")
        elif ports:
            self.port_var.set(ports[0].device)

    def open_port(self) -> None:
        try:
            if self.ser:
                self.ser.close()
            self.ser = serial.Serial(self.port_var.get(), 19200,
                                     bytesize=8, parity="N", stopbits=1,
                                     timeout=2.0)
            self.write(f"[OK] Opened {self.port_var.get()} @ 19200 8N1")
            self._send_cmd(CMD_RESET, label="initial-reset")
            self._set_status(ok=True, text=f"● Dongle: {self.port_var.get()}")
            try:
                self.port_card.pack_forget()
            except Exception:
                pass
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc))
            self._set_status(ok=False, text="● Dongle: open failed")

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

    # ----- Mode (DUT-LINK / MANUAL / STANDALONE) ---------------------------
    def is_standalone(self) -> bool:
        try:
            return self.mode_var.get() == self.MODE_STANDALONE
        except Exception:
            return False

    def is_manual(self) -> bool:
        try:
            return self.mode_var.get() == self.MODE_MANUAL
        except Exception:
            return False

    def _cycle_mode(self) -> None:
        order = [self.MODE_DUT_LINK, self.MODE_MANUAL, self.MODE_STANDALONE]
        cur = self.mode_var.get()
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order \
            else self.MODE_DUT_LINK
        self.mode_var.set(nxt)
        self.standalone_var.set(nxt == self.MODE_STANDALONE)
        self._render_mode_switch()
        self._on_mode_change()

    def _render_mode_switch(self) -> None:
        C = self.COLORS
        m = self.mode_var.get()
        if m == self.MODE_STANDALONE:
            self.mode_switch.config(text="● STANDALONE",
                                    bg=C["violet"], fg="white",
                                    activebackground=C["violet"])
        elif m == self.MODE_MANUAL:
            self.mode_switch.config(text="● MANUAL",
                                    bg=C["accent"], fg="#0f172a",
                                    activebackground=C["accent"])
        else:
            self.mode_switch.config(text="● DUT-LINK",
                                    bg=C["ok"], fg="white",
                                    activebackground=C["ok"])

    def _on_mode_change(self) -> None:
        m = self.mode_var.get()
        if m == self.MODE_STANDALONE:
            self.write("[MODE] Standalone (dongle only). "
                       "DUT/SSH/Ethernet calls are disabled.")
            try:
                self.ssh.close()
            except Exception:
                pass
            self._apply_mode_widgets(standalone=True, manual=False)
        elif m == self.MODE_MANUAL:
            self.write("[MODE] MANUAL (user-supplied SSH/Ethernet + script). "
                       "Edit fields below, then press 'Connect / Apply'.")
            try:
                self.ssh.close()
            except Exception:
                pass
            self._apply_mode_widgets(standalone=False, manual=True)
        else:
            self.write("[MODE] DUT-LINK (defaults: BMW Telematics).")
            self._apply_mode_widgets(standalone=False, manual=False)

    def _apply_mode_widgets(self, *, standalone: bool, manual: bool) -> None:
        # REBOOT DUT button: only meaningful with DUT link or MANUAL link.
        if hasattr(self, "reboot_btn"):
            self.reboot_btn.config(state=("disabled" if standalone else "normal"))
        # AUTO RUN (with reboot) is meaningless without DUT link.
        if hasattr(self, "auto_btn"):
            self.auto_btn.config(state=("disabled" if standalone else "normal"))
        # MANUAL config now lives in a modal dialog (opened via the "DUT…"
        # header button). The button is always visible; we just hint the
        # operator in MANUAL mode by changing its style.
        if hasattr(self, "dut_dlg_btn"):
            try:
                C = self.COLORS
                if manual:
                    self.dut_dlg_btn.config(
                        text="DUT settings…", bg=C["accent"], fg="#0f172a")
                    self.dut_dlg_btn._dtm_base_bg = C["accent"]  # type: ignore[attr-defined]
                    self.dut_dlg_btn._dtm_base_fg = "#0f172a"  # type: ignore[attr-defined]
                else:
                    self.dut_dlg_btn.config(
                        text="DUT…", bg=C["panel2"], fg="white")
                    self.dut_dlg_btn._dtm_base_bg = C["panel2"]  # type: ignore[attr-defined]
                    self.dut_dlg_btn._dtm_base_fg = "white"  # type: ignore[attr-defined]
            except Exception:
                pass
        # When leaving MANUAL, the SSH session is closed in _on_mode_change.
        # Reflect that in the MANUAL summary widgets so they look correct
        # the next time the user opens the dialog.
        if hasattr(self, "manual_status_var"):
            self._set_manual_ssh_status(connected=False)
        # Final pass: enforce REBOOT / AUTO-RUN(reboot) gating based on
        # whether the MANUAL reboot-frame field is filled in.
        self._update_reboot_gating()

    def _update_reboot_gating(self) -> None:
        """If the user is in MANUAL mode AND left the 'Reboot frame' field
        blank (or malformed), the DUT cannot be rebooted programmatically.
        In that state we disable AUTO RUN (with reboot) and REBOOT DUT so
        the operator cannot trigger an action that would silently fail."""
        if not hasattr(self, "auto_btn") or not hasattr(self, "reboot_btn"):
            return
        mode = self.mode_var.get() if hasattr(self, "mode_var") else ""
        if mode == getattr(self, "MODE_STANDALONE", "STANDALONE"):
            # Standalone: both already disabled by _apply_mode_widgets.
            self.auto_btn.config(state="disabled")
            self.reboot_btn.config(state="disabled")
            return
        if mode == getattr(self, "MODE_MANUAL", "MANUAL"):
            try:
                rb, _se = self._manual_reboot_frames()
                ok = bool(rb)
            except Exception:
                ok = False
            new_state = "normal" if ok else "disabled"
            self.auto_btn.config(state=new_state)
            self.reboot_btn.config(state=new_state)
            # Visual hint on the entry field itself.
            if hasattr(self, "manual_reboot_entry"):
                try:
                    self.manual_reboot_entry.state(
                        ["invalid"] if not ok else ["!invalid"])
                except Exception:
                    pass
        else:
            # DUT-LINK mode: defaults are always present.
            self.auto_btn.config(state="normal")
            self.reboot_btn.config(state="normal")

    # ----- MANUAL mode helpers ----------------------------------------------
    def _open_manual_dialog(self) -> None:
        """Open (or focus) the modal MANUAL DUT CONNECTION dialog.
        Available in all modes - in DUT-LINK / STANDALONE the operator can
        still inspect / pre-edit the values."""
        # Bring an existing dialog forward instead of opening a duplicate.
        if self._manual_dlg is not None and self._manual_dlg.winfo_exists():
            try:
                self._manual_dlg.deiconify()
                self._manual_dlg.lift()
                self._manual_dlg.focus_force()
            except Exception:
                pass
            return
        C = self.COLORS
        dlg = tk.Toplevel(self.root)
        dlg.title("MANUAL DUT CONNECTION")
        dlg.configure(bg=C["panel"])
        dlg.transient(self.root)
        try:
            dlg.grab_set()  # modal
        except Exception:
            pass
        self._manual_dlg = dlg
        body = ttk.Frame(dlg, style="Card.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        # SSH row
        ttk.Label(body, text="SSH host",
                  style="Card.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.manual_ssh_host,
                  width=18).grid(row=1, column=1, padx=(6, 14), sticky="w")
        ttk.Label(body, text="User",
                  style="Card.TLabel").grid(row=1, column=2, sticky="w")
        ttk.Entry(body, textvariable=self.manual_ssh_user,
                  width=12).grid(row=1, column=3, padx=(6, 14), sticky="w")
        ttk.Label(body, text="Key (PEM)",
                  style="Card.TLabel").grid(row=2, column=0, sticky="w",
                                            pady=(8, 0))
        ttk.Entry(body, textvariable=self.manual_ssh_key,
                  width=44).grid(row=2, column=1, columnspan=4,
                                 padx=(6, 4), sticky="we", pady=(8, 0))
        self._mk_btn(body, "Browse…", self._browse_ssh_key,
                     bg=C["panel2"], width=10, height=1,
                     font=("Segoe UI", 9)
                     ).grid(row=2, column=5, padx=2, pady=(8, 0))

        # Ethernet row (reboot frame target)
        ttk.Label(body, text="Ethernet host",
                  style="Card.TLabel").grid(row=3, column=0, sticky="w",
                                            pady=(8, 0))
        ttk.Entry(body, textvariable=self.manual_eth_host,
                  width=18).grid(row=3, column=1, padx=(6, 14),
                                 sticky="w", pady=(8, 0))
        ttk.Label(body, text="Port",
                  style="Card.TLabel").grid(row=3, column=2, sticky="w",
                                            pady=(8, 0))
        ttk.Spinbox(body, from_=1, to=65535,
                    textvariable=self.manual_eth_port, width=8
                    ).grid(row=3, column=3, padx=(6, 14),
                           sticky="w", pady=(8, 0))
        ttk.Label(body, text="(reboot control frame target)",
                  style="Muted.TLabel"
                  ).grid(row=3, column=4, columnspan=2, sticky="w",
                         pady=(8, 0))

        # Reboot-frame rows
        ttk.Label(body, text="Reboot frame (hex)",
                  style="Card.TLabel").grid(row=4, column=0, sticky="w",
                                            pady=(8, 0))
        self.manual_reboot_entry = ttk.Entry(
            body, textvariable=self.manual_reboot_frame, width=58)
        self.manual_reboot_entry.grid(row=4, column=1, columnspan=5,
                                      padx=(6, 4), sticky="we",
                                      pady=(8, 0))
        ttk.Label(body, text="Service-enable (hex, opt.)",
                  style="Card.TLabel").grid(row=5, column=0, sticky="w",
                                            pady=(4, 0))
        ttk.Entry(body, textvariable=self.manual_service_frame, width=58
                  ).grid(row=5, column=1, columnspan=5, padx=(6, 4),
                         sticky="we", pady=(4, 0))
        ttk.Label(body,
                  text="(leave 'Reboot frame' empty to disable REBOOT / AUTO RUN)",
                  style="Muted.TLabel"
                  ).grid(row=6, column=0, columnspan=6, sticky="w",
                         pady=(2, 8))

        # Script dir + TX / TX-off
        ttk.Label(body, text="Script dir",
                  style="Card.TLabel").grid(row=7, column=0, sticky="w",
                                            pady=(6, 0))
        ttk.Entry(body, textvariable=self.manual_script_dir, width=44
                  ).grid(row=7, column=1, columnspan=5, padx=(6, 4),
                         sticky="we", pady=(6, 0))

        ttk.Label(body, text="TX script",
                  style="Card.TLabel").grid(row=8, column=0, sticky="w",
                                            pady=(8, 0))
        self.manual_tx_cb = ttk.Combobox(
            body, textvariable=self.manual_tx_script,
            width=32, state="disabled")
        self.manual_tx_cb.grid(row=8, column=1, columnspan=3,
                               padx=(6, 4), sticky="w", pady=(8, 0))
        self.manual_tx_browse_btn = self._mk_btn(
            body, "Select script",
            lambda: self._pick_remote_script(self.manual_tx_script,
                                             title="Pick TX script"),
            bg=C["panel2"], width=14, height=1, font=("Segoe UI", 9))
        self.manual_tx_browse_btn.grid(row=8, column=4, columnspan=2,
                                       padx=2, pady=(8, 0), sticky="w")
        self.manual_tx_browse_btn.config(state="disabled")

        ttk.Label(body, text="Test End script",
                  style="Card.TLabel").grid(row=9, column=0, sticky="w",
                                            pady=(4, 0))
        self.manual_off_cb = ttk.Combobox(
            body, textvariable=self.manual_off_script,
            width=32, state="disabled")
        self.manual_off_cb.grid(row=9, column=1, columnspan=3,
                                padx=(6, 4), sticky="w", pady=(4, 0))
        self.manual_off_browse_btn = self._mk_btn(
            body, "Select script",
            lambda: self._pick_remote_script(self.manual_off_script,
                                             title="Pick Test End script"),
            bg=C["panel2"], width=14, height=1, font=("Segoe UI", 9))
        self.manual_off_browse_btn.grid(row=9, column=4, columnspan=2,
                                        padx=2, pady=(4, 0), sticky="w")
        self.manual_off_browse_btn.config(state="disabled")

        # Status + action buttons
        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.grid(row=10, column=0, columnspan=6, sticky="we",
                     pady=(14, 0))
        self.manual_connect_btn = self._mk_btn(
            btn_row, "Connect SSH", self._manual_connect,
            bg=C["accent"], fg="#0f172a",
            width=14, height=1, font=("Segoe UI Semibold", 9))
        self.manual_connect_btn.pack(side="left", padx=(0, 4))
        self.manual_disconnect_btn = self._mk_btn(
            btn_row, "Disconnect", self._manual_disconnect,
            bg=C["panel2"], width=12, height=1, font=("Segoe UI", 9))
        self.manual_disconnect_btn.pack(side="left", padx=4)
        self.manual_apply_btn = self._mk_btn(
            btn_row, "Apply settings", self._manual_apply,
            bg=C["ok"], width=14, height=1, font=("Segoe UI Semibold", 9))
        self.manual_apply_btn.pack(side="left", padx=4)
        self._mk_btn(
            btn_row, "Logs & errors…", self._open_logs_errors_dialog,
            bg=C["panel2"], width=14, height=1, font=("Segoe UI", 9)
        ).pack(side="left", padx=4)
        self._mk_btn(btn_row, "Close", self._close_manual_dialog,
                     bg=C["panel2"], width=10, height=1,
                     font=("Segoe UI", 9)
                     ).pack(side="right", padx=(4, 0))
        self.manual_status_lbl = tk.Label(
            btn_row, textvariable=self.manual_status_var,
            bg=C["panel"], fg=C["muted"],
            font=("Segoe UI Semibold", 9))
        self.manual_status_lbl.pack(side="right", padx=8)

        ttk.Label(body,
                  text="1) Edit fields  2) 'Connect SSH'  "
                       "3) Pick TX / TX-off  4) 'Apply settings'",
                  style="Muted.TLabel"
                  ).grid(row=11, column=0, columnspan=6, sticky="w",
                         pady=(10, 0))

        # Reflect current SSH state into the freshly built widgets.
        self._set_manual_ssh_status()
        # If we already have a script list cached on the connected session,
        # populate the comboboxes right away.
        if getattr(self, "ssh", None) is not None and self.ssh.is_alive():
            try:
                names = self._fetch_remote_scripts(quiet=True)
                if names:
                    self.manual_tx_cb["values"] = names
                    self.manual_off_cb["values"] = names
            except Exception:
                pass

        dlg.protocol("WM_DELETE_WINDOW", self._close_manual_dialog)
        dlg.bind("<Escape>", lambda _e: self._close_manual_dialog())
        # Centre on parent.
        dlg.update_idletasks()
        try:
            px, py = self.root.winfo_rootx(), self.root.winfo_rooty()
            pw, ph = self.root.winfo_width(), self.root.winfo_height()
            dw, dh = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry(f"+{px + (pw - dw)//2}+{py + (ph - dh)//3}")
        except Exception:
            pass

    def _close_manual_dialog(self) -> None:
        dlg = self._manual_dlg
        self._manual_dlg = None
        # Drop widget references so other helpers know the dialog is closed.
        self.manual_tx_cb = None
        self.manual_off_cb = None
        self.manual_tx_browse_btn = None
        self.manual_off_browse_btn = None
        self.manual_connect_btn = None
        self.manual_disconnect_btn = None
        self.manual_apply_btn = None
        self.manual_status_lbl = None
        self.manual_reboot_entry = None
        if dlg is not None:
            try:
                dlg.grab_release()
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass

    def _browse_ssh_key(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select SSH private key (PEM)",
            filetypes=[("PEM key", "*.pem"), ("All files", "*.*")])
        if path:
            self.manual_ssh_key.set(path)

    def _manual_connect(self) -> None:
        """Build a new SSHSession from the MANUAL card fields and connect.
        Connect only - does NOT (re)apply the script selections."""
        host = (self.manual_ssh_host.get() or "").strip()
        user = (self.manual_ssh_user.get() or "").strip() or None
        key = (self.manual_ssh_key.get() or "").strip() or None
        if not host:
            messagebox.showerror("MANUAL", "SSH host is empty.")
            return
        if key and not os.path.isfile(key):
            messagebox.showerror("MANUAL", f"SSH key not found:\n{key}")
            return
        try:
            self.ssh.close()
        except Exception:
            pass
        self.ssh = dut_control.SSHSession(host=host, user=user, key_path=key)
        self.write(f"[MANUAL] Connecting SSH {user or '<auto>'}@{host} "
                   f"key={key} ...")
        self._set_manual_ssh_status(connecting=True)

        def _do() -> None:
            try:
                self.ssh.connect()
                self.write(f"[MANUAL] SSH connected as {self.ssh.user}.")
                # Auto-populate the script list once connected.
                names = self._fetch_remote_scripts(quiet=False)
                self.root.after(0, lambda n=names: self._on_manual_ssh_ready(n))
            except Exception as exc:
                self.write(f"[MANUAL] SSH connect failed: {exc}")
                self.root.after(0, lambda: self._set_manual_ssh_status(
                    connecting=False, connected=False))
        threading.Thread(target=_do, daemon=True).start()

    def _manual_disconnect(self) -> None:
        try:
            self.ssh.close()
        except Exception:
            pass
        self.write("[MANUAL] SSH disconnected.")
        self._set_manual_ssh_status(connecting=False, connected=False)

    def _manual_apply(self) -> None:
        """Re-read the MANUAL card fields and log the active configuration.
        Useful after editing Ethernet host/port or switching scripts
        without dropping the SSH session."""
        sd, eh, ep, tx, off = self._manual_params()
        self.write(
            f"[MANUAL] applied -> script_dir={sd}  "
            f"eth={eh}:{ep}  tx={tx}  tx_off={off}")
        # Reflect the new active config in the stage indicator briefly.
        self._set_stage(f"MANUAL applied  tx={tx}",
                        color=self.COLORS["ok"], active_buttons=[])

    # ----- Custom logs / error-pattern dialog -------------------------------
    def _extra_log_paths_list(self) -> list[str]:
        """Split the multi-line StringVar into a clean list of remote paths."""
        raw = self.manual_extra_log_paths.get() or ""
        return [ln.strip() for ln in raw.splitlines() if ln.strip()
                and not ln.strip().startswith("#")]

    def _extra_fail_patterns_list(self) -> list[str]:
        """Split the multi-line StringVar into a list of regex patterns.
        Lines starting with ``#`` are treated as comments and ignored."""
        raw = self.manual_extra_fail_patterns.get() or ""
        return [ln.strip() for ln in raw.splitlines() if ln.strip()
                and not ln.strip().startswith("#")]

    def _open_logs_errors_dialog(self) -> None:
        """MANUAL sub-dialog: edit custom DUT log paths and failure patterns,
        and review all active error messages used by RX fail/pass logic."""
        C = self.COLORS
        dlg = tk.Toplevel(self.root)
        dlg.title("Custom DUT logs & error patterns (MANUAL)")
        dlg.configure(bg=C["panel"])
        dlg.transient(self._manual_dlg or self.root)
        try:
            dlg.grab_set()
        except Exception:
            pass

        body = ttk.Frame(dlg, style="Card.TFrame", padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body,
                  text="Remote DUT log paths (one per line). "
                       "These are downloaded in addition to the built-in "
                       "bt_test.log / bt_bootstrap.log.",
                  style="Card.TLabel", wraplength=620, justify="left"
                  ).pack(anchor="w")
        paths_txt = tk.Text(body, height=6, width=80, wrap="none",
                            bg=C["panel2"], fg="white",
                            insertbackground="white",
                            font=("Consolas", 9))
        paths_txt.pack(fill="x", pady=(4, 10))
        paths_txt.insert("1.0", self.manual_extra_log_paths.get())

        ttk.Label(body,
                  text="Custom failure patterns (one regex per line). "
                       "Use '|' to OR alternatives (dlt/logcat style). "
                       "Lines starting with '#' are comments. "
                       "A match in any downloaded log fails the RX test.",
                  style="Card.TLabel", wraplength=620, justify="left"
                  ).pack(anchor="w")
        pats_txt = tk.Text(body, height=8, width=80, wrap="none",
                           bg=C["panel2"], fg="white",
                           insertbackground="white",
                           font=("Consolas", 9))
        pats_txt.pack(fill="x", pady=(4, 10))
        pats_txt.insert("1.0", self.manual_extra_fail_patterns.get())

        ttk.Label(body, text="Active error messages (built-in + user):",
                  style="CardTitle.TLabel").pack(anchor="w")
        review = scrolledtext.ScrolledText(
            body, height=8, width=80, wrap="none",
            bg=C["panel2"], fg=C["muted"],
            font=("Consolas", 9))
        review.pack(fill="both", expand=True, pady=(4, 8))

        def _refresh_review() -> None:
            review.config(state="normal")
            review.delete("1.0", "end")
            review.insert("end", "# Built-in text patterns (always active)\n")
            for p in dut_control.TEXT_FAIL_PATTERNS:
                try:
                    review.insert("end", f"  {p.decode()}\n")
                except Exception:
                    review.insert("end", f"  {p!r}\n")
            review.insert("end",
                          "\n# Built-in firmware-crash byte signatures\n")
            for p in dut_control.CRASH_PATTERNS:
                review.insert("end", f"  {p!r}\n")
            user_pats = [ln.strip() for ln in pats_txt.get("1.0", "end").splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]
            review.insert("end",
                          f"\n# User patterns ({len(user_pats)}) - "
                          "MANUAL mode only\n")
            for p in user_pats:
                review.insert("end", f"  {p}\n")
            user_paths = [ln.strip() for ln in paths_txt.get("1.0", "end").splitlines()
                          if ln.strip() and not ln.strip().startswith("#")]
            review.insert("end",
                          f"\n# Extra remote log paths ({len(user_paths)}) - "
                          "MANUAL mode only\n")
            for p in user_paths:
                review.insert("end", f"  {p}\n")
            review.config(state="disabled")

        _refresh_review()

        def _validate() -> bool:
            import re as _re
            bad = []
            for ln in pats_txt.get("1.0", "end").splitlines():
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                try:
                    _re.compile(s)
                except _re.error as exc:
                    bad.append(f"  {s!r}: {exc}")
            if bad:
                messagebox.showerror(
                    "Invalid regex",
                    "The following patterns are invalid:\n\n"
                    + "\n".join(bad))
                return False
            return True

        def _save_and_close() -> None:
            if not _validate():
                return
            self.manual_extra_log_paths.set(
                paths_txt.get("1.0", "end").rstrip("\n"))
            self.manual_extra_fail_patterns.set(
                pats_txt.get("1.0", "end").rstrip("\n"))
            n_paths = len(self._extra_log_paths_list())
            n_pats = len(self._extra_fail_patterns_list())
            self.write(f"[MANUAL] custom logs/errors saved: "
                       f"{n_paths} extra path(s), {n_pats} extra pattern(s).")
            try:
                dlg.grab_release()
            except Exception:
                pass
            dlg.destroy()

        btn_row = tk.Frame(body, bg=C["panel"])
        btn_row.pack(fill="x")
        self._mk_btn(btn_row, "Refresh review", _refresh_review,
                     bg=C["panel2"], width=14, height=1,
                     font=("Segoe UI", 9)).pack(side="left")
        self._mk_btn(btn_row, "Save", _save_and_close,
                     bg=C["ok"], width=10, height=1,
                     font=("Segoe UI Semibold", 9)).pack(side="right",
                                                         padx=(4, 0))
        self._mk_btn(btn_row, "Cancel",
                     lambda: (dlg.grab_release(), dlg.destroy()),
                     bg=C["panel2"], width=10, height=1,
                     font=("Segoe UI", 9)).pack(side="right")

        dlg.bind("<Escape>", lambda _e: (dlg.grab_release(), dlg.destroy()))
        dlg.protocol("WM_DELETE_WINDOW",
                     lambda: (dlg.grab_release(), dlg.destroy()))
        dlg.update_idletasks()
        try:
            anchor = self._manual_dlg or self.root
            px, py = anchor.winfo_rootx(), anchor.winfo_rooty()
            pw, ph = anchor.winfo_width(), anchor.winfo_height()
            dw, dh = dlg.winfo_width(), dlg.winfo_height()
            dlg.geometry(f"+{px + (pw - dw)//2}+{py + (ph - dh)//3}")
        except Exception:
            pass

    # ----- MANUAL SSH state helpers -----------------------------------------
    def _set_manual_ssh_status(self, *, connecting: bool = False,
                               connected: bool | None = None) -> None:
        """Update the MANUAL dialog status label (when open) and the small
        summary label in the TEST CONFIG header. Also enable/disable the
        script selection widgets if the dialog is currently open."""
        def _apply_label(text: str, color_key: str) -> None:
            self.manual_status_var.set(text)
            color = self.COLORS[color_key]
            for lbl in (getattr(self, "manual_status_lbl", None),
                        getattr(self, "manual_status_summary_lbl", None)):
                if lbl is not None:
                    try:
                        lbl.config(fg=color)
                    except Exception:
                        pass
            # Reflect the same text in the header summary.
            if hasattr(self, "manual_status_summary_var"):
                # Only display the SSH dot in MANUAL mode, otherwise blank.
                if self.is_manual():
                    self.manual_status_summary_var.set(text)
                else:
                    self.manual_status_summary_var.set("")

        def _btn(name: str, state: str) -> None:
            b = getattr(self, name, None)
            if b is not None:
                try:
                    b.config(state=state)
                except Exception:
                    pass

        if connecting:
            _apply_label("● SSH: connecting…", "warn")
            _btn("manual_connect_btn", "disabled")
            _btn("manual_disconnect_btn", "disabled")
            self._set_script_widgets_enabled(False)
            return
        is_conn = bool(connected) if connected is not None \
            else (getattr(self, "ssh", None) is not None
                  and self.ssh.is_alive())
        if is_conn:
            _apply_label(f"● SSH: connected ({self.ssh.user})", "ok")
            _btn("manual_connect_btn", "disabled")
            _btn("manual_disconnect_btn", "normal")
            self._set_script_widgets_enabled(True)
        else:
            _apply_label("● SSH: disconnected", "muted")
            _btn("manual_connect_btn", "normal")
            _btn("manual_disconnect_btn", "disabled")
            self._set_script_widgets_enabled(False)

    def _set_script_widgets_enabled(self, enabled: bool) -> None:
        """Enable/disable the TX / TX-off combobox & 'pick' buttons."""
        cb_state = "readonly" if enabled else "disabled"
        btn_state = "normal" if enabled else "disabled"
        for w in (getattr(self, "manual_tx_cb", None),
                  getattr(self, "manual_off_cb", None)):
            if w is not None:
                try:
                    w.config(state=cb_state)
                except Exception:
                    pass
        for b in (getattr(self, "manual_tx_browse_btn", None),
                  getattr(self, "manual_off_browse_btn", None)):
            if b is not None:
                try:
                    b.config(state=btn_state)
                except Exception:
                    pass

    def _on_manual_ssh_ready(self, names: list[str]) -> None:
        """Called on the Tk main loop after a successful SSH connect."""
        if names:
            if self.manual_tx_cb is not None:
                try:
                    self.manual_tx_cb["values"] = names
                except Exception:
                    pass
            if self.manual_off_cb is not None:
                try:
                    self.manual_off_cb["values"] = names
                except Exception:
                    pass
            tx = self.manual_tx_script.get()
            if tx not in names:
                picked = next((n for n in names if "tx" in n.lower()
                               and "off" not in n.lower()), names[0])
                self.manual_tx_script.set(picked)
            off = self.manual_off_script.get()
            if off not in names:
                picked_off = next((n for n in names if "off" in n.lower()),
                                  names[0])
                self.manual_off_script.set(picked_off)
        self._set_manual_ssh_status(connected=True)

    def _fetch_remote_scripts(self, *, quiet: bool = False) -> list[str]:
        script_dir = (self.manual_script_dir.get() or "").strip() \
            or dut_control.DEFAULT_SCRIPT_DIR
        try:
            names = dut_control.list_remote_scripts(self.ssh, script_dir)
        except Exception as exc:
            if not quiet:
                self.write(f"[MANUAL] could not list {script_dir}: {exc}")
            return []
        if not quiet:
            if names:
                self.write(f"[MANUAL] {len(names)} script(s) found in {script_dir}.")
            else:
                self.write(f"[MANUAL] no *.sh files found in {script_dir}")
        return names

    def _pick_remote_script(self, target_var: tk.StringVar, *,
                            title: str = "Pick script") -> None:
        """Modal picker dialog: lists *.sh files in the DUT script dir and
        writes the selected name into ``target_var``.

        The MANUAL dialog (its likely parent) already holds the global Tk
        grab via ``grab_set()``. We therefore parent this Toplevel to the
        MANUAL dialog when it is open (otherwise to root), and take over
        the grab while this picker is alive so that Esc / X / OK can
        actually close it."""
        if not getattr(self, "ssh", None) or not self.ssh.is_alive():
            messagebox.showinfo(
                "MANUAL",
                "SSH is not connected yet.\nPress 'Connect SSH' first.")
            return
        # Always refresh from the remote so an edited script_dir is honoured.
        names = self._fetch_remote_scripts(quiet=True)
        if not names:
            script_dir = (self.manual_script_dir.get() or "").strip() \
                or dut_control.DEFAULT_SCRIPT_DIR
            messagebox.showinfo("MANUAL", f"No *.sh files in {script_dir}.")
            return
        script_dir = (self.manual_script_dir.get() or "").strip() \
            or dut_control.DEFAULT_SCRIPT_DIR

        parent = (self._manual_dlg
                  if (self._manual_dlg is not None
                      and self._manual_dlg.winfo_exists())
                  else self.root)
        dlg = tk.Toplevel(parent)
        dlg.title(f"{title} - {script_dir}")
        dlg.configure(bg=self.COLORS["panel"])
        dlg.transient(parent)
        # Steal the grab from the MANUAL dialog so this picker is the one
        # receiving events. We'll restore the parent grab on close.
        try:
            dlg.grab_set()
        except Exception:
            pass

        tk.Label(dlg, text=script_dir, bg=self.COLORS["panel"],
                 fg=self.COLORS["muted"]).pack(anchor="w", padx=10, pady=(8, 2))
        lb = tk.Listbox(dlg, width=44, height=min(16, max(4, len(names))),
                        bg="#0b1220", fg=self.COLORS["text"],
                        selectbackground=self.COLORS["accent"],
                        exportselection=False)
        for n in names:
            lb.insert("end", n)
        lb.pack(fill="both", expand=True, padx=10, pady=4)
        try:
            idx = names.index(target_var.get())
            lb.selection_set(idx)
            lb.see(idx)
        except ValueError:
            pass

        def _cleanup() -> None:
            try:
                dlg.grab_release()
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass
            # Hand the grab back to the MANUAL dialog if it is still open.
            if (self._manual_dlg is not None
                    and self._manual_dlg.winfo_exists()):
                try:
                    self._manual_dlg.grab_set()
                except Exception:
                    pass

        def _ok() -> None:
            sel = lb.curselection()
            if sel:
                target_var.set(names[sel[0]])
                # Keep both comboboxes in sync with the latest listing.
                if getattr(self, "manual_tx_cb", None) is not None:
                    try:
                        self.manual_tx_cb["values"] = names
                    except Exception:
                        pass
                if getattr(self, "manual_off_cb", None) is not None:
                    try:
                        self.manual_off_cb["values"] = names
                    except Exception:
                        pass
            _cleanup()

        btn_row = tk.Frame(dlg, bg=self.COLORS["panel"])
        btn_row.pack(fill="x", padx=10, pady=(4, 10))
        tk.Button(btn_row, text="OK", command=_ok, width=10,
                  bg=self.COLORS["accent"], fg="#0f172a",
                  relief="flat").pack(side="right", padx=(4, 0))
        tk.Button(btn_row, text="Cancel", command=_cleanup, width=10,
                  bg=self.COLORS["panel2"], fg="white",
                  relief="flat").pack(side="right")
        lb.bind("<Double-Button-1>", lambda _e: _ok())
        lb.bind("<Return>", lambda _e: _ok())
        dlg.bind("<Escape>", lambda _e: _cleanup())
        dlg.protocol("WM_DELETE_WINDOW", _cleanup)
        lb.focus_set()

    def _manual_params(self) -> tuple[str, str, int, str, str]:
        """Return (script_dir, eth_host, eth_port, tx_script, off_script)."""
        return (
            (self.manual_script_dir.get() or "").strip()
            or dut_control.DEFAULT_SCRIPT_DIR,
            (self.manual_eth_host.get() or "").strip()
            or dut_control.DUT_HOST,
            int(self.manual_eth_port.get() or dut_control.DUT_PORT),
            (self.manual_tx_script.get() or "").strip()
            or "bt_tx_test_39ch.sh",
            (self.manual_off_script.get() or "").strip()
            or "bt_test_off.sh",
        )

    def _manual_reboot_frames(self) -> tuple[bytes, bytes]:
        """Parse the MANUAL reboot/service-enable hex entries.
        Raises ValueError if the hex strings are malformed."""
        rb = dut_control._parse_hex_frame(self.manual_reboot_frame.get())
        se = dut_control._parse_hex_frame(self.manual_service_frame.get())
        return rb, se

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
        """Start DUT TX without blocking the GUI.
        Reuses the persistent SSH session so we don't re-handshake every time."""
        if self.is_standalone():
            self.write("[STANDALONE] Skip DUT TX start (no SSH).")
            return
        if self.is_manual():
            script_dir, _eh, _ep, tx, _off = self._manual_params()
            self.write(f"[DUT] {tx}  (manual, dir={script_dir})")
            self._dut_call_async(dut_control.run_bt_tx_test,
                                 tx, session=self.ssh,
                                 script_dir=script_dir)
            return
        self.write("[DUT] bt_tx_test_39ch.sh (persistent SSH)")
        self._dut_call_async(dut_control.run_bt_tx_test,
                             "bt_tx_test_39ch.sh", session=self.ssh)

    def _dut_tx_off(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] Skip bt_test_off.sh (no SSH).")
            return
        if self.is_manual():
            script_dir, _eh, _ep, _tx, off = self._manual_params()
            self.write(f"[DUT] {off}  (manual, dir={script_dir})")
            self._dut_call(dut_control.bt_test_off,
                           session=self.ssh,
                           script=off, script_dir=script_dir)
            return
        self.write("[DUT] bt_test_off.sh (persistent SSH)")
        self._dut_call(dut_control.bt_test_off, session=self.ssh)

    def _dut_reboot(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] Skip DUT reboot (no Ethernet).")
            return
        if self.is_manual():
            _sd, eh, ep, _tx, _off = self._manual_params()
            try:
                rb, se = self._manual_reboot_frames()
            except ValueError as exc:
                self.write(f"[DUT] reboot aborted - bad hex frame: {exc}")
                return
            if not rb:
                self.write("[DUT] reboot skipped - no reboot frame configured "
                           "(MANUAL card).")
                return
            self.write(f"[DUT] reboot (manual {eh}:{ep}, "
                       f"rb={len(rb)}B, svc={len(se)}B)")
            self._dut_call(dut_control.reboot_dut, host=eh, port=ep,
                           reboot_frame=rb, service_enable_frame=se)
        else:
            self.write("[DUT] reboot (drops SSH on purpose)")
            # The reboot frame is a TCP control message, not SSH, so it does
            # not affect our paramiko session by itself. However the DUT
            # will tear down the network shortly after, so close & re-open
            # the SSH session on the next use.
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
        self._set_stage(f"RX running  (ch={ch}, len={length})",
                        color=self.COLORS["ok"],
                        active_buttons=[self.start_btn])
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
        self._set_stage("ending RX", color=self.COLORS["err"],
                        active_buttons=[self.end_btn])
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
        self._stage_idle()

    def on_reboot(self) -> None:
        if self.is_standalone():
            self.write("[STANDALONE] REBOOT DUT is disabled in standalone mode.")
            return
        self.write("[REBOOT] Sending reboot frame to DUT...")
        self._set_stage("rebooting DUT", color=self.COLORS["warn"],
                        active_buttons=[self.reboot_btn])
        self._dut_reboot()
        self._stage_idle()

    def on_reset(self) -> None:
        self._set_stage("DTM reset", color=self.COLORS["accent"],
                        active_buttons=[self.reset_btn])
        self._send_cmd(CMD_RESET, label="Reset")
        self._stage_idle()

    # ----- Dongle recovery --------------------------------------------------
    def _reopen_dongle(self) -> bool:
        """Close + reopen the dongle CDC-ACM port and verify it responds.
        Returns True if the dongle answers a DTM Reset after reopening.

        The Nordic dongle's USB CDC-ACM endpoint occasionally stops
        answering after the DUT reboot (USB hub glitch / driver hiccup).
        Symptom in the GUI log: every DTM frame returns ``<timeout>
        partial=`` and ``rx_count`` drops to -1. Cycling the serial
        handle clears the stuck state without any user action.
        """
        import time
        old_dev = None
        try:
            if self.ser is not None:
                old_dev = self.ser.port
                try:
                    self.ser.close()
                except Exception:
                    pass
            time.sleep(0.5)
            dev = old_dev or self._find_dongle_port()
            if not dev:
                self.write("[DONGLE] auto-detect could not find the dongle "
                           "after a USB cycle.")
                return False
            self.ser = serial.Serial(dev, 19200, bytesize=8, parity="N",
                                     stopbits=1, timeout=2.0)
            # Probe with a Reset - this should always answer 00 00.
            ev = self._send_cmd(CMD_RESET, label="recovery-reset")
            ok = bool(ev and ev[0] == "status")
            if ok:
                self.write(f"[DONGLE] recovered on {dev}")
                self._set_status(ok=True, text=f"● Dongle: {dev}")
            else:
                self.write(f"[DONGLE] reopen succeeded on {dev} but "
                           f"it still does not answer DTM.")
                self._set_status(ok=False, text=f"● Dongle: stuck ({dev})")
            return ok
        except Exception as exc:
            self.write(f"[DONGLE] reopen failed: {exc}")
            self._set_status(ok=False, text="● Dongle: reopen failed")
            return False

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
        # In MANUAL mode, the reboot frame is operator-supplied; if it's
        # empty the AUTO-RUN-with-reboot button should already be disabled,
        # but guard here as well.
        if self.is_manual() and reboot_between:
            try:
                rb, _se = self._manual_reboot_frames()
            except Exception as exc:
                messagebox.showerror(
                    "AUTO",
                    f"MANUAL 'Reboot frame' is invalid hex:\n{exc}")
                return
            if not rb:
                messagebox.showerror(
                    "AUTO",
                    "MANUAL mode: 'Reboot frame' is empty.\n"
                    "Fill it in or use AUTO (no reboot).")
                return
        iterations = max(1, int(self.iter_var.get()))
        duration = max(1, int(self.dur_var.get()))
        cooldown = max(0, int(self.cool_var.get()))
        self._auto_stop.clear()
        self.auto_btn.config(state="disabled")
        if hasattr(self, "auto_nr_btn"):
            self.auto_nr_btn.config(state="disabled")
        self.stop_btn.pack(side="left", padx=4)
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
        active_auto_btn = (self.auto_btn if reboot_between
                           else self.auto_nr_btn)
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
                self._stage_async(f"[{i}/{iterations}] DUT TX starting",
                                  color=self.COLORS["blue"],
                                  active_buttons=[active_auto_btn])
                self._dut_test_async()
                # Give the DUT a moment to actually start transmitting.
                if self._auto_stop.wait(startup_delay):
                    self.write("[AUTO] interrupted during startup delay")
                    break
                # Clean dongle state, then start RX.
                rst = self._send_cmd(CMD_RESET, label="pre-RX reset")
                if rst is None:
                    # Dongle stopped answering (typically right after a
                    # DUT reboot cycled the USB hub). Try one auto-recovery
                    # before declaring this iteration a wash.
                    self.write("[AUTO] dongle did not answer pre-RX reset - "
                               "attempting USB recovery.")
                    self._stage_async(
                        f"[{i}/{iterations}] recovering dongle",
                        color=self.COLORS["warn"],
                        active_buttons=[active_auto_btn])
                    if not self._reopen_dongle():
                        self.write("[AUTO] dongle recovery failed - "
                                   "stopping AUTO loop.")
                        if not self.is_standalone():
                            self._collect_failure_artifacts(
                                iter_idx=i, reason="dongle_unresponsive")
                        break
                ev = self._send_cmd(CMD_RECEIVER, freq=ch, length=length,
                                    pkt=PKT_PRBS9, label="Receiver Test")
                if not (ev and ev[0] == "status" and ev[1] == 0):
                    self.write(f"[AUTO] Dongle did not accept RX: {ev}")
                # Wait RX duration (interruptible)
                self._stage_async(
                    f"[{i}/{iterations}] RX running  ({duration}s, ch={ch})",
                    color=self.COLORS["ok"],
                    active_buttons=[active_auto_btn])
                if self._auto_stop.wait(duration):
                    self.write("[AUTO] interrupted during RX window")
                # End RX -> packet count
                self._stage_async(f"[{i}/{iterations}] ending RX",
                                  color=self.COLORS["err"],
                                  active_buttons=[active_auto_btn])
                ev = self._send_cmd(CMD_END, label="Test End")
                rx_count = ev[1] if ev and ev[0] == "packet_count" else -1
                self.write(f"[RESULT] iter={i} rx_count={rx_count}")
                self._save_csv(rx_count)

                # ---- Failure-stop check ------------------------------------
                # In DUT-linked mode:
                #   rx_count == 0  -> DUT TX produced nothing (FW crash / link)
                #   rx_count == -1 -> dongle did not return a Test-End event
                #                     (USB CDC-ACM stuck). Try one auto
                #                     recovery; if that fails, treat as fail.
                if not self.is_standalone() and rx_count == -1:
                    self.write(f"[AUTO] !!! rx_count=-1 at iter {i} - "
                               f"dongle unresponsive, attempting recovery.")
                    self._stage_async(
                        f"[{i}/{iterations}] recovering dongle",
                        color=self.COLORS["warn"],
                        active_buttons=[active_auto_btn])
                    if not self._reopen_dongle():
                        self._stage_async("collecting failure artifacts",
                                          color=self.COLORS["err"],
                                          active_buttons=[active_auto_btn])
                        self._collect_failure_artifacts(
                            iter_idx=i, reason="dongle_unresponsive")
                        break
                    # Recovered: still flag this iteration as a failure so
                    # the operator sees something happened, but keep going.
                    self.write("[AUTO] dongle recovered - continuing loop.")
                if not self.is_standalone() and rx_count == 0:
                    self.write(f"[AUTO] !!! rx_count=0 at iter {i} - "
                               f"stopping and collecting DUT logs.")
                    self._stage_async("collecting failure artifacts",
                                      color=self.COLORS["err"],
                                      active_buttons=[active_auto_btn])
                    self._collect_failure_artifacts(iter_idx=i,
                                                   reason="rx_count==0")
                    break

                if reboot_between:
                    # IMPORTANT: bt_tx_test_39ch.sh is still transmitting at
                    # this point (it runs forever on the DUT). The DUT's
                    # reboot control service does not respond reliably while
                    # the BT TX test is holding HCI, so we MUST stop the TX
                    # cleanly first - exactly the same sequence the manual
                    # "END RX -> REBOOT DUT" flow uses, which is known to
                    # work. Without this pre-step the AUTO RUN reboot
                    # silently fails.
                    self._stage_async(f"[{i}/{iterations}] stopping DUT TX",
                                      color=self.COLORS["warn"],
                                      active_buttons=[active_auto_btn])
                    self._dut_tx_off()
                    # Small settle so HCI is fully released before the
                    # reboot control frame is sent.
                    if self._auto_stop.wait(1.0):
                        break
                    # Reboot DUT to fully reset state.
                    self._stage_async(f"[{i}/{iterations}] rebooting DUT",
                                      color=self.COLORS["warn"],
                                      active_buttons=[active_auto_btn])
                    self._dut_reboot()
                else:
                    # Stop DUT TX without rebooting (reuse SSH session).
                    self._stage_async(f"[{i}/{iterations}] stopping DUT TX",
                                      color=self.COLORS["warn"],
                                      active_buttons=[active_auto_btn])
                    self._dut_tx_off()

                # After-iteration log scan: detect failure strings created
                # during this run, even when packets were received.
                if not self.is_standalone():
                    self._stage_async(f"[{i}/{iterations}] scanning DUT logs",
                                      color=self.COLORS["accent"],
                                      active_buttons=[active_auto_btn])
                    findings = self._scan_logs_quick()
                    if findings:
                        self.write("[AUTO] failure pattern detected in DUT logs:")
                        for f in findings:
                            self.write(f"   - {f}")
                        self._stage_async("collecting failure artifacts",
                                          color=self.COLORS["err"],
                                          active_buttons=[active_auto_btn])
                        self._collect_failure_artifacts(iter_idx=i,
                                                       reason="log_pattern",
                                                       findings=findings)
                        break

                # Pause before next iteration
                if i < iterations and cooldown > 0:
                    label = "cooldown" if reboot_between else "gap"
                    self.write(f"[AUTO] {label} {cooldown}s ...")
                    self._stage_async(
                        f"[{i}/{iterations}] {label} {cooldown}s",
                        color=self.COLORS["muted"],
                        active_buttons=[active_auto_btn])
                    if self._auto_stop.wait(cooldown):
                        self.write(f"[AUTO] interrupted during {label}")
                        break
            else:
                # Loop ran to completion: every configured iteration finished
                # without a failure-stop or user STOP. Send a PASS notification.
                self.write(f"[AUTO] all {iterations} iterations passed.")
                try:
                    sent = notify.notify_success(
                        iterations=iterations, channel=ch, length=length)
                    if sent:
                        self.write("[AUTO] Teams success notification sent.")
                    elif notify.get_webhook_url():
                        self.write("[AUTO] Teams success notification failed.")
                except Exception as exc:
                    self.write(f"[AUTO] Teams notification error: {exc}")
        finally:
            self.write("=== AUTO RUN finished ===\n")
            self.root.after(0, self._auto_done_ui)

    def _auto_done_ui(self) -> None:
        self.auto_btn.config(state="normal")
        if hasattr(self, "auto_nr_btn"):
            self.auto_nr_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        try:
            self.stop_btn.pack_forget()
        except Exception:
            pass
        self._stage_idle()

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
        cmin = min(counts)
        cmax = max(counts)
        cavg = sum(counts) / len(counts)
        fig = plt.figure(f"DTM RX counts ({len(counts)} runs)")
        plt.plot(indices, counts, marker="o", linestyle="-",
                 color="#3b82f6", label="rx_count")
        plt.xlabel("Test index")
        plt.ylabel("RX packet count")
        plt.title(f"DTM RX results - {datetime.date.today():%Y-%m-%d}  "
                  f"(n={len(counts)})")
        plt.grid(True, alpha=0.3)
        # min / max / avg reference lines
        plt.axhline(cmax, color="#22c55e", linestyle=":",
                    label=f"max={cmax}")
        plt.axhline(cavg, color="#ef4444", linestyle="--",
                    label=f"avg={cavg:.1f}")
        plt.axhline(cmin, color="#f59e0b", linestyle=":",
                    label=f"min={cmin}")
        # Annotate the actual min/max points
        try:
            i_max = counts.index(cmax)
            i_min = counts.index(cmin)
            plt.annotate(f"max={cmax}",
                         (indices[i_max], cmax),
                         textcoords="offset points", xytext=(6, 8),
                         color="#22c55e", fontsize=9)
            plt.annotate(f"min={cmin}",
                         (indices[i_min], cmin),
                         textcoords="offset points", xytext=(6, -14),
                         color="#f59e0b", fontsize=9)
        except Exception:
            pass
        # Stats box in the corner
        stats = (f"min  = {cmin}\n"
                 f"max  = {cmax}\n"
                 f"avg  = {cavg:.2f}\n"
                 f"runs = {len(counts)}")
        plt.gca().text(0.02, 0.98, stats, transform=plt.gca().transAxes,
                       va="top", ha="left", fontsize=9, family="monospace",
                       bbox=dict(boxstyle="round,pad=0.4",
                                 facecolor="#f8fafc",
                                 edgecolor="#94a3b8", alpha=0.9))
        plt.legend(loc="lower right")
        plt.tight_layout()
        plt.show()

    def _plot_tk(self, indices: list[int], counts: list[int]) -> None:
        win = tk.Toplevel(self.root)
        win.title("DTM RX counts")
        W, H, PAD = 740, 400, 50
        cv = tk.Canvas(win, width=W, height=H, bg="white")
        cv.pack(fill="both", expand=True)
        if not counts:
            return
        cmax = max(counts) or 1
        cmin = min(counts)
        cavg = sum(counts) / len(counts)
        n = len(counts)
        bw = max(2, (W - 2 * PAD) / n)

        def y_of(v: float) -> float:
            return (H - PAD) - (v / cmax) * (H - 2 * PAD)

        # Bars
        for i, c in enumerate(counts):
            x0 = PAD + i * bw
            x1 = x0 + bw * 0.8
            y1 = H - PAD
            y0 = y_of(c)
            cv.create_rectangle(x0, y0, x1, y1, fill="#3b82f6", outline="")
        # Axes
        cv.create_line(PAD, H - PAD, W - PAD, H - PAD, fill="black")
        cv.create_line(PAD, PAD, PAD, H - PAD, fill="black")
        # min / max / avg reference lines
        for v, color, label in (
                (cmax, "#22c55e", f"max={cmax}"),
                (cavg, "#ef4444", f"avg={cavg:.1f}"),
                (cmin, "#f59e0b", f"min={cmin}")):
            y = y_of(v)
            cv.create_line(PAD, y, W - PAD, y, fill=color, dash=(4, 3))
            cv.create_text(W - PAD - 4, y - 8, text=label, fill=color,
                           anchor="e", font=("Segoe UI", 9, "bold"))
        # Title + stats box
        cv.create_text(W / 2, 14,
                       text=f"RX counts (n={n})",
                       font=("Segoe UI Semibold", 11))
        stats = f"min={cmin}   avg={cavg:.2f}   max={cmax}   runs={n}"
        cv.create_text(PAD, H - 10, anchor="w", text=stats,
                       font=("Consolas", 9), fill="#0f172a")

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
        extra_paths = self._extra_log_paths_list() if self.is_manual() else []
        extra_pats = self._extra_fail_patterns_list() if self.is_manual() else []
        try:
            saved = dut_control.fetch_dut_logs(
                self.ssh, folder, extra_paths=extra_paths)
        except Exception as exc:
            saved = []
            self.write(f"[FAIL] log download failed: {exc}")

        # 2) Analyze for known failure patterns.
        all_findings: list[str] = list(findings or [])
        if saved:
            all_findings.extend(
                dut_control.analyze_dut_logs(saved, extra_patterns=extra_pats))
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

        # 3a) DUT OS version (VERSION= field from /etc/os-release).
        try:
            os_version = dut_control.dump_os_version(self.ssh)
        except Exception as exc:
            os_version = f"<error: {exc}>"
            self.write(f"[FAIL] could not read /etc/os-release: {exc}")

        # 3b) On rx_count==0, capture the kernel ring buffer (dmesg) for
        #     debugging USB/UART resets, driver errors and crash traces.
        dmesg_dump = None
        if reason == "rx_count==0":
            try:
                dmesg_dump = dut_control.dump_dmesg(self.ssh)
            except Exception as exc:
                dmesg_dump = f"<error: {exc}>"
                self.write(f"[FAIL] could not read dmesg: {exc}")

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
                f.write(f"DUT VERSION   : {os_version or '(unknown)'}\n")
                f.write("\n--- downloaded logs ---\n")
                for p in saved:
                    f.write(p + "\n")
                f.write("\n--- findings ---\n")
                for line in all_findings or ["(none)"]:
                    f.write(line + "\n")
                f.write("\n--- /proc/tty/driver/serial ---\n")
                f.write(serial_dump or "(empty)\n")
                if dmesg_dump is not None:
                    f.write("\n--- dmesg (kernel ring buffer) ---\n")
                    f.write(dmesg_dump or "(empty)\n")
            self.write(f"[FAIL] summary -> {summary}")
        except Exception as exc:
            self.write(f"[FAIL] could not write summary: {exc}")

        # 5) Optional Teams notification (no-op if no webhook configured).
        try:
            sent = notify.notify_failure(
                reason=reason,
                test_index=self.test_index,
                iteration=iter_idx,
                channel=self.ch_var.get(),
                length=self.len_var.get(),
                dut_version=os_version,
                findings=all_findings,
                summary_path=summary)
            if sent:
                self.write("[FAIL] Teams notification sent.")
            elif notify.get_webhook_url():
                self.write("[FAIL] Teams notification failed to send.")
        except Exception as exc:
            self.write(f"[FAIL] Teams notification error: {exc}")

    def _scan_logs_quick(self) -> list[str]:
        """Lightweight per-iteration scan: download logs into a temp folder
        and look for known failure patterns. Returns the list of findings.

        SSH can be momentarily unavailable right after ``bt_test_off.sh``
        (sshd restart / PAM hiccup), so we retry a few times with a short
        backoff before giving up. If all retries fail we just skip this
        iteration's deep-log scan - the rx_count==0 trigger still works,
        and the next iteration will usually succeed once sshd settles.
        """
        import time
        MAX_TRIES = 3
        BACKOFF_S = 2.0
        folder = None
        last_exc: Exception | None = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                # Force the persistent session to reconnect on each retry so
                # we don't keep reusing a half-dead transport.
                if attempt > 1:
                    try:
                        self.ssh.close()
                    except Exception:
                        pass
                folder = self._logs_folder(tag="scan")
                saved = dut_control.fetch_dut_logs(
                    self.ssh, folder,
                    extra_paths=(self._extra_log_paths_list()
                                 if self.is_manual() else []))
                findings = (
                    dut_control.analyze_dut_logs(
                        saved,
                        extra_patterns=(self._extra_fail_patterns_list()
                                        if self.is_manual() else []))
                    if saved else [])
                if not findings:
                    # Keep the workspace tidy when nothing interesting was found.
                    try:
                        for p in saved:
                            os.remove(p)
                        os.rmdir(folder)
                    except Exception:
                        pass
                if attempt > 1:
                    self.write(f"[SCAN] recovered on attempt {attempt}.")
                # Reset consecutive-failure counter on any success.
                self._scan_fail_streak = 0
                return findings
            except Exception as exc:
                last_exc = exc
                # Discard the (likely empty) folder we just created.
                if folder:
                    try:
                        os.rmdir(folder)
                    except Exception:
                        pass
                if attempt < MAX_TRIES:
                    self.write(f"[SCAN] attempt {attempt}/{MAX_TRIES} "
                               f"failed ({exc}); retrying in {BACKOFF_S:.0f}s.")
                    if self._auto_stop.wait(BACKOFF_S):
                        break  # user pressed STOP during backoff
        # All retries exhausted -> skip this scan, don't abort the AUTO loop.
        self._scan_fail_streak = getattr(self, "_scan_fail_streak", 0) + 1
        self.write(f"[SCAN] log scan skipped after {MAX_TRIES} tries: "
                   f"{last_exc}")
        if self._scan_fail_streak >= 5:
            self.write("[SCAN] WARNING: 5 consecutive log-scan failures - "
                       "DUT SSH appears unstable. AUTO loop continues, "
                       "but deep log analysis is currently unavailable.")
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
