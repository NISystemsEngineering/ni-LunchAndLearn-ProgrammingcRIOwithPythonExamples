import paramiko
import platform
import os


def ssh_run_python_script(
    hostname,
    username,
    remote_directory,
    python_script,
    password=None,
    key_filepath=None,
    port=22,
    python_interpreter="python3",
    script_args=None
):
    """
    SSHs into a Linux machine, changes to a directory, and executes a Python script.

    Args:
        hostname (str): IP address or hostname of the Linux machine
        username (str): SSH username
        remote_directory (str): Directory to cd into on the remote machine
        python_script (str): Python script filename to execute
        password (str): SSH password (optional if using key)
        key_filepath (str): Path to private SSH key file (optional if using password)
        port (int): SSH port, default 22
        python_interpreter (str): Python interpreter to use (python3, python, etc.)
        script_args (list): Optional list of arguments to pass to the script
    """

    # --- Step 1: OS Check ---
    current_os = platform.system()
    print(f"Running on: {current_os}")

    # --- Step 2: Build the remote command ---
    # Combine args into a string if provided
    args_str = " ".join(script_args) if script_args else ""

    # Build full command: cd into directory, then run the python script
    remote_command = f"cd {remote_directory} && {python_interpreter} {python_script} {args_str}".strip()
    print(f"Remote command: {remote_command}")

    # --- Step 3: Connect via SSH ---
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print(f"Connecting to {hostname} using password...")
        ssh.connect(hostname, port=port, username=username, password=password)
        with ssh.open_sftp() as sftp:
            remote_filepath = remote_directory + "/" + python_script
            local_filepath = os.getcwd() + "\\" +  python_script
            print(f"Transferring '{local_filepath}' -> '{hostname}:{remote_filepath}'...")
            sftp.put(local_filepath, remote_filepath)

            # --- Step 5: Verify file exists on remote ---
            remote_file_size = sftp.stat(remote_filepath).st_size
            local_file_size = os.path.getsize(local_filepath)

            if remote_file_size != local_file_size:
                raise IOError(
                    f"File size mismatch after transfer! "
                    f"Local: {local_file_size} bytes, Remote: {remote_file_size} bytes"
                )

            print(f"Transfer verified. File size: {remote_file_size} bytes")

        print(f"Connected to {hostname}\n")

        # --- Step 4: Execute the remote command ---
        print(f"Changing to directory: {remote_directory}")
        print(f"Executing script: {python_script}\n")
        print("-" * 50)

        stdin, stdout, stderr = ssh.exec_command(remote_command)

        # --- Step 5: Stream and print stdout in real time ---
        print("Output:")
        for line in iter(stdout.readline, ""):
            print(f"  {line}", end="")

        # --- Step 6: Print any errors ---
        error_output = stderr.read().decode()
        if error_output:
            print("\nErrors:")
            for line in error_output.splitlines():
                print(f"  {line}")

        # --- Step 7: Check exit code ---
        exit_code = stdout.channel.recv_exit_status()
        print("-" * 50)

        if exit_code == 0:
            print(f"\nScript '{python_script}' executed successfully. Exit code: {exit_code}")
        else:
            print(f"\nScript '{python_script}' finished with errors. Exit code: {exit_code}")

    except paramiko.AuthenticationException:
        print("Authentication failed. Check your username, password, or SSH key.")
    except paramiko.SSHException as e:
        print(f"SSH connection error: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")
    finally:
        ssh.close()
        print("SSH connection closed.")


# Using a virtual environment's Python interpreter
ssh_run_python_script(
    hostname="192.168.68.67",
    username="admin",
    remote_directory="/home/lvuser/natinst/PythonApp/",
    python_script="BasicDAQmxPythonExample.py",
    password = "ni",
    python_interpreter="/home/lvuser/natinst/PythonApp/venv/bin/python"
)