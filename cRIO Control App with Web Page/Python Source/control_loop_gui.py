#!/usr/bin/env python3
"""
control_loop_gui.py
===================
Tkinter + matplotlib GUI client for the CompactRIO gRPC ControlLoopService.

Prerequisites
-------------
    pip install grpcio grpcio-tools protobuf matplotlib

Generate stubs (once):
    python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. control_loop.proto

Usage:
    python control_loop_gui.py
"""

from __future__ import annotations

import collections
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional

import grpc
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import control_loop_pb2 as pb2
import control_loop_pb2_grpc as pb2_grpc

# ────────────────────────────────────────────────────────────────────
#  Constants
# ────────────────────────────────────────────────────────────────────

MAX_POINTS = 500                   # rolling window for the plot
PLOT_REFRESH_MS = 100              # how often the canvas redraws (ms)
STATUS_LABEL_REFRESH_MS = 200      # how often numeric labels update (ms)

# Matplotlib colours (colour-blind-friendly palette)
COLORS = {
    "ai_value": "#0077BB",
    "ao_value": "#EE7733",
    "setpoint": "#009988",
    "error":    "#CC3311",
}

# ────────────────────────────────────────────────────────────────────
#  Application
# ────────────────────────────────────────────────────────────────────

class ControlLoopGUI:
    """Main application window."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("CompactRIO Control Loop Client")
        self.root.protocol("WM_DELETE_WINDOW", self._on_exit)
        self.root.minsize(960, 700)

        # gRPC state
        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[pb2_grpc.ControlLoopServiceStub] = None
        self._stream_context: Optional[grpc.Future] = None
        self._connected = False
        self._stream_lock = threading.Lock()

        # Data buffers (thread-safe via GIL for simple appends)
        self._timestamps: collections.deque[float] = collections.deque(maxlen=MAX_POINTS)
        self._ai_data: collections.deque[float] = collections.deque(maxlen=MAX_POINTS)
        self._ao_data: collections.deque[float] = collections.deque(maxlen=MAX_POINTS)
        self._sp_data: collections.deque[float] = collections.deque(maxlen=MAX_POINTS)
        self._err_data: collections.deque[float] = collections.deque(maxlen=MAX_POINTS)
        self._tick: int = 0

        # Latest status snapshot for the labels
        self._latest_status: Optional[pb2.ControlLoopStatus] = None

        self._build_ui()
        self._start_refresh_loops()

    # ────────────────────────────────────────────────────────────────
    #  UI construction
    # ────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── top: connection bar ──────────────────────────────────────
        conn_frame = ttk.LabelFrame(self.root, text="Connection", padding=8)
        conn_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(conn_frame, text="IP Address:").pack(side=tk.LEFT, padx=(0, 4))
        self._ip_var = tk.StringVar(value="localhost")
        ttk.Entry(conn_frame, textvariable=self._ip_var, width=18).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(conn_frame, text="Port:").pack(side=tk.LEFT, padx=(0, 4))
        self._port_var = tk.StringVar(value="50051")
        ttk.Entry(conn_frame, textvariable=self._port_var, width=8).pack(side=tk.LEFT, padx=(0, 10))

        self._conn_btn = ttk.Button(conn_frame, text="Connect", command=self._toggle_connection)
        self._conn_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._conn_status_var = tk.StringVar(value="Disconnected")
        self._conn_status_lbl = ttk.Label(conn_frame, textvariable=self._conn_status_var,
                                          foreground="red", font=("TkDefaultFont", 10, "bold"))
        self._conn_status_lbl.pack(side=tk.LEFT, padx=10)

        # ── middle: plot ─────────────────────────────────────────────
        plot_frame = ttk.LabelFrame(self.root, text="Live Data", padding=4)
        plot_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._fig = Figure(figsize=(9, 3.8), dpi=100, facecolor="#f0f0f0")
        self._ax = self._fig.add_subplot(111)
        self._configure_axes()

        self._canvas = FigureCanvasTkAgg(self._fig, master=plot_frame)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # ── status readouts ──────────────────────────────────────────
        readout_frame = ttk.LabelFrame(self.root, text="Current Values", padding=8)
        readout_frame.pack(fill=tk.X, padx=8, pady=4)

        labels = [
            ("AI Value (V):", "ai_val"),
            ("AO Value (V):", "ao_val"),
            ("Setpoint (V):", "sp_val"),
            ("Error (V):",    "err_val"),
            ("Kp:",           "kp_val"),
            ("Iteration:",    "iter_val"),
            ("AI Ovr:",       "ai_ovr"),
            ("AO Ovr:",       "ao_ovr"),
        ]
        self._readout_vars: dict[str, tk.StringVar] = {}
        for i, (text, key) in enumerate(labels):
            ttk.Label(readout_frame, text=text, font=("TkFixedFont", 10)).grid(
                row=0, column=i * 2, sticky=tk.E, padx=(8, 2))
            var = tk.StringVar(value="—")
            self._readout_vars[key] = var
            ttk.Label(readout_frame, textvariable=var, width=10,
                      font=("TkFixedFont", 10, "bold")).grid(
                row=0, column=i * 2 + 1, sticky=tk.W, padx=(0, 6))

        # ── bottom: controls ─────────────────────────────────────────
        ctrl_frame = ttk.LabelFrame(self.root, text="Controls", padding=8)
        ctrl_frame.pack(fill=tk.X, padx=8, pady=(4, 8))

        # -- AI Override row --
        ai_ovr_frame = ttk.Frame(ctrl_frame)
        ai_ovr_frame.pack(fill=tk.X, pady=3)
        ttk.Label(ai_ovr_frame, text="AI Override Value (V):").pack(side=tk.LEFT, padx=(0, 4))
        self._ai_ovr_var = tk.StringVar(value="0.0")
        ttk.Entry(ai_ovr_frame, textvariable=self._ai_ovr_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ai_ovr_frame, text="Enable AI Override",
                   command=lambda: self._set_ai_override(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(ai_ovr_frame, text="Disable AI Override",
                   command=lambda: self._set_ai_override(False)).pack(side=tk.LEFT, padx=2)

        # -- AO Override row --
        ao_ovr_frame = ttk.Frame(ctrl_frame)
        ao_ovr_frame.pack(fill=tk.X, pady=3)
        ttk.Label(ao_ovr_frame, text="AO Override Value (V):").pack(side=tk.LEFT, padx=(0, 4))
        self._ao_ovr_var = tk.StringVar(value="0.0")
        ttk.Entry(ao_ovr_frame, textvariable=self._ao_ovr_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ao_ovr_frame, text="Enable AO Override",
                   command=lambda: self._set_ao_override(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(ao_ovr_frame, text="Disable AO Override",
                   command=lambda: self._set_ao_override(False)).pack(side=tk.LEFT, padx=2)

        # -- Setpoint row --
        sp_frame = ttk.Frame(ctrl_frame)
        sp_frame.pack(fill=tk.X, pady=3)
        ttk.Label(sp_frame, text="Setpoint (V):").pack(side=tk.LEFT, padx=(0, 4))
        self._sp_var = tk.StringVar(value="0.0")
        ttk.Entry(sp_frame, textvariable=self._sp_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(sp_frame, text="Set Setpoint", command=self._set_setpoint).pack(side=tk.LEFT, padx=2)

        ttk.Label(sp_frame, text="      Kp:").pack(side=tk.LEFT, padx=(20, 4))
        self._kp_var = tk.StringVar(value="1.0")
        ttk.Entry(sp_frame, textvariable=self._kp_var, width=10).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(sp_frame, text="Set Gain", command=self._set_gain).pack(side=tk.LEFT, padx=2)

        # -- Action buttons row --
        btn_frame = ttk.Frame(ctrl_frame)
        btn_frame.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(btn_frame, text="Clear All Overrides", command=self._clear_overrides).pack(side=tk.LEFT, padx=4)
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=12)
        ttk.Button(btn_frame, text="Exit", command=self._on_exit).pack(side=tk.RIGHT, padx=4)

    # ────────────────────────────────────────────────────────────────
    #  Plot helpers
    # ────────────────────────────────────────────────────────────────

    def _configure_axes(self) -> None:
        self._ax.set_xlabel("Sample")
        self._ax.set_ylabel("Volts")
        self._ax.set_title("Control Loop — Live Data", fontsize=11)
        self._ax.grid(True, alpha=0.3)
        self._ax.set_xlim(0, MAX_POINTS)
        self._ax.set_ylim(-10.5, 10.5)

        # Pre-create the four line objects (empty data).
        self._lines = {
            "ai_value": self._ax.plot([], [], color=COLORS["ai_value"], linewidth=1.2, label="AI Value")[0],
            "ao_value": self._ax.plot([], [], color=COLORS["ao_value"], linewidth=1.2, label="AO Value")[0],
            "setpoint": self._ax.plot([], [], color=COLORS["setpoint"], linewidth=1.2, linestyle="--", label="Setpoint")[0],
            "error":    self._ax.plot([], [], color=COLORS["error"],    linewidth=1.0, alpha=0.7, label="Error")[0],
        }
        self._ax.legend(loc="upper right", fontsize=8, framealpha=0.8)
        self._fig.tight_layout()

    def _redraw_plot(self) -> None:
        if not self._timestamps:
            return

        xs = list(self._timestamps)
        self._lines["ai_value"].set_data(xs, list(self._ai_data))
        self._lines["ao_value"].set_data(xs, list(self._ao_data))
        self._lines["setpoint"].set_data(xs, list(self._sp_data))
        self._lines["error"].set_data(xs, list(self._err_data))

        # Auto-scroll the x-axis.
        xmin = xs[0]
        xmax = max(xs[-1], xmin + 50)
        self._ax.set_xlim(xmin, xmax)

        # Auto-scale y with some padding.
        all_vals = list(self._ai_data) + list(self._ao_data) + list(self._sp_data) + list(self._err_data)
        if all_vals:
            ymin = min(all_vals) - 0.5
            ymax = max(all_vals) + 0.5
            if ymin == ymax:
                ymin -= 1
                ymax += 1
            self._ax.set_ylim(ymin, ymax)

        self._canvas.draw_idle()

    # ────────────────────────────────────────────────────────────────
    #  Periodic refresh (called from the main Tk thread)
    # ────────────────────────────────────────────────────────────────

    def _start_refresh_loops(self) -> None:
        self._refresh_plot()
        self._refresh_labels()

    def _refresh_plot(self) -> None:
        self._redraw_plot()
        self.root.after(PLOT_REFRESH_MS, self._refresh_plot)

    def _refresh_labels(self) -> None:
        s = self._latest_status
        if s is not None:
            self._readout_vars["ai_val"].set(f"{s.ai_value:+.4f}")
            self._readout_vars["ao_val"].set(f"{s.ao_value:+.4f}")
            self._readout_vars["sp_val"].set(f"{s.setpoint:+.4f}")
            self._readout_vars["err_val"].set(f"{s.error:+.4f}")
            self._readout_vars["kp_val"].set(f"{s.kp:.4f}")
            self._readout_vars["iter_val"].set(str(s.iteration_count))
            self._readout_vars["ai_ovr"].set("ON" if s.ai_override_active else "off")
            self._readout_vars["ao_ovr"].set("ON" if s.ao_override_active else "off")
        self.root.after(STATUS_LABEL_REFRESH_MS, self._refresh_labels)

    # ────────────────────────────────────────────────────────────────
    #  gRPC connection / streaming
    # ────────────────────────────────────────────────────────────────

    def _toggle_connection(self) -> None:
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        ip = self._ip_var.get().strip()
        port = self._port_var.get().strip()
        if not ip or not port:
            messagebox.showwarning("Input Error", "Enter both IP address and port.")
            return

        target = f"{ip}:{port}"
        try:
            self._channel = grpc.insecure_channel(
                target,
                options=[
                    ("grpc.keepalive_time_ms", 10000),
                    ("grpc.keepalive_timeout_ms", 5000),
                ],
            )
            # Quick connectivity check (timeout 3 s).
            grpc.channel_ready_future(self._channel).result(timeout=3)
        except grpc.FutureTimeoutError:
            messagebox.showerror("Connection Failed",
                                 f"Could not reach {target} within 3 seconds.")
            self._channel.close()
            self._channel = None
            return
        except Exception as exc:
            messagebox.showerror("Connection Failed", str(exc))
            return

        self._stub = pb2_grpc.ControlLoopServiceStub(self._channel)
        self._connected = True
        self._conn_btn.configure(text="Disconnect")
        self._conn_status_var.set(f"Connected to {target}")
        self._conn_status_lbl.configure(foreground="green")

        # Clear old plot data.
        self._timestamps.clear()
        self._ai_data.clear()
        self._ao_data.clear()
        self._sp_data.clear()
        self._err_data.clear()
        self._tick = 0

        # Start the streaming thread.
        self._stream_thread = threading.Thread(target=self._stream_worker, daemon=True)
        self._stream_thread.start()

    def _disconnect(self) -> None:
        self._connected = False
        with self._stream_lock:
            if self._stream_context is not None:
                self._stream_context.cancel()
                self._stream_context = None
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        self._stub = None
        self._conn_btn.configure(text="Connect")
        self._conn_status_var.set("Disconnected")
        self._conn_status_lbl.configure(foreground="red")

    def _stream_worker(self) -> None:
        """Background thread that consumes the StreamStatus server-stream."""
        try:
            stream = self._stub.StreamStatus(pb2.Empty())
            with self._stream_lock:
                self._stream_context = stream

            for status in stream:
                if not self._connected:
                    break

                # Store latest snapshot for the label updater.
                self._latest_status = status

                # Append to the rolling data buffers.
                self._tick += 1
                self._timestamps.append(self._tick)
                self._ai_data.append(status.ai_value)
                self._ao_data.append(status.ao_value)
                self._sp_data.append(status.setpoint)
                self._err_data.append(status.error)

        except grpc.RpcError as rpc_err:
            if self._connected:
                # Schedule the error dialog on the main thread.
                self.root.after(0, lambda: messagebox.showerror(
                    "Stream Error", f"gRPC stream lost:\n{rpc_err.details()}"))
                self.root.after(0, self._disconnect)

    # ────────────────────────────────────────────────────────────────
    #  RPC action helpers (run in short-lived threads)
    # ────────────────────────────────────────────────────────────────

    def _rpc_call(self, fn, request, label: str) -> None:
        """Fire an RPC in a background thread so the UI never blocks."""
        if not self._connected or self._stub is None:
            messagebox.showwarning("Not Connected", "Connect to the server first.")
            return

        def _worker():
            try:
                ack = fn(request)
                self.root.after(0, lambda: self._flash_status(f"{label}: {ack.message}"))
            except grpc.RpcError as exc:
                self.root.after(0, lambda: messagebox.showerror(
                    "RPC Error", f"{label} failed:\n{exc.details()}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _flash_status(self, msg: str) -> None:
        """Briefly show an RPC result in the connection-status label."""
        old = self._conn_status_var.get()
        self._conn_status_var.set(msg)
        self.root.after(2000, lambda: self._conn_status_var.set(old))

    # ── button callbacks ─────────────────────────────────────────────

    def _set_ai_override(self, enable: bool) -> None:
        try:
            val = float(self._ai_ovr_var.get())
        except ValueError:
            messagebox.showwarning("Input Error", "AI override value must be a number.")
            return
        req = pb2.AIOverrideRequest(enable=enable, value=val)
        self._rpc_call(self._stub.SetAIOverride, req, "AI Override")

    def _set_ao_override(self, enable: bool) -> None:
        try:
            val = float(self._ao_ovr_var.get())
        except ValueError:
            messagebox.showwarning("Input Error", "AO override value must be a number.")
            return
        req = pb2.AOOverrideRequest(enable=enable, value=val)
        self._rpc_call(self._stub.SetAOOverride, req, "AO Override")

    def _set_setpoint(self) -> None:
        try:
            val = float(self._sp_var.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Setpoint must be a number.")
            return
        req = pb2.SetpointRequest(setpoint=val)
        self._rpc_call(self._stub.SetSetpoint, req, "Setpoint")

    def _set_gain(self) -> None:
        try:
            val = float(self._kp_var.get())
        except ValueError:
            messagebox.showwarning("Input Error", "Kp must be a number.")
            return
        req = pb2.GainRequest(kp=val)
        self._rpc_call(self._stub.SetGain, req, "Gain")

    def _clear_overrides(self) -> None:
        self._rpc_call(self._stub.ClearOverrides, pb2.Empty(), "Clear Overrides")

    # ────────────────────────────────────────────────────────────────
    #  Shutdown
    # ────────────────────────────────────────────────────────────────

    def _on_exit(self) -> None:
        self._disconnect()
        self.root.quit()
        self.root.destroy()


# ────────────────────────────────────────────────────────────────────
#  Entry point
# ────────────────────────────────────────────────────────────────────

def main() -> None:
    root = tk.Tk()
    _app = ControlLoopGUI(root)        # noqa: F841  (prevent GC)
    root.mainloop()


if __name__ == "__main__":
    main()
