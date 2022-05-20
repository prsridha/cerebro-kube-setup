#!/bin/bash

## update and upgrade
apt-get update && apt-get upgrade -y

## install docker
apt-get install -y docker.io

## install kubeadm, kubelet and kubectl
apt-get install -y apt-transport-https ca-certificates curl
curl -fsSLo /usr/share/keyrings/kubernetes-archive-keyring.gpg https://packages.cloud.google.com/apt/doc/apt-key.gpg
echo "deb [signed-by=/usr/share/keyrings/kubernetes-archive-keyring.gpg] https://apt.kubernetes.io/ kubernetes-xenial main" | sudo tee /etc/apt/sources.list.d/kubernetes.list
apt-get update
apt-get install -y kubelet kubeadm kubectl
apt-mark hold kubelet kubeadm kubectl

## turn off swap memory
swapoff -a
sed -i '/ swap / s/^/#/' /etc/fstab

## reload docker
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo systemctl restart kubelet

# Paste the IPV4 addr from master
echo -e "$1\tk8smaster" >> /etc/hosts

## install dependencies

#nfs-kernel
apt install nfs-kernel-server