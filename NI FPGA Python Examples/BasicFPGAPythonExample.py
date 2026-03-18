import nifpga
import time

def run_fpga(
    bitfile_path,
    resource,
    indicator_name,
    control_name,
    simulated_control,
    poll_interval=0.1
):
    """
    The Python app opens an NI FPGA session then continuously generates a square wave on an FPGA control.
    The FPGA Bitfile reads the square wave and counts the rising edges.
    The Python Application reads the count back from the FPGA Bitfile.
    Automatically stops after 100 cycles.

    Args:
        bitfile_path (str):   Full path to the .lvbitx bitfile
        resource (str):       RIO resource string e.g. "RIO0" or "rio://192.168.1.100/RIO0"
        indicator_name (str): Name of the U64 indicator on the FPGA VI front panel
        control_name (str):   Name of the boolean control on the FPGA VI front panel
        control_value (bool): Value to write to the boolean control (default True)
        simulated_control (str): Enables a simulated control value
        poll_interval (float): Seconds between each read (default 0.1 = 10 Hz)
    """

    # Flag used by the signal handler to stop the read loop cleanly
    running = True

    #
    print(f"Connecting to FPGA: {resource}")
    print(f"Bitfile: {bitfile_path}")

    # Create an FPGA Session with user specified bitfile and resource
    with nifpga.Session(bitfile=bitfile_path, resource=resource) as session:

        # --- Reset and run the FPGA ---
        session.reset()
        session.run()
        print("FPGA is running.\n")

        # --- Write TRUE to the FPGA Control "Simulated IO" ---
        session.registers[simulated_control].write(True)

        # --- Create the header for the table reporting the current values ---
        print(f"{'Timestamp':<10}{'Loop#':<7}{indicator_name:<15}{control_name}")
        print("-" * 55)

        # --- Continuous write / read loop ---
        read_count = 0
        control_value = True
        while running:
            # --- Write Simulated Square Wave to FPGA ---
            session.registers[control_name].write(control_value)

            # --- Read processed result (count on rising edge) from FPGA ---
            u64_value = session.registers[indicator_name].read()

            # --- Capture loop count and timestamp ---
            read_count += 1
            timestamp = time.strftime("%H:%M:%S")

            # --- Report out ---
            print(f"{timestamp:<10}{read_count:<7}{indicator_name} = {u64_value:<6}{control_name} = {control_value}")

            # --- Stop if ran 100 times. Else, invert square wave and sleep. ---
            if read_count >= 100:
                running = False
            else:
                control_value = not control_value
                time.sleep(poll_interval)

        # --- Cleanup: reset control to False on exit ---
        print("\nCleaning up...")
        session.registers[control_name].write(False)
        session.registers[simulated_control].write(False)
        print(f"Reset '{control_name}' = False")
        print(f"Total reads: {read_count}")
        print("Session closed.")


# --- Example Usage ---
run_fpga(
    bitfile_path="/home/lvuser/natinst/PythonApp/FPGAMainBitfile.lvbitx",
    resource="RIO0",
    indicator_name="Count",
    control_name="Simulated Mod2 DIO0",
    simulated_control="Simulate IO",
    poll_interval=0.1)       # Read at 10 Hz — adjust as needed