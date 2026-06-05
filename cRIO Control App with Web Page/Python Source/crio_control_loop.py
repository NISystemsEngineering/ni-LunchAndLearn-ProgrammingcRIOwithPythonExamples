#!/usr/bin/env python3
"""
crio_control_loop.py
====================
10 Hz proportional closed-loop controller running on an NI CompactRIO.

Hardware layout
---------------
* Analog Input  : Mod5/ai0  – differential terminal config, on-demand sampling
* Analog Output : Mod3/ao0  – on-demand (single-point) writes

The script also starts a gRPC server (default port 50051) that exposes
real-time status and accepts runtime overrides for AI / AO values,
setpoint, and gain.

Dependencies
------------
    pip install nidaqmx grpcio grpcio-tools protobuf

Generate the gRPC stubs once before first run:
    python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. control_loop.proto
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field

import grpc
import nidaqmx
from nidaqmx.constants import TerminalConfiguration

# ── generated gRPC stubs ────────────────────────────────────────────
import control_loop_pb2 as pb2
import control_loop_pb2_grpc as pb2_grpc

import locale

locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')

# ────────────────────────────────────────────────────────────────────
#  Dashboard server (server.py) integration
# ────────────────────────────────────────────────────────────────────

# server.py reads variable values from this file and serves them on its
# /data endpoint.  We publish the live control-loop state here so the
# dashboard shows real data.  (Path must match VARIABLES_FILE in server.py.)
VARIABLES_FILE = "/tmp/crio_variables.json"

# server.py is expected to live alongside this script by default.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SERVER_SCRIPT = os.path.join(SCRIPT_DIR, "server.py")


def launch_dashboard_server(script_path: str, port: int) -> subprocess.Popen | None:
    """Start server.py as a parallel child process and return its handle."""
    if not os.path.exists(script_path):
        logging.warning(
            "Dashboard server script not found at %s — running without it.",
            script_path,
        )
        return None
    cmd = [sys.executable, script_path, "--port", str(port)]
    logging.info("Launching dashboard server: %s", " ".join(cmd))
    return subprocess.Popen(cmd)


def stop_dashboard_server(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """Terminate the dashboard server child process if it is still running."""
    if proc is None or proc.poll() is not None:
        return
    logging.info("Stopping dashboard server (pid %d)…", proc.pid)
    proc.terminate()                     # SIGTERM → server.py shuts down cleanly
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.warning("Dashboard server did not exit in time; killing.")
        proc.kill()
        proc.wait()
    logging.info("Dashboard server stopped.")


def write_variables_file(state: "ControlState") -> None:
    """Snapshot the control state to VARIABLES_FILE for server.py to serve."""
    with state.lock:
        data = {
            "_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "variables": {
                "AI_Value_V":      round(state.ai_value, 4),
                "AO_Value_V":      round(state.ao_value, 4),
                "Setpoint_V":      round(state.setpoint, 4),
                "Error_V":         round(state.error, 4),
                "Kp":              round(state.kp, 4),
                "AI_Override":     state.ai_override_active,
                "AO_Override":     state.ao_override_active,
                "Loop_Running":    state.loop_running,
                "Iteration_Count": state.iteration_count,
            },
        }
    # Write to a temp file then atomically replace, so server.py never reads
    # a half-written file (which would cause a JSONDecodeError on its end).
    tmp_path = VARIABLES_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, VARIABLES_FILE)
    except OSError as exc:
        logging.debug("Could not write %s: %s", VARIABLES_FILE, exc)


# ────────────────────────────────────────────────────────────────────
#  Shared state (thread-safe via a lock)
# ────────────────────────────────────────────────────────────────────

@dataclass
class ControlState:
    """Mutable state shared between the control thread and gRPC handlers."""

    # Process values
    ai_value: float = 0.0
    ao_value: float = 0.0
    setpoint: float = 0.0
    error: float = 0.0

    # AI override
    ai_override_active: bool = False
    ai_override_value: float = 0.0

    # AO override
    ao_override_active: bool = False
    ao_override_value: float = 0.0

    # Controller tuning
    kp: float = 1.0

    # Bookkeeping
    loop_running: bool = False
    iteration_count: int = 0

    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── convenience helpers ──────────────────────────────────────────

    def to_proto(self) -> pb2.ControlLoopStatus:
        """Return a protobuf snapshot while holding the lock."""
        with self.lock:
            return pb2.ControlLoopStatus(
                ai_value=self.ai_value,
                ao_value=self.ao_value,
                setpoint=self.setpoint,
                error=self.error,
                ai_override_active=self.ai_override_active,
                ai_override_value=self.ai_override_value,
                ao_override_active=self.ao_override_active,
                ao_override_value=self.ao_override_value,
                kp=self.kp,
                loop_running=self.loop_running,
                iteration_count=self.iteration_count,
            )


# ────────────────────────────────────────────────────────────────────
#  gRPC service implementation
# ────────────────────────────────────────────────────────────────────

class ControlLoopServicer(pb2_grpc.ControlLoopServiceServicer):
    """Thin layer that reads / mutates the shared *ControlState*."""

    def __init__(self, state: ControlState) -> None:
        self._state = state

    # ── queries ──────────────────────────────────────────────────────

    def GetStatus(self, request, context):
        return self._state.to_proto()

    def StreamStatus(self, request, context):
        """Yield a status snapshot every ~100 ms while the client listens."""
        while context.is_active():
            yield self._state.to_proto()
            time.sleep(0.1)

    # ── mutations ────────────────────────────────────────────────────

    def SetAIOverride(self, request, context):
        with self._state.lock:
            self._state.ai_override_active = request.enable
            self._state.ai_override_value = request.value
        action = "enabled" if request.enable else "disabled"
        msg = f"AI override {action} (value={request.value:.4f} V)"
        logging.info(msg)
        return pb2.AckResponse(success=True, message=msg)

    def SetAOOverride(self, request, context):
        with self._state.lock:
            self._state.ao_override_active = request.enable
            self._state.ao_override_value = request.value
        action = "enabled" if request.enable else "disabled"
        msg = f"AO override {action} (value={request.value:.4f} V)"
        logging.info(msg)
        return pb2.AckResponse(success=True, message=msg)

    def SetSetpoint(self, request, context):
        with self._state.lock:
            self._state.setpoint = request.setpoint
        msg = f"Setpoint changed to {request.setpoint:.4f} V"
        logging.info(msg)
        return pb2.AckResponse(success=True, message=msg)

    def SetGain(self, request, context):
        with self._state.lock:
            self._state.kp = request.kp
        msg = f"Kp changed to {request.kp:.4f}"
        logging.info(msg)
        return pb2.AckResponse(success=True, message=msg)

    def ClearOverrides(self, request, context):
        with self._state.lock:
            self._state.ai_override_active = False
            self._state.ai_override_value = 0.0
            self._state.ao_override_active = False
            self._state.ao_override_value = 0.0
        msg = "All overrides cleared"
        logging.info(msg)
        return pb2.AckResponse(success=True, message=msg)


# ────────────────────────────────────────────────────────────────────
#  DAQmx helpers
# ────────────────────────────────────────────────────────────────────

def create_ai_task(device_prefix: str, channel: str) -> nidaqmx.Task:
    """
    Create an analog-input task on **Mod5** with differential terminals
    and on-demand (no sample clock) acquisition.
    """
    task = nidaqmx.Task(new_task_name="AI_Task")
    phys_chan = f"Mod5/{channel}"
    task.ai_channels.add_ai_voltage_chan(
        phys_chan,
        terminal_config=TerminalConfiguration.DIFF,
        min_val=-10.0,
        max_val=10.0,
    )
    # On-demand: no sample clock configured – each read() returns one sample.
    logging.info("AI task created on %s (differential, on-demand)", phys_chan)
    return task


def create_ao_task(device_prefix: str, channel: str) -> nidaqmx.Task:
    """
    Create an analog-output task on **Mod3** with on-demand writes.
    """
    task = nidaqmx.Task(new_task_name="AO_Task")
    phys_chan = f"Mod3/{channel}"
    task.ao_channels.add_ao_voltage_chan(
        phys_chan,
        min_val=-10.0,
        max_val=10.0,
    )
    logging.info("AO task created on %s (on-demand)", phys_chan)
    return task


# ────────────────────────────────────────────────────────────────────
#  Control loop
# ────────────────────────────────────────────────────────────────────

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def control_loop(
    state: ControlState,
    stop_event: threading.Event,
    device_prefix: str,
    rate_hz: float = 10.0,
) -> None:
    """
    Run a simple proportional controller at *rate_hz*.

    Each iteration:
        1. Read AI (or use override).
        2. Compute error = setpoint − ai_value.
        3. Compute controller output  ao = Kp × error  (or use override).
        4. Write AO.
    """
    period = 1.0 / rate_hz

    ai_task = create_ai_task(device_prefix, "ai0")
    ao_task = create_ao_task(device_prefix, "ao0")

    try:
        ai_task.start()
        ao_task.start()

        with state.lock:
            state.loop_running = True

        logging.info("Control loop running at %.1f Hz", rate_hz)

        last_publish = 0.0   # monotonic timestamp of last dashboard-file write

        while not stop_event.is_set():
            t_start = time.monotonic()

            # 1. Acquire the process variable.
            with state.lock:
                if state.ai_override_active:
                    ai_reading = state.ai_override_value
                else:
                    ai_reading = ai_task.read()

            # 2. Compute error and controller output.
            with state.lock:
                state.ai_value = ai_reading
                state.error = state.setpoint - ai_reading
                kp = state.kp

            controller_output = kp * state.error

            # 3. Apply AO override if active; otherwise write controller output.
            with state.lock:
                if state.ao_override_active:
                    ao_write_value = state.ao_override_value
                else:
                    ao_write_value = clamp(controller_output, -10.0, 10.0)
                state.ao_value = ao_write_value

            ao_task.write(ao_write_value)

            # 4. Bookkeeping.
            with state.lock:
                state.iteration_count += 1

            # 5. Publish state to the dashboard file (throttled to ~5 Hz so we
            #    don't thrash the filesystem at the full loop rate).
            now = time.monotonic()
            if now - last_publish >= 0.2:
                write_variables_file(state)
                last_publish = now

            # Maintain the target period.
            elapsed = time.monotonic() - t_start
            sleep_time = period - elapsed
            if sleep_time > 0:
                stop_event.wait(timeout=sleep_time)

    except nidaqmx.DaqError as exc:
        logging.error("DAQmx error: %s", exc)
    finally:
        with state.lock:
            state.loop_running = False

        # Safely shut down hardware.
        try:
            ao_task.write(0.0)          # Drive output to 0 V on exit.
        except nidaqmx.DaqError:
            pass

        ao_task.stop()
        ao_task.close()
        ai_task.stop()
        ai_task.close()
        logging.info("Control loop stopped; tasks closed.")


# ────────────────────────────────────────────────────────────────────
#  Application entry point
# ────────────────────────────────────────────────────────────────────

def serve(args: argparse.Namespace) -> None:
    state = ControlState(setpoint=args.setpoint, kp=args.kp)
    stop_event = threading.Event()

    # ── start the dashboard server (server.py) in parallel ───────────
    dashboard_proc = None
    if not args.no_dashboard:
        dashboard_proc = launch_dashboard_server(args.server_script, args.dashboard_port)

    # ── start gRPC server ────────────────────────────────────────────
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_ControlLoopServiceServicer_to_server(
        ControlLoopServicer(state), server,
    )
    listen_addr = f"[::]:{args.port}"
    server.add_insecure_port(listen_addr)
    server.start()
    logging.info("gRPC server listening on %s", listen_addr)

    # ── start control loop in a background thread ────────────────────
    loop_thread = threading.Thread(
        target=control_loop,
        args=(state, stop_event, args.device, args.rate),
        daemon=True,
    )
    loop_thread.start()

    # ── graceful shutdown on SIGINT / SIGTERM ─────────────────────────
    def _shutdown(signum, frame):
        logging.info("Caught signal %d – shutting down…", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        # Wait for the control loop to finish.  If the dashboard process dies
        # on its own, bring the control loop down too so the two stay in sync.
        while loop_thread.is_alive():
            loop_thread.join(timeout=0.5)
            if dashboard_proc is not None and dashboard_proc.poll() is not None:
                logging.warning(
                    "Dashboard server exited (code %s); stopping control loop.",
                    dashboard_proc.returncode,
                )
                stop_event.set()
                loop_thread.join()
    finally:
        # Whenever the control loop stops, tear everything down — including
        # the dashboard server child process.
        stop_event.set()
        server.stop(grace=2).wait()
        stop_dashboard_server(dashboard_proc)
        logging.info("Server stopped.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="10 Hz closed-loop controller on NI CompactRIO with gRPC interface",
    )
    parser.add_argument(
        "--device", default="cRIO1/",
        help="NI-DAQmx device prefix including trailing slash (default: cRIO1/)",
    )
    parser.add_argument(
        "--rate", type=float, default=10.0,
        help="Control-loop rate in Hz (default: 10)",
    )
    parser.add_argument(
        "--setpoint", type=float, default=0.0,
        help="Initial setpoint in volts (default: 0.0)",
    )
    parser.add_argument(
        "--kp", type=float, default=1.0,
        help="Initial proportional gain (default: 1.0)",
    )
    parser.add_argument(
        "--port", type=int, default=50051,
        help="gRPC listen port (default: 50051)",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=8181,
        help="HTTP port for the dashboard server.py (default: 8181)",
    )
    parser.add_argument(
        "--server-script", default=DEFAULT_SERVER_SCRIPT,
        help="Path to server.py (default: alongside this script)",
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Do not launch the dashboard server.py",
    )
    serve(parser.parse_args())


if __name__ == "__main__":
    main()
