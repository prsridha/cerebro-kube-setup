FROM nvidia/cuda:11.0.3-devel-ubuntu20.04

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git
RUN apt-get install -y lshw
RUN apt-get install -y zip
RUN apt-get install -y python3-pip
RUN pip install --upgrade click==8.0.4

COPY requirements.txt .

# install base requirements
RUN pip install -r requirements.txt

# install ML requirements with CUDA specific version
RUN pip3 install --pre torch torchvision torchaudio -f https://download.pytorch.org/whl/nightly/cu113/torch_nightly.html

# install aws-iam-authenticator
RUN curl -Lo /usr/local/bin/aws-iam-authenticator \
    https://github.com/kubernetes-sigs/aws-iam-authenticator/releases/download/v0.5.9/aws-iam-authenticator_0.5.9_linux_amd64 && \
    chmod +x /usr/local/bin/aws-iam-authenticator

# install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# install aws cli
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update && \
    rm awscliv2.zip

# set environment variables
ENV PATH="/root/.local/bin:${PATH}"