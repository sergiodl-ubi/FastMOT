#!/bin/bash

set -e

L4T_VERSION=$(dpkg-query --showformat='${Version}' --show nvidia-l4t-core | cut -f1 -d'-')

# Jetpack>=4.4 (OpenCV, CUDA, TensorRT) is required
if dpkg --compare-versions $L4T_VERSION ge 32.6; then
    TF_VERSION=1.15.5
    NV_VERSION=21.7
    JP_VERSION=46
elif dpkg --compare-versions $L4T_VERSION ge 32.5; then
    TF_VERSION=1.15.4
    NV_VERSION=20.12
    JP_VERSION=45
elif dpkg --compare-versions $L4T_VERSION ge 32.4; then
    TF_VERSION=1.15.2
    NV_VERSION=20.4
    JP_VERSION=44
else
    echo "Error: unsupported L4T version $L4T_VERSION"
    exit 1
fi

# Set up CUDA environment
if [ ! -x "$(command -v nvcc)" ]; then
    echo "export PATH=/usr/local/cuda/bin\${PATH:+:\${PATH}}" >> ~/.bashrc
    echo "export LD_LIBRARY_PATH=/usr/local/cuda/lib64\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}" >> ~/.bashrc
    source ~/.bashrc
fi

# NumPy and TensorFlow
sudo apt-get update
sudo apt-get install -y python3-pip libhdf5-serial-dev hdf5-tools libcanberra-gtk-module
sudo -H pip3 install numpy
sudo ln -s /usr/include/locale.h /usr/include/xlocale.h
sudo -H pip3 install --no-cache-dir --extra-index-url https://developer.download.nvidia.com/compute/redist/jp/v$JP_VERSION tensorflow==$TF_VERSION+nv$NV_VERSION

# SciPy
sudo apt-get install -y libatlas-base-dev gfortran
sudo -H pip3 install scipy==1.5

# Numba
sudo apt-get install -y llvm-8 llvm-8-dev
sudo -H LLVM_CONFIG=/usr/bin/llvm-config-8 pip3 install numba==0.48

# CuPy
echo "Installing CuPy, this may take a while..."
sudo -H CUPY_NVCC_GENERATE_CODE="current" CUPY_NUM_BUILD_JOBS=$(nproc) pip3 install cupy==9.2
