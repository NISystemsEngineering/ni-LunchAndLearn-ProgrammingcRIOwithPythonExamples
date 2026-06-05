#!/usr/bin/env python3
"""
ssh_deploy_run.py
=================
Transfer files to a remote target over SSH, execute a Python script,
and stream its console output (stdout + stderr) until the remote
process exits or the user presses Ctrl-C.

Dependencies
------------
    pip install paramiko

Examples
--------
    # Password auth — deploy two files, run main.py on the target:
    python ssh_deploy_run.py \\
        --host 192.168.1.100 --user admin --password mypass \\
        --files control_loop.proto control_loop_pb2.py crio_control_loop.py \\
        --remote-dir /home/admin/app \\
        --run crio_control_loop.py \\
        --args "--device cRIO1/ --rate 10"

    # Key-based auth — deploy everything in a manifest:
    python ssh_deploy_run.py \\
        --host 192.168.1.100 --user admin --key ~/.ssh/id_rsa \\
        --files $(cat deploy_manifest.txt) \\
        --remote-dir /home/admin/app \\
        --run crio_control_loop.py

    # Activate a venv inside the remote dir before running:
    python ssh_deploy_run.py \\
        --host 192.168.1.100 --user admin --key ~/.ssh/id_rsa \\
        --files control_loop_pb2.py crio_control_loop.py \\
        --remote-dir /home/admin/app \\
        --venv .venv \\
        --run crio_control_loop.py

    # Use an absolute venv path elsewhere on the target:
    python ssh_deploy_run.py \\
        --host 10.0.0.50 --port 2222 --user lvuser \\
        --key ~/.ssh/crio_key \\
        --files my_app.py helpers.py \\
        --remote-dir /home/lvuser/project \\
        --venv /opt/envs/myproject \\
        --run my_app.py
"""

from __future__ import annotations

import argparse
import getpass
import os
import posixpath
import signal
import sys
import time
from typing import Optional

import paramiko


# ────────────────────────────────────────────────────────────────────
#  File transfer
# ────────────────────────────────────────────────────────────────────

def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """Recursively create *remote_dir* on the target if it doesn't exist."""
    dirs_to_create: list[str] = []
    current = remote_dir

    while True:
        try:
            sftp.stat(current)
            break                       # this level exists
        except FileNotFoundError:
            dirs_to_create.append(current)
            parent = posixpath.dirname(current)
            if parent == current:       # reached root
                break
            current = parent

    for d in reversed(dirs_to_create):
        print(f"  Creating remote directory: {d}")
        sftp.mkdir(d)


def transfer_files(
    sftp: paramiko.SFTPClient,
    local_files: list[str],
    remote_dir: str,
) -> list[str]:
    """
    Copy every file in *local_files* into *remote_dir* on the target.

    Returns the list of full remote paths that were written.
    """
    ensure_remote_dir(sftp, remote_dir)
    remote_paths: list[str] = []

    for local_path in local_files:
        if not os.path.isfile(local_path):
            print(f"  WARNING: '{local_path}' not found locally — skipping.")
            continue

        filename = os.path.basename(local_path)
        remote_path = posixpath.join(remote_dir, filename)
        file_size = os.path.getsize(local_path)

        print(f"  {local_path}  ->  {remote_path}  ({file_size:,} bytes) … ", end="", flush=True)
        sftp.put(local_path, remote_path)
        # Preserve the executable bit so scripts can be run directly.
        local_mode = os.stat(local_path).st_mode
        sftp.chmod(remote_path, local_mode & 0o7777)
        print("OK")
        remote_paths.append(remote_path)

    return remote_paths


# ────────────────────────────────────────────────────────────────────
#  Remote execution + live output streaming
# ────────────────────────────────────────────────────────────────────

def run_remote(
    client: paramiko.SSHClient,
    remote_dir: str,
    script_name: str,
    python_bin: str,
    extra_args: str,
    venv_path: Optional[str] = None,
) -> int:
    """
    Execute *script_name* on the remote host inside *remote_dir* and
    stream both stdout and stderr to the local console in real time.

    If *venv_path* is given the virtual-environment's ``activate``
    script is sourced first.  A relative path is resolved against
    *remote_dir*; an absolute path is used as-is.

    Returns the remote process exit code.
    """
    remote_script = posixpath.join(remote_dir, script_name)

    # Build the shell command, optionally prefixed with venv activation.
    parts: list[str] = [f"cd {remote_dir}"]

    if venv_path:
        # Resolve relative paths against the remote working directory.
        if not posixpath.isabs(venv_path):
            venv_path = posixpath.join(remote_dir, venv_path)
        activate = posixpath.join(venv_path, "bin", "activate")
        parts.append(f"source {activate}")

    # When a venv is active its python sits at the front of $PATH, so
    # the default "python3" (or whatever --python is) will resolve to
    # the venv interpreter automatically.
    parts.append(f"{python_bin} -u {remote_script}")

    command = " && ".join(parts)
    if extra_args:
        command += f" {extra_args}"

    print(f"\n{'─' * 60}")
    print(f"  Executing: {command}")
    print(f"  Press Ctrl-C to stop.")
    print(f"{'─' * 60}\n")

    # Open an interactive-ish channel so we get a PTY and can
    # forward Ctrl-C as a signal to the remote process.
    transport = client.get_transport()
    channel = transport.open_session()
    channel.get_pty()
    channel.exec_command(command)

    # Make the channel non-blocking so we can multiplex stdout/stderr
    # while also checking for Ctrl-C on the local side.
    channel.setblocking(False)

    try:
        while True:
            # ── read stdout ──────────────────────────────────────────
            if channel.recv_ready():
                chunk = channel.recv(4096)
                if chunk:
                    sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                    sys.stdout.flush()

            # ── read stderr ──────────────────────────────────────────
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096)
                if chunk:
                    sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                    sys.stderr.flush()

            # ── check if remote process has finished ─────────────────
            if channel.exit_status_ready():
                # Drain any remaining bytes.
                while channel.recv_ready():
                    sys.stdout.write(
                        channel.recv(4096).decode("utf-8", errors="replace"))
                while channel.recv_stderr_ready():
                    sys.stderr.write(
                        channel.recv_stderr(4096).decode("utf-8", errors="replace"))
                break

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\n>>> Ctrl-C caught — sending interrupt to remote process …")
        # Send the interrupt character (Ctrl-C / 0x03) over the PTY.
        channel.send(b"\x03")
        # Give the remote side a moment to wind down.
        time.sleep(1.0)
        # Drain final output.
        while channel.recv_ready():
            sys.stdout.write(
                channel.recv(4096).decode("utf-8", errors="replace"))

    exit_code = channel.recv_exit_status()
    channel.close()
    return exit_code


# ────────────────────────────────────────────────────────────────────
#  SSH connection factory
# ────────────────────────────────────────────────────────────────────

def connect(
    host: str,
    port: int,
    user: str,
    password: Optional[str],
    key_path: Optional[str],
    timeout: float,
) -> paramiko.SSHClient:
    """Return a connected SSHClient using either key or password auth."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = dict(
        hostname=host,
        port=port,
        username=user,
        timeout=timeout,
    )

    if key_path:
        print(f"Authenticating with key: {key_path}")
        connect_kwargs["key_filename"] = os.path.expanduser(key_path)
    elif password:
        connect_kwargs["password"] = password
    else:
        # Fall back to interactive prompt.
        connect_kwargs["password"] = getpass.getpass(
            f"Password for {user}@{host}: ")

    client.connect(**connect_kwargs)
    print(f"Connected to {user}@{host}:{port}\n")
    return client


# ────────────────────────────────────────────────────────────────────
#  CLI
# ────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Deploy files to a remote target over SSH, run a Python "
                    "script, and stream its output until it exits or Ctrl-C.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Connection
    conn = p.add_argument_group("connection")
    conn.add_argument("--host", required=True,
                       help="Remote hostname or IP address.")
    conn.add_argument("--port", type=int, default=22,
                       help="SSH port (default: 22).")
    conn.add_argument("--user", required=True,
                       help="SSH username.")
    conn.add_argument("--password", default=None,
                       help="SSH password (omit to use key auth or be prompted).")
    conn.add_argument("--key", default=None, metavar="PATH",
                       help="Path to SSH private key file.")
    conn.add_argument("--timeout", type=float, default=10.0,
                       help="Connection timeout in seconds (default: 10).")

    # File transfer
    xfer = p.add_argument_group("file transfer")
    xfer.add_argument("--files", nargs="+", required=True, metavar="FILE",
                       help="Local files to copy to the remote target.")
    xfer.add_argument("--remote-dir", default="/tmp/deploy",
                       help="Remote directory to upload files into "
                            "(default: /tmp/deploy).")

    # Execution
    exe = p.add_argument_group("execution")
    exe.add_argument("--run", required=True, metavar="SCRIPT.py",
                      help="Name of the Python file to execute on the target "
                           "(must be one of the --files entries, or already "
                           "present in --remote-dir).")
    exe.add_argument("--venv", default=None, metavar="PATH",
                      help="Path to a Python virtual environment on the remote "
                           "host.  Can be relative to --remote-dir (e.g. "
                           "'.venv') or absolute (e.g. '/opt/envs/myapp').  "
                           "The venv's bin/activate is sourced before running "
                           "the script.")
    exe.add_argument("--python", default="python3",
                      help="Python interpreter path on the remote host "
                           "(default: python3).")
    exe.add_argument("--args", default="", metavar='"--flag val"',
                      help="Extra CLI arguments forwarded to the remote script "
                           "(quote the whole string).")

    return p.parse_args()


# ────────────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Ignore SIGINT during the initial transfer phase; we handle it
    # ourselves inside run_remote().
    original_sigint = signal.getsignal(signal.SIGINT)

    client: Optional[paramiko.SSHClient] = None

    try:
        # 1. Connect.
        client = connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            key_path=args.key,
            timeout=args.timeout,
        )

        # 2. Transfer files.
        print("Transferring files …")
        sftp = client.open_sftp()
        transfer_files(sftp, args.files, args.remote_dir)
        sftp.close()
        print(f"\nAll files uploaded to {args.remote_dir}\n")

        # 3. Execute and stream output.
        signal.signal(signal.SIGINT, original_sigint)
        exit_code = run_remote(
            client,
            remote_dir=args.remote_dir,
            script_name=args.run,
            python_bin=args.python,
            extra_args=args.args,
            venv_path=args.venv,
        )

        print(f"\n{'─' * 60}")
        print(f"  Remote process exited with code {exit_code}")
        print(f"{'─' * 60}")
        sys.exit(exit_code)

    except paramiko.AuthenticationException:
        print("ERROR: SSH authentication failed. Check credentials.", file=sys.stderr)
        sys.exit(1)
    except paramiko.SSHException as exc:
        print(f"ERROR: SSH error — {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: Network error — {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if client:
            client.close()
            print("SSH connection closed.")


if __name__ == "__main__":
    main()