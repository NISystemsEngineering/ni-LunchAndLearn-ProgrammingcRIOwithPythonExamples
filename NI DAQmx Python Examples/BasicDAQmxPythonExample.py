import nidaqmx
from nidaqmx.constants import AcquisitionType
from nidaqmx.stream_readers import AnalogSingleChannelReader
import numpy as np
import platform
from pathlib import Path
import locale

os_name = platform.system()
if os_name == "Windows":
    import matplotlib.pyplot as plt

#Highlight NI DAQmx works on Windows and on NI Linux RT
print(f"{Path(__file__).name} is executing on {os_name}")
if os_name == "Windows":
    #Set Module Names for the debug system
    mod_name = "cDAQ9189-2628AACMod1"
elif os_name == "Linux":
    #Set Module Names for the cRIO
    mod_name = "Mod4"

chan_name="ai0"

# Configure timing for finite samples
sample_rate = 1000  # Hz
num_samples = 100

#Check that DAQmx is installed
print(f"DAQmx Version: {nidaqmx.version}")

#Workaround for my system
locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')

with nidaqmx.Task() as task:
    # Add an analog input voltage channel
    task.ai_channels.add_ai_voltage_chan(f"{mod_name}/{chan_name}")

    # Configure simple timing task
    task.timing.cfg_samp_clk_timing(rate=sample_rate, samps_per_chan=num_samples, sample_mode=AcquisitionType.FINITE)

    # Allocate buffer
    data = np.zeros(num_samples, dtype=np.float64)
    reader = AnalogSingleChannelReader(task.in_stream)

    # Start and read data
    task.start()
    reader.read_many_sample(data, number_of_samples_per_channel=num_samples, timeout=10.0)

    # Print data
    if os_name == "Windows":
        print("Acquired data:", data)
        # --- Plot (optional) ---
        plt.figure()
        plt.plot(data, label=f"{mod_name}/{chan_name}")
        plt.legend()
        plt.show(block=False)
        key_pressed = False
        while key_pressed == False:
            key_pressed = plt.waitforbuttonpress()
        plt.close()
    else:
        print("Acquired data:", data)