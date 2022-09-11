import os
import json
import time
import fire
import subprocess
import oyaml as yaml
from pathlib import Path
from kubernetes import client, config
from fabric2 import ThreadingGroup, Connection

## Prerequisites
# Create an IAM user with Admin permissions + Access Key - Programmatic access. Save the .csv cred file
# Create a key-pair on EC2 with name cerebro-kube-kp
# Create an ssh-key on local
# Install aws cli on local and configure it using the downloaded cred file
# Install eksctl CLI
# Install kubectl
# Install docker

def run(cmd, shell=True, capture_output=True, text=True):
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=capture_output, text=text)
        # print(cmd)
        return out.stdout
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
        
    def initializeFabric(self):
        # get controller and worker addresses
        host = None
        nodes = []
        cmd1 = "aws ec2 describe-instances"
        reservations = json.loads(run(cmd1))
        for reservation in reservations["Reservations"]:
            for i in reservation["Instances"]:
                tags = i["Tags"]
                if "controller" in str(tags):
                    host = i["PublicDnsName"]
                    break
                else:
                    nodes.append(i["PublicDnsName"])
        
        self.controller = host
        self.workers = nodes
        
        # load pem and initialize connections
        user = "ec2-user"
        pem_path = self.values_yaml["cluster"]["pemPath"]
        connect_kwargs = {"key_filename": pem_path}
        
        self.conn = Connection(host, user=user, connect_kwargs=connect_kwargs)
        self.s = ThreadingGroup(*nodes, user=user,
                                connect_kwargs=connect_kwargs)
    
    def oneTime(self):
        # https://docs.aws.amazon.com/IAM/latest/UserGuide/id_users_create.html#id_users_create_console
        # https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html
        # https://docs.aws.amazon.com/eks/latest/userguide/eksctl.html
        # https://docs.aws.amazon.com/eks/latest/userguide/install-kubectl.html
        
        # generate ssh key
        ssh_cmd = ' ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -q -N "" '
        run(ssh_cmd)
        print("Created ssh key")
        
        # add public key to git
        get_pub_key = 'cat ~/.ssh/id_rsa.pub'
        pub_key = run(get_pub_key)
        git_cmd = """ curl -H "Authorization: token {git_token}" --data '{{"title":"cerebroLocal","key":"{ssh_pub}"}}' https://api.github.com/user/keys """
        formatted_git_cmd = git_cmd.format(git_token=self.values_yaml["creds"]["gitToken"], ssh_pub=pub_key)
        run(formatted_git_cmd)
        print("Added ssh key to github")
        
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
            out = run(cmd)
            print(out)

        name = "prom-grafana"
        ns = "prom-metrics"
        body = v1.read_namespaced_service(namespace=ns, name=name)
        body.spec.type = "NodePort"
        v1.patch_namespaced_service(name, ns, body)

        svc = v1.read_namespaced_service(namespace=ns, name=name)
        port = svc.spec.ports[0].node_port
        
        # add ingress rule for port on security group
        cluster_name = self.values_yaml["cluster"]["name"]
        
        cmd1 = "aws ec2 describe-security-groups"
        out = json.loads(run(cmd1))
        for sg in out["SecurityGroups"]:
            if cluster_name in sg["GroupName"] and "controller" in sg["GroupName"]:
                controller_sg_id = sg["GroupId"]
        
        cmd2 = """aws ec2 authorize-security-group-ingress \
        --group-id {} \
        --protocol tcp \
        --port {} \
        --cidr 0.0.0.0/0
        """.format(controller_sg_id, port)
        
        print(cmd2)
        out = run(cmd2)
        print("Added Ingress rule in Controller SecurityGroup for Grafana port")
        
        cmd3 = "aws ec2 describe-instances"
        instances = json.loads(run(cmd3))
        for i in instances["Reservations"]:
            tags = i["Instances"][0]["Tags"]
            if "controller" in str(tags):
                public_dns_name = i["Instances"][0]["PublicDnsName"]
                break
        print(public_dns_name)
        
        print(
            "Access Grafana with this link:\nhttp://{}:{}".format(public_dns_name, port))
        print("username: {}\npassword: {}".format("admin", "prom-operator"))

        home = self.home
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/metrics_monitor_credentials.txt", "w+") as f:
            f.write(
                "Access Grafana with this link:\nhttp://<AWS Host Name>: {}\n".format(port))
            f.write("username: {}\npassword: {}".format(
                "admin", "prom-operator"))

    def initCerebro(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()
        
        # patch nodes with cerebro/nodename label
        body = v1.list_node(label_selector="role=controller")
        body.items[0].metadata.labels["cerebro/nodename"] = "node0"
        node_name = body.items[0].metadata.name
        patched_node = body.items[0]
        
        v1.patch_node(node_name, patched_node)
        print("Patched Controller node:", node_name, "as node0")
        
        worker_i = 1
        bodies = v1.list_node(label_selector="role=worker")
        for body in bodies.items:
            body.metadata.labels["cerebro/nodename"] = "node" + str(worker_i)
            node_name = body.metadata.name
            patched_node = body
            v1.patch_node(node_name, patched_node)
            print("Patched Worker node:", node_name, "as node" + str(worker_i))
            worker_i += 1
        
        # create namespace, set context and setup kube-config
        cmds1 = [
            "kubectl create namespace {}".format(self.kube_namespace),
            "kubectl config set-context --current --namespace={}".format(
                self.kube_namespace),
            "kubectl create -n {} secret generic kube-config --from-file={}".format(
                self.kube_namespace, os.path.expanduser("~/.kube/config")),
        ]
    
        for cmd in cmds1:
            out = run(cmd)
            print(out)
        print("Created Cerebro namespace, set context and added kube-config secret")
    
        # create kubernetes secret using ssh key and git server as known host
        known_hosts_cmd = "ssh-keyscan {} > ./reference/known_hosts".format(self.values_yaml["creds"]["gitServer"])
        github_known_hosts = run(known_hosts_cmd)
        kube_git_secret = "kubectl create secret generic git-creds --from-file=ssh=$HOME/.ssh/id_rsa --from-file=known_hosts=./reference/known_hosts"
        run(kube_git_secret.format(github_known_hosts))
        rm_known_hosts = "rm ./reference/known_hosts"
        run(rm_known_hosts)
        print("Created kubernetes secret for git")

        # login to docker using tokens
        docker_cmd = "docker login -u {} -p {}".format(self.values_yaml["creds"]["dockerUser"], self.values_yaml["creds"]["dockerToken"])
        docker_cmd2 = "chmod -R 777 $HOME/.docker"
        run(docker_cmd)
        run(docker_cmd2)

        # create docker secret
        docker_secret_cmd = "kubectl create secret generic regcred --from-file=.dockerconfigjson=$HOME/.docker/config.json --type=kubernetes.io/dockerconfigjson"
        run(docker_secret_cmd)

        home = "/home/ec2-user"
        self.conn.run("mkdir {}/cerebro-repo".format(home))
        self.conn.run("mkdir {}/user-repo".format(home))
    
    def createController(self):
        pass
    
    def createWorkers(self):
        pass

    def deleteCluster(self):
        fs_id = self.values_yaml["cluster"]["efsFileSystemID"]
        cluster_name = self.values_yaml["cluster"]["name"]
        
        # delete the cluster
        cmd1 = "eksctl delete cluster -f ./init_cluster/eks_cluster.yaml"
        out = run(cmd1, capture_output=False)
        print(out)
        print("Deleted the cluster")
        
        time.sleep(5)
        
        # Delete MountTargets
        cmd2 = """ aws efs describe-mount-targets \
        --file-system-id {} \
        --output json
        """.format(fs_id)
        out = json.loads(run(cmd2))
        mt_ids = [i["MountTargetId"] for i in out["MountTargets"]]
    
        cmd3 = """ aws efs delete-mount-target \
        --mount-target-id {}
        """
        
        for mt_id  in mt_ids:
            out = run(cmd3.format(mt_id))
            print("Deleted MountTarget:", mt_id)
            
        time.sleep(5)
            
        # delete SecurityGroup efs-nfs-sg
        sg_gid = None
        cmd4 = "aws ec2 describe-security-groups"
        out = json.loads(run(cmd4))
        
        for sg in out["SecurityGroups"]:
            if sg["GroupName"] == "efs-nfs-sg":
                sg_gid = sg["GroupId"]
                break
        
        cmd5 = "aws ec2 delete-security-group --group-id {}".format(sg_gid)
        out = run(cmd5)
        print("Deleted SecurityGroup efs-nfs-sg")
        
        time.sleep(5)
        
        # delete FileSystem
        cmd6 = """ aws efs delete-file-system \
        --file-system-id {}
        """.format(fs_id)
        
        out = run(cmd6)
        print("Deleted FileSystem:", fs_id)
        
        time.sleep(5)
        
        # delete Subnets
        cmd7 = " aws ec2 describe-subnets"
        cmd8 = "aws ec2 delete-subnet --subnet-id {}"
        out = json.loads(run(cmd7))
        for i in out["Subnets"]:
            if cluster_name in str(i["Tags"]):
                run(cmd8.format(i["SubnetId"]))
                print("Deleted Subnet:",i["SubnetId"])
                
        time.sleep(5)
                
        # delete VPC
        cmd9 = " aws ec2 describe-vpcs"
        cmd10 = "aws ec2 delete-vpc --vpc-id {}"
        out = json.loads(run(cmd9))
        for i in out["Vpcs"]:
            if "Tags" in i and cluster_name in str(i["Tags"]):
                run(cmd10.format(i["VpcId"]))
                print("Deleted VPC:",i["VpcId"])
                
        time.sleep(5)
                
        # delete Cloudformation Stack
        stack_name = "eksctl-" + cluster_name + "-cluster"
        cmd11 = "aws cloudformation delete-stack --stack-name {}".format(stack_name)
        out = run(cmd11)
        print("Deleted CloudFormation Stack")
    
    # call the below functions from CLI
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
            
        # add storage
        self.addStorage()

    def installCerebro(self):
        # load fabric connections
        self.initializeFabric()
        
        # install Prometheus and Grafana
        self.installMetricsMonitor()
        
        # initialize basic cerebro components
        self.initCerebro()
        
        # create controller
        self.createController()
        
        # create workers
        self.createWorkers()
    
    def testing(self):
        pass

if __name__ == '__main__':
    fire.Fire(CerebroInstaller)