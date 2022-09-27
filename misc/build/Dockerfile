FROM nvidia/cuda:11.6.0-base-ubuntu20.04

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git
RUN apt-get install -y python3-pip
RUN pip install --upgrade click==8.0.4

COPY requirements.txt .

# install base requirements
RUN pip install -r requirements.txt

# install ML requirements with CUDA specific version
RUN pip3 install torch torchvision torchaudio --extra-index-url https://download.pytorch.org/whl/cu113