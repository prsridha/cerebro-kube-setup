import json
from logging import exception
import time
import subprocess
import oyaml as yaml
from pathlib import Path
from argparse import ArgumentParser

def run(cmd, shell=True, capture_output=True, text=True):
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=capture_output, text=text)
        # print(cmd)
        return out.stdout.strip("\n")
    except Exception as e:
        print("Command Unsuccessful:", cmd)
        print(str(e))
        raise Exception

class CerebroInstaller:
    def __init__(self):
        self.values_yaml = None
        
        with open('values.yaml', 'r') as yamlfile:
            self.values_yaml = yaml.safe_load(yamlfile)
            
        self.num_workers = self.values_yaml["cluster"]["workers"]
        
    def create_eks_cluster(self):
        from datetime import timedelta
         
        with open("init_cluster/eks_cluster.yaml", 'r') as yamlfile:
            eks_cluster_yaml = yaml.safe_load(yamlfile)
        
        eks_cluster_yaml["metadata"]["name"] = self.values_yaml["cluster"]["name"]
        eks_cluster_yaml["metadata"]["region"] = self.values_yaml["cluster"]["region"]
        eks_cluster_yaml["managedNodeGroups"][0]["instanceType"] = self.values_yaml["cluster"]["controllerInstance"]
        eks_cluster_yaml["managedNodeGroups"][0]["volumeSize"] = self.values_yaml["cluster"]["volumeSize"]
        eks_cluster_yaml["managedNodeGroups"][1]["instanceType"] = self.values_yaml["cluster"]["workerInstance"]
        eks_cluster_yaml["managedNodeGroups"][1]["volumeSize"] = self.values_yaml["cluster"]["volumeSize"]
        eks_cluster_yaml["managedNodeGroups"][1]["desiredCapacity"] = self.num_workers

        with open("init_cluster/eks_cluster.yaml", "w") as yamlfile:
            yaml.safe_dump(eks_cluster_yaml, yamlfile)

        try:
            start = time.time()
            cmd = "eksctl create cluster -f ./init_cluster/eks_cluster.yaml"
            subprocess.run(cmd, shell=True, text=True)
            end = time.time()
            print("Created cluster successfully")
            print("Time taken to create cluster:", str(timedelta(seconds=end-start)))
        except Exception as e:
            print("Couldn't create the cluster")
            print(str(e))
    
    def add_efs_storage(self):
        region = self.values_yaml["cluster"]["region"]
        
        # get cluster name
        cmd1 = "eksctl get cluster -o json"
        out = run(cmd1)
        cluster_name = json.loads(out)[0]["Name"]
        print("Cluster name:", cluster_name)

        # get vpc id
        cmd2 = 'aws eks describe-cluster --name {} --query "cluster.resourcesVpcConfig.vpcId" --output text'.format(cluster_name)
        vpc_id = run(cmd2)
        print("VPC ID:", vpc_id)

        # get CIDR range
        cmd3 = 'aws ec2 describe-vpcs --vpc-ids {} --query "Vpcs[].CidrBlock" --output text'.format(vpc_id)
        cidr_range = run(cmd3)
        print("CIDR Range", cidr_range)
        
        # create security group for inbound NFS traffic
        cmd4 = 'aws ec2 create-security-group --group-name efs-nfs-sg --description "Allow NFS traffic for EFS" --vpc-id {}'.format(vpc_id)
        # out = run(cmd4)
        out = """ {
            "GroupId": "sg-06e1854f9adb3ec78"
        } """
        sg_id = json.loads(out)["GroupId"]
        print("Created security group")
        print(sg_id)
        
        # add rules to the security group
        cmd5 = 'aws ec2 authorize-security-group-ingress --group-id {} --protocol tcp --port 2049 --cidr {}'.format(sg_id, cidr_range)
        # out = run(cmd5)
        print("Added ingress rules to security group")
        
        # create the AWS EFS File System (unencrypted)
        cmd6 = "aws efs create-file-system --region {}".format(region)
        # out = run(cmd6)
        print("Created EFS file system")
        # get file system id
        cmd7 = "aws efs describe-file-systems"
        out = run(cmd7)
        file_sys_id = json.loads(out)["FileSystems"][0]["FileSystemId"]
        print("Filesystem ID:", file_sys_id)
        
        # get subnets for vpc
        cmd8 = "aws ec2 describe-subnets --filter Name=vpc-id,Values={} --query 'Subnets[?MapPublicIpOnLaunch==`false`].SubnetId'".format(vpc_id)
        out = run(cmd8)
        subnets_list = json.loads(out)
        print("Subnets list:", str(subnets_list))
        
        # create mount targets
        cmd9 = """
        for subnet in {}; do
        aws efs create-mount-target \
            --file-system-id {} \
            --security-group  {} \
            --subnet-id $subnet \
            --region {}
        done""".format(" ".join(subnets_list), file_sys_id, sg_id, region)
        # out = run(cmd9)
        print("Created mount target for subnets")

        # install the EFS CSI driver
        cmd10 = 'kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/dev/?ref=master"'
        # out = run(cmd10)
        cmd11 = "kubectl get csidrivers.storage.k8s.io"
        out = run(cmd11)
        print(out)
        print("Installed EFS CSI driver on the cluster")
        
        # create storageclass
        cmd12 = "kubectl apply -f ./init_cluster/storage_class.yaml"
        # out = run(cmd12)
        cmd13 = "kubectl get sc"
        out = run(cmd13)
        print(out)
        print("Created storage class")
        
        # update file system id to values.yaml
        self.values_yaml["efs"]["fsID"] = file_sys_id
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)
        print("Saved FileSystem ID in values.yaml")
        
    def install_cerebro(self):
        cmd1 = "kubectl get nodes"
        out = run(cmd1)
        print(out)

def testing():
    cmd = "bash ./test.sh"
    subprocess.run(cmd, shell=True, text=True)

def main():
    installer = CerebroInstaller()
    
    # installer.create_eks_cluster()
    installer.add_efs_storage()
    # installer.install_cerebro()
    
    # parser = ArgumentParser()
    # parser.add_argument("cmd", help="install dependencies")
    
main()
