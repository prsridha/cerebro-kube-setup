import os
import json
import time
import fire
import subprocess
import oyaml as yaml
from pathlib import Path
from kubernetes import client, config

## Prerequisites
# Create an IAM user with Admin permissions + Access Key - Programmatic access. Save the .csv cred file
# Install aws cli on local and configure it using the downloaded cred file
# Install eksctl CLI
# Create a key-pair on EC2 with name cerebro-kube-kp



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
        self.home = "/Users/pradsrid/Mine/Masters/Research/Cerebro/cerebro-kube-setup"
        self.values_yaml = None
        self.kube_namespace = "cerebro"
        
        with open('values.yaml', 'r') as yamlfile:
            self.values_yaml = yaml.safe_load(yamlfile)
            
        self.num_workers = self.values_yaml["cluster"]["workers"]
        
    def createCluster(self):
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
    
    def addStorage(self):
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
        out = run(cmd4)
        sg_id = json.loads(out)["GroupId"]
        print("Created security group")
        print(sg_id)
        
        # add rules to the security group
        cmd5 = 'aws ec2 authorize-security-group-ingress --group-id {} --protocol tcp --port 2049 --cidr {}'.format(sg_id, cidr_range)
        out = run(cmd5)
        print("Added ingress rules to security group")
        
        # create the AWS EFS File System (unencrypted)
        cmd6 = "aws efs create-file-system --region {}".format(region)
        out = run(cmd6)
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
        out = run(cmd9)
        print("Created mount target for subnets")

        # install the EFS CSI driver
        cmd10 = 'kubectl apply -k "github.com/kubernetes-sigs/aws-efs-csi-driver/deploy/kubernetes/overlays/dev/?ref=master"'
        out = run(cmd10)
        cmd11 = "kubectl get csidrivers.storage.k8s.io"
        out = run(cmd11)
        print(out)
        print("Installed EFS CSI driver on the cluster")
        
        # create storageclass
        cmd12 = "kubectl apply -f ./init_cluster/storage_class.yaml"
        out = run(cmd12)
        cmd13 = "kubectl get sc"
        out = run(cmd13)
        print(out)
        print("Created storage class")
        
        # update file system id to values.yaml
        self.values_yaml["cluster"]["efsFileSystemID"] = file_sys_id
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)
        print("Saved FileSystem ID in values.yaml")
   
   
    def installMetricsMonitor(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()

        node = "node0"

        cmds = [
            "kubectl create namespace prom-metrics",
            "helm repo add prometheus-community https://prometheus-community.github.io/helm-charts",
            "helm repo update",
            "helm install --namespace=prom-metrics prom prometheus-community/kube-prometheus-stack --version 30.1.0 --set nodeSelector.'cerebro/nodename'={}".format(
                node)
        ]

        for cmd in cmds:
            run(cmd)

        name = "prom-grafana"
        ns = "prom-metrics"
        body = v1.read_namespaced_service(namespace=ns, name=name)
        body.spec.type = "NodePort"
        v1.patch_namespaced_service(name, ns, body)

        svc = v1.read_namespaced_service(namespace=ns, name=name)
        port = svc.spec.ports[0].node_port

        print(
            "Access Grafana with this link:\nhttp://<AWS Host Name>: {}".format(port))
        print("username: {}\npassword: {}".format("admin", "prom-operator"))

        home = self.home
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/metrics_monitor_credentials.txt", "w+") as f:
            f.write(
                "Access Grafana with this link:\nhttp://<AWS Host Name>: {}\n".format(port))
            f.write("username: {}\npassword: {}".format(
                "admin", "prom-operator"))
    
    def installCerebro(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()
        
        # patch nodes with cerebro/nodename label
        body = v1.list_node(label_selector="role=controller")
        body.items[0].metadata.labels["cerebro/nodename"] = "node0"
        node_name = body.items[0].metadata.name
        patched_node = body.items[0]
        
        # v1.patch_node(node_name, patched_node)
        print("Patched Controller node:", node_name, "as node0")
        
        worker_i = 1
        bodies = v1.list_node(label_selector="role=worker")
        for body in bodies.items:
            body.metadata.labels["cerebro/nodename"] = "node" + str(worker_i)
            node_name = body.metadata.name
            patched_node = body
            # v1.patch_node(node_name, patched_node)
            print("Patched Worker node:", node_name, "as node" + str(worker_i))
            worker_i += 1
        
        cmds1 = [
            "kubectl create namespace {}".format(self.kube_namespace),
            "kubectl config set-context --current --namespace={}".format(
                self.kube_namespace),
            "kubectl create -n {} secret generic kube-config --from-file={}".format(
                self.kube_namespace, os.path.expanduser("~/.kube/config")),
        ]
        
        # for cmd in cmds1:
        #     out = run(cmd)
        #     print(out)
        
        # install Prometheus and Grafana
        self.installMetricsMonitor()
        
        #TODO: install kube-config
        
    def testing(self):
        pass
            
            
        
        
    def deleteCluster(self):
        try:
            cmd = "eksctl delete cluster -f ./init_cluster/eks_cluster.yaml"
            subprocess.run(cmd, shell=True, text=True)
        except Exception as e:
            print("Couldn't delete the cluster")
            print(str(e))

if __name__ == '__main__':
  fire.Fire(CerebroInstaller)