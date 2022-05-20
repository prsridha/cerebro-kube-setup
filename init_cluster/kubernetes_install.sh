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

## create kubeadm config file
touch kubeadm-config.yaml
echo "apiVersion: kubeadm.k8s.io/v1beta3
kind: ClusterConfiguration
kubernetesVersion: stable
controlPlaneEndpoint: "k8smaster:6443"
networking:
  podSubnet: 192.168.0.0/16
" > kubeadm-config.yaml

## add node0 IP address to hosts
ip4="10.10.1.1"
sudo echo -e "$ip4\tk8smaster" >> /etc/hosts

## init cluster
kubeadm init --config=kubeadm-config.yaml --upload-certs | tee kubeadm-init.out

## untaint control-plane
sleep(5)
kubectl taint nodes $(hostname) node-role.kubernetes.io/control-plane:NoSchedule-
kubectl taint nodes $(hostname) node-role.kubernetes.io/master:NoSchedule-

## copy kube config to /user and root and set permissions
# $1 is the username
mkdir -p /users/$1/.kube
cp -rf /etc/kubernetes/admin.conf /users/$1/.kube/config
uid=$(id -u $1)
gid=$(id -g $1)
chown $uid:$gid /users/$1/.kube/config
chown $uid:$gid /users/$1/.kube/

#TODO: note that kube config is being copied to root as well.
mkdir -p /root/.kube
cp -rf /etc/kubernetes/admin.conf /root/.kube/config

## setup calico
wget https://docs.projectcalico.org/manifests/calico.yaml
sed -i '/# - name: CALICO_IPV4POOL_CIDR/c\            - name: CALICO_IPV4POOL_CIDR' ./calico.yaml
sed -i '/#   value: "192.168.0.0\/16"/c\              value: "192.168.0.0\/16"' ./calico.yaml
kubectl apply -f calico.yaml

## install other dependencies
# nfs-server
apt install nfs-kernel-server

# helm
curl https://baltocdn.com/helm/signing.asc | sudo apt-key add -
echo "deb https://baltocdn.com/helm/stable/debian/ all main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
sudo apt-get update
sudo apt-get install helm

# misc
sudo apt-get -y install jq
sudo apt -y install python3-pip
pip install kubernetes
sudo apt-get install apt-transport-https --yes
sudo apt-get install dtach