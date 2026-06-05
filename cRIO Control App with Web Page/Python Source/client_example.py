#!/usr/bin/env python3
"""
client_example.py
=================
Quick gRPC client demonstrating every RPC on the ControlLoopService.

Usage:
    python client_example.py [--host localhost --port 50051]
"""

from __future__ import annotations

import argparse
import time

import grpc

import control_loop_pb2 as pb2
import control_loop_pb2_grpc as pb2_grpc


def run(host: str, port: int) -> None:
    target = f"{host}:{port}"
    print(f"Connecting to {target} …")

    with grpc.insecure_channel(target) as channel:
        stub = pb2_grpc.ControlLoopServiceStub(channel)

        # 1. Get a single status snapshot.
        status = stub.GetStatus(pb2.Empty())
        print("\n── Current Status ──")
        print(f"  AI value       : {status.ai_value:.4f} V")
        print(f"  AO value       : {status.ao_value:.4f} V")
        print(f"  Setpoint       : {status.setpoint:.4f} V")
        print(f"  Error          : {status.error:.4f} V")
        print(f"  Kp             : {status.kp:.4f}")
        print(f"  Loop running   : {status.loop_running}")
        print(f"  Iterations     : {status.iteration_count}")
        print(f"  AI override    : {'ON' if status.ai_override_active else 'off'}")
        print(f"  AO override    : {'ON' if status.ao_override_active else 'off'}")

        # 2. Change the setpoint to 2.5 V.
        ack = stub.SetSetpoint(pb2.SetpointRequest(setpoint=2.5))
        print(f"\nSetSetpoint → {ack.message}")

        # 3. Change proportional gain.
        ack = stub.SetGain(pb2.GainRequest(kp=0.8))
        print(f"SetGain     → {ack.message}")

        # 4. Override the AI reading to a fixed 1.0 V.
        ack = stub.SetAIOverride(pb2.AIOverrideRequest(enable=True, value=1.0))
        print(f"SetAIOverride → {ack.message}")

        # 5. Override the AO output to a fixed 3.3 V.
        ack = stub.SetAOOverride(pb2.AOOverrideRequest(enable=True, value=3.3))
        print(f"SetAOOverride → {ack.message}")

        # 6. Stream a few status updates.
        print("\n── Streaming status (5 updates) ──")
        stream = stub.StreamStatus(pb2.Empty())
        for i, status in enumerate(stream):
            if i >= 5:
                stream.cancel()
                break
            print(
                f"  [{i}] AI={status.ai_value:+.4f}  AO={status.ao_value:+.4f}  "
                f"err={status.error:+.4f}  iter={status.iteration_count}"
            )
            time.sleep(0.15)

        # 7. Clear all overrides.
        ack = stub.ClearOverrides(pb2.Empty())
        print(f"\nClearOverrides → {ack.message}")

        # Final status.
        status = stub.GetStatus(pb2.Empty())
        print(f"\nFinal status  : AI={status.ai_value:+.4f} V  "
              f"AO={status.ao_value:+.4f} V  setpoint={status.setpoint:.4f} V")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.68.64")
    parser.add_argument("--port", type=int, default=50051)
    args = parser.parse_args()
    run(args.host, args.port)
