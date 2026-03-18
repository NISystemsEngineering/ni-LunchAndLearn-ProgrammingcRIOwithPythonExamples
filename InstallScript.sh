#!/bin/bash

echo "Installing Python..." 
opkg install python3-pip
echo "Moving to User Directory..."
cd '/home/lvuser/natinst/'
echo "Creating New Application Directory..."
mkdir 'PythonApp'
echo "Moving to New Application Directory..."
cd 'PythonApp'
echo "Create Virtual Environment..."
python -m venv venv
echo "Activate Virtual Environment..."
source venv/bin/activate
echo "Upgrading PiP..."
python -m pip install --upgrade pip
echo "Install NI DAQmx..."
pip install nidaqmx
echo "Install NI FPGA..."
pip install nifpga