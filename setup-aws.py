import json
import time
import subprocess
import oyaml as yaml
from pathlib import Path
from argparse import ArgumentParser

def run(cmd, shell=True, capture_output=True, text=True):
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=capture_output, text=text)
        print(cmd)
        return out.stdout.strip("\n")
    except Exception as e:
        print("Command Unsuccessful:", cmd)
        print(str(e))

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
        # get cluster name
        cmd1 = "eksctl get cluster -o json"
        out = run(cmd1)
        cluster_name = json.loads(out)[0]["Name"]

        # get vpc id
        cmd2 = 'aws eks describe-cluster --name {} --query "cluster.resourcesVpcConfig.vpcId" --output text'.format(cluster_name)
        vpc_id = run(cmd2)

        # get CIDR range
        cmd3 = 'aws ec2 describe-vpcs --vpc-ids {} --query "Vpcs[].CidrBlock" --output text'.format(vpc_id)
        cidr_range = run(cmd3)
        print(cidr_range)
        
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
