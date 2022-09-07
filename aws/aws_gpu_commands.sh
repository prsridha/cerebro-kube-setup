# basic
sudo apt-get update
sudo apt install -y gcc
sudo apt-get install -y build-essential
sudo apt install -y python3-pip

# nvidia-smi and cuda for Tesla GPU
BASE_URL=https://us.download.nvidia.com/tesla
DRIVER_VERSION=470.141.03
curl -fSsl -O $BASE_URL/$DRIVER_VERSION/NVIDIA-Linux-x86_64-$DRIVER_VERSION.run
sudo sh NVIDIA-Linux-x86_64-$DRIVER_VERSION.run

# cerebro stuff
git clone https://github.com/prsridha/cerebro-kube-setup.git
pip install -r cerebro-kube-setup/misc/build/requirements.txt
pip install jupyterlab

# Jupyter Lab
python3 -m jupyterlab
ssh -i Mine/Masters/Research/prsridha-cerebro.pem ubuntu@ec2-52-39-134-107.us-west-2.compute.amazonaws.com -L 9999:localhost:8888


# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
# EBS storage on AWS - EKS                                        #
# https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html   #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# download IAM policy
curl -o iam-policy-example.json https://raw.githubusercontent.com/kubernetes-sigs/aws-efs-csi-driver/master/docs/iam-policy-example.json

# create the IAM policy to 
aws iam create-policy \
    --policy-name AmazonEKS_EFS_CSI_Driver_Policy \
    --policy-document file://iam-policy-example.json

# find your amazon ID
aws sts get-caller-identity
# accountID: 782408612084

# # # # # # # # # # # # # # # # # # # # # # # # # #
# Till here (above) is a one-time only operation  #
# # # # # # # # # # # # # # # # # # # # # # # # # #

# create an IAM OIDC provider for your cluster
eksctl utils associate-iam-oidc-provider --cluster cerebro-kube-1 --approve --region ap-south-1

# create IAM role and kubernetes service account
# replace cluster name, accountID and region-code
eksctl create iamserviceaccount \
    --cluster cerebro-kube-1 \
    --namespace kube-system \
    --name efs-csi-controller-sa \
    --attach-policy-arn arn:aws:iam::782408612084:policy/AmazonEKS_EFS_CSI_Driver_Policy \
    --approve \
    --region ap-south-1

# add EFS-CSI driver for Kubernetes using Helm
helm repo add aws-efs-csi-driver https://kubernetes-sigs.github.io/aws-efs-csi-driver/
helm repo update

#TODO: This might not work, because they've changed the repo after 1.14, and haven't updated the aws webpage yet
kubectl apply -k "https://github.com/kubernetes-sigs/aws-efs-csi-driver/tree/master/deploy/kubernetes/overlays/stable"

# create vpc
vpc_id=$(aws eks describe-cluster \
    --name cerebro-kube-1 \
    --region ap-south-1 \
    --query "cluster.resourcesVpcConfig.vpcId" \
    --output text)

# get cidr range
cidr_range=$(aws ec2 describe-vpcs \
    --vpc-ids $vpc_id \
    --region ap-south-1 \
    --query "Vpcs[].CidrBlock" \
    --output text)

# create security group
security_group_id=$(aws ec2 create-security-group \
    --group-name MyEfsSecurityGroup \
    --description "My EFS security group" \
    --vpc-id $vpc_id \
    --region ap-south-1 \
    --output text)

# create inbound rule for NFS traffic
aws ec2 authorize-security-group-ingress \
    --group-id $security_group_id \
    --protocol tcp \
    --port 2049 \
    --region ap-south-1 \
    --cidr $cidr_range

# create EFS file system
file_system_id=$(aws efs create-file-system \
    --region ap-south-1 \
    --performance-mode generalPurpose \
    --query 'FileSystemId' \
    --output text)

# get subnet IDs for each node
aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=$vpc_id" \
    --query 'Subnets[*].{SubnetId: SubnetId,AvailabilityZone: AvailabilityZone,CidrBlock: CidrBlock}' \
    --region ap-south-1 \
    --output table

# add mount targets
# need to use IP and subnet from previous two commands, and select those subnets which are within
# the CIDR range of the nodeIPs
aws efs create-mount-target \
    --file-system-id $file_system_id \
    --subnet-id subnet-0495729d8f33508a2 \
    --security-groups $security_group_id \
    --region ap-south-1

# run a sample application
# need to create pv
# example PV.yaml here:

# apiVersion: v1
# kind: PersistentVolume
# metadata:
#   name: efs-pv
# spec:
#   capacity:
#     storage: 5Gi
#   volumeMode: Filesystem
#   accessModes:
#     - ReadWriteMany
#   persistentVolumeReclaimPolicy: Retain
#   storageClassName: efs-sc
#   csi:
#     driver: efs.csi.aws.com
#     volumeHandle: fs-0db3841ca8e082247

# get file_system_id and replace volumeHandle with the file_system_id
echo $file_system_id

