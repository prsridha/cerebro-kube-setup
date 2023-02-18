import os
import json
import time
import fire
import psutil
import webbrowser
import subprocess
from git import Repo
import oyaml as yaml
from pathlib import Path
from pprint import pprint
from kubernetes import client, config
from fabric2 import ThreadingGroup, Connection

# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # 
## Prerequisites
# Create an IAM user with Admin permissions + Access Key - Programmatic access. Save the .csv cred file
# Install aws cli on local and configure it using the downloaded cred file
# Create a key-pair on EC2 with name cerebro-kube-kp
# Create an ssh-key on local for git
# Install eksctl CLI
# Install kubectl
# Install docker
# Install helm
# Run oneTime() given below
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

def run(cmd, shell=True, capture_output=True, text=True, haltException=True):
    try:
        out = subprocess.run(cmd, shell=shell, capture_output=capture_output, text=text)
        # print(cmd)
        if out.stderr:
            if haltException:
                raise Exception("Command Error:" + str(out.stderr))
            else:
                print("Command Error:" + str(out.stderr))
        if capture_output:
            return out.stdout.rstrip("\n")
        else:
            return None
    except Exception as e:
        print("Command Unsuccessful:", cmd)
        print(str(e))
        raise Exception

def checkPodStatus(label, namespace):
    from kubernetes import client, config

    config.load_kube_config()
    v1 = client.CoreV1Api()
    names = []
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    for pod in pods_list.items:
        names.append(pod.metadata.name)

    if not names:
        return False

    for i in names:
        pod = v1.read_namespaced_pod_status(i, namespace, pretty='true')
        status = pod.status.phase
        if status != "Running":
            return False
    return True

def getPodNames(namespace):
    label = "app=cerebro-controller"
    config.load_kube_config()
    v1 = client.CoreV1Api()
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    if len(pods_list.items) > 0:
        controller = pods_list.items[0].metadata.name
    else:
        controller = ""

    label = "type=cerebro-worker-etl"
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    etl_workers = [i.metadata.name for i in pods_list.items]

    label = "type=cerebro-worker-mop"
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    mop_workers = [i.metadata.name for i in pods_list.items]

    return {
        "controller": controller,
        "etl_workers": etl_workers,
        "mop_workers": mop_workers
    }

class CerebroInstaller:
    def __init__(self):
        self.home = "."
        self.values_yaml = None
        self.kube_namespace = "cerebro"
        
        self.copyUserYAML()
        with open('values.yaml', 'r') as yamlfile:
            self.values_yaml = yaml.safe_load(yamlfile)
            
        self.num_workers = self.values_yaml["cluster"]["numWorkers"]
        
    def copyUserYAML(self):
        with open('user-values.yaml', 'r') as yamlfile:
            user_yaml = yaml.safe_load(yamlfile)
        
        with open('values.yaml', 'r') as yamlfile:
            values_yaml = yaml.safe_load(yamlfile)
            
        values_yaml["cluster"]["name"] = user_yaml["clusterName"]
        values_yaml["cluster"]["region"] = user_yaml["clusterRegion"]
        values_yaml["cluster"]["workerSpotInstance"] = user_yaml["workerSpotInstance"]
        values_yaml["cluster"]["numWorkers"] = user_yaml["numWorkers"]
        values_yaml["cluster"]["controllerInstance"] = user_yaml["controllerInstance"]
        values_yaml["cluster"]["workerInstances"] = user_yaml["workerInstances"]
        values_yaml["workerETL"]["percentETLCores"] = user_yaml["percentETLCores"]
            
        with open("values.yaml", "w") as f:
            yaml.safe_dump(values_yaml, f)
        
    def initializeFabric(self):
        # get controller and worker addresses
        host = None
        nodes = []
        cmd1 = "aws ec2 describe-instances --filters 'Name=instance-state-code,Values=16'"
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
        
        # create IAM EFS Policy
        cmd1 = """
        aws iam create-policy \
            --policy-name AmazonEKS_EFS_CSI_Driver_Policy \
            --policy-document file://init_cluster/iam-policy-eks-efs.json
        """
        run(cmd1)
        print("Created IAM policy for EFS")
        
        # create iam policy for read-only s3
        cmd11 = """aws iam create-policy \
            --policy-name AmazonEKS_S3_Policy \
            --policy-document file://init_cluster/iam-policy-eks-s3.json
        """
        run(cmd11)
        print("Created IAM read-only policy for S3")
        
    def addStorage(self):
        region = self.values_yaml["cluster"]["region"]
        cluster_name = self.values_yaml["cluster"]["name"]
        image_registry = self.values_yaml["cluster"]["containerImageRegistery"]
        
        # add OIDC to IAM role
        cmd4 = "eksctl utils associate-iam-oidc-provider --cluster {} --approve".format(cluster_name)
        run(cmd4, capture_output=False)
        time.sleep(3)
        
        # create service account
        cmd5 = "aws sts get-caller-identity"
        account_id = json.loads(run(cmd5))["Account"]
        
        cmd6 = """
        eksctl create iamserviceaccount \
            --cluster {} \
            --namespace kube-system \
            --name efs-csi-controller-sa \
            --attach-policy-arn arn:aws:iam::{}:policy/AmazonEKS_EFS_CSI_Driver_Policy \
            --approve \
            --region {}
        """.format(cluster_name, account_id, region)
        run(cmd6, capture_output=False)
        print("Created IAM ServiceAccount")
        time.sleep(3)
        
        # install efs csi driver
        cmd13 = "helm repo add aws-efs-csi-driver https://kubernetes-sigs.github.io/aws-efs-csi-driver/"
        cmd14 = "helm repo update"
        run(cmd13)
        run(cmd14)
        cmd7 = """
        helm upgrade -i aws-efs-csi-driver aws-efs-csi-driver/aws-efs-csi-driver \
            --namespace kube-system \
            --set image.repository={}/eks/aws-efs-csi-driver \
            --set controller.serviceAccount.create=false \
            --set controller.serviceAccount.name=efs-csi-controller-sa
        """.format(image_registry)
        run(cmd7, capture_output=False)
        print("Installed EFS CSI driver")
        # wait till driver pods are up and running:
        print("Waiting for csi driver pods to start-up")
        label = "app.kubernetes.io/name=aws-efs-csi-driver,app.kubernetes.io/instance=aws-efs-csi-driver"
        ns = "kube-system"
        while not checkPodStatus(label, ns):
            time.sleep(1)
        print("CSI pods ready")
        
        # get vpc id
        cmd8 = 'aws eks describe-cluster --name {} --query "cluster.resourcesVpcConfig.vpcId" --output text'.format(cluster_name)
        vpc_id = run(cmd8)
        print("VPC ID:", vpc_id)
        
        # get CIDR range
        cmd3 = 'aws ec2 describe-vpcs --vpc-ids {} --query "Vpcs[].CidrBlock" --output text'.format(vpc_id)
        cidr_range = run(cmd3)
        print("CIDR Range:", cidr_range)
        
        # create security group for inbound NFS traffic
        cmd4 = 'aws ec2 create-security-group --group-name efs-nfs-sg --description "Allow NFS traffic for EFS" --vpc-id {}'.format(vpc_id)
        out = run(cmd4)
        sg_id = json.loads(out)["GroupId"]
        print("Created security group")
        print(sg_id)
        
        # add rules to the security group
        cmd5 = 'aws ec2 authorize-security-group-ingress --group-id {} --protocol tcp --port 2049 --cidr {}'.format(sg_id, cidr_range)
        run(cmd5)
        print("Added ingress rules to security group")
        
        # create the AWS EFS File Systems (one for each PV) (unencrypted)
        cmd6 = """
            aws efs create-file-system \
            --region {} \
            --performance-mode generalPurpose \
            --query 'FileSystemId'
            """.format(region)
        for i in range(4):
            run(cmd6)
        print("Created 4 EFS file systems")
        time.sleep(5)
        
        # get file system ids
        file_systems = []
        cmd7 = "aws efs describe-file-systems"
        out = run(cmd7)
        for i in range(4):
            file_sys_id = json.loads(out)["FileSystems"][i]["FileSystemId"]
            file_systems.append(file_sys_id)
        print("Filesystem IDs:", file_systems)
        
        # get subnets for vpc
        cmd8 = "aws ec2 describe-subnets --filter Name=vpc-id,Values={} --query 'Subnets[?MapPublicIpOnLaunch==`false`].SubnetId'".format(vpc_id)
        out = run(cmd8)
        subnets_list = json.loads(out)
        print("Subnets list:", str(subnets_list))
        time.sleep(5)
        
        # create mount targets
        cmd9 = """
        for subnet in {}; do
        aws efs create-mount-target \
            --file-system-id {} \
            --security-group  {} \
            --subnet-id $subnet \
            --region {}
        done"""
        for i in range(4):
            file_sys_id = file_systems[i]
            out = run(cmd9.format(" ".join(subnets_list), file_sys_id, sg_id, region))
            print("Created mount targets for all subnets for file system {}".format(file_sys_id))
        print("Created mount targets for all subnets for all file systems")
        
        # create storage class
        cmd10 = "kubectl apply -f init_cluster/storage_class.yaml"
        run(cmd10)
        print("Created Storage Class")
        
        # create service account for s3
        cmd12 = """ eksctl create iamserviceaccount \
        --name s3-eks-sa \
        --namespace cerebro \
        --cluster {} \
        --attach-policy-arn arn:aws:iam::{}:policy/AmazonEKS_S3_Policy \
        --approve
        """.format(cluster_name, account_id)
        
        run(cmd12, capture_output=False)
        print("Created serviceaccount for S3")
        
        # update file system id to values.yaml
        self.values_yaml["cluster"]["efsFileSystemIds"] = {
            "checkpointFsId": file_systems[0],
            "dataFsId": file_systems[1],
            "metricsFsId": file_systems[2],
            "configFsId": file_systems[3]
        }
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)
        print("Saved FileSystem ID in values.yaml")
   
    def addLocalDNSCache(self):
        self.initializeFabric()
        self.s.run("mkdir -p /home/ec2-user/init_cluster")
        self.s.put("init_cluster/local_dns.sh", "/home/ec2-user/init_cluster")
        # run local_dns script
        self.s.sudo("/bin/bash /home/ec2-user/init_cluster/local_dns.sh")
        
        print("Created Local DNS Cache on worker nodes")
        
    def installMetricsMonitor(self):
        # load fabric connections
        self.initializeFabric()
        
        config.load_kube_config()
        v1 = client.CoreV1Api()
        
        dir = "init_cluster/metrics_monitor"
        Path(dir).mkdir(parents=True, exist_ok=True)
        
        node = "node0"
        prometheus_port = self.values_yaml["cluster"]["prometheusNodePort"]
        
        # pull Prometheus values file
        prom_path = "init_cluster/kube-prometheus-stack.values"
        
        cmds = [
            "kubectl create namespace prom-metrics",
            "helm repo add prometheus-community https://prometheus-community.github.io/helm-charts",
            "helm repo update"
        ]

        for cmd in cmds:
            out = run(cmd)
            print(out)
        
        cmd1 = """
        helm install prom prometheus-community/kube-prometheus-stack \
        --namespace prom-metrics """
        # --values {}
        # .format(prom_path)
        
        cmd2 = "helm repo add grafana https://grafana.github.io/helm-charts"
        cmd3 = "helm upgrade --install loki grafana/loki-stack -n prom-metrics"
        
        print("Installing Prometheus and Grafana...")
        run(cmd1, capture_output=False)
        run(cmd2, capture_output=False)
        run(cmd3, capture_output=False)
        
        # install DCGM exporter
        dcgm_path = "init_cluster/metrics_monitor/dcgm"
        Path(dcgm_path).mkdir(parents=True, exist_ok=True)
        Repo.clone_from("https://github.com/NVIDIA/gpu-monitoring-tools.git", dcgm_path)
        
        # modification to the repo
        path1 = os.path.join(dcgm_path, "etc/dcgm-exporter/default-counters.csv")
        path2 = os.path.join(dcgm_path, "etc/dcgm-exporter/dcp-metrics-included.csv")
        path3 = os.path.join(dcgm_path, "deployment/dcgm-exporter/templates/daemonset.yaml")
        
        with open(path1, "r") as f1, open(path2, "r") as f2, open(path3, "r") as f3:
            path1_s = f1.read()
            path2_s = f2.read()
            path3_s = f3.read()
            
            path1_s = path1_s.replace("# DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_UTIL")
            path2_s = path2_s.replace("# DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_GPU_UTIL")
            path3_s = path3_s.replace("initialDelaySeconds: 5", "initialDelaySeconds: 30")
        
        with open(path1, "w") as f1, open(path2, "w") as f2, open(path3, "w") as f3:
            f1.write(path1_s)
            f2.write(path2_s)
            f3.write(path3_s)
        
        print("Installing DCGM Exporter...")
        cmd2 = "(cd {}/deployment ; helm install --generate-name --namespace prom-metrics dcgm-exporter)".format(dcgm_path)
        run(cmd2, capture_output=False)

        name = "prom-grafana"
        ns = "prom-metrics"
        body = v1.read_namespaced_service(namespace=ns, name=name)
        body.spec.type = "NodePort"
        v1.patch_namespaced_service(name, ns, body)

        svc = v1.read_namespaced_service(namespace=ns, name=name)
        port = svc.spec.ports[0].node_port
        
        # add ingress rule for ports on security group
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
        """
        
        out = run(cmd2.format(controller_sg_id, port))
        out = run(cmd2.format(controller_sg_id, prometheus_port), haltException=False)

        print("Added Ingress rules in Controller SecurityGroup for Grafana and Prometheus ports")
        
        cmd3 = "aws ec2 describe-instances --filters 'Name=instance-state-code,Values=16'"
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
        print("Add Loki Datasource with this url: \nhttp://loki:3100")
        
        print(
            "Access Prometheus with this link:\nhttp://{}:{}".format(public_dns_name, prometheus_port))

        home = self.home
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/metrics_monitor_credentials.txt", "w+") as f:
            f.write(
                "Access Grafana with this link:\nhttp://{}:{}\n".format(public_dns_name, port))
            f.write("username: {}\npassword: {}\n".format(
                "admin", "prom-operator"))
            f.write(
                "Add Loki Datasource with this url:\nhttp://loki:3100\n")
            f.write(
                "\nAccess Prometheus with this link:\nhttp://{}:{}\n".format(public_dns_name, prometheus_port))

        # delete temporarily created files
        cmd = "rm -rf {}".format(dir)
        run(cmd)
        
        # open Grafana URL
        url = "http://{}:{}".format(public_dns_name, port)
        webbrowser.open(url)

    def patchNodes(self):
        # load fabric connections
        self.initializeFabric()
        
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
            
        # create Cerebro directories on Controller and Worker nodes
        home = "/home/ec2-user"
        self.conn.run("mkdir -p {}/cerebro-repo".format(home))
        self.conn.run("mkdir -p {}/user-repo".format(home))
        self.s.run("mkdir -p {}/user-repo".format(home))
        self.s.run("mkdir -p {}/cerebro-repo".format(home))
        
        print("Created directories for cerebro repos")
    
    def initCerebro(self):
        # load fabric connections
        self.initializeFabric()
        
        config.load_kube_config()
        v1 = client.CoreV1Api()
        
        # patch nodes with cerebro/nodename label
        self.patchNodes()
        
        # create namespace, set context and setup kube-config
        cmds1 = [
            # "kubectl create namespace {}".format(self.kube_namespace),
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
        github_known_hosts = run(known_hosts_cmd, capture_output=False)
        kube_git_secret = "kubectl create secret generic git-creds --from-file=ssh=$HOME/.ssh/id_rsa --from-file=known_hosts=./reference/known_hosts"
        run(kube_git_secret.format(github_known_hosts))
        rm_known_hosts = "rm ./reference/known_hosts"
        run(rm_known_hosts)
        print("Created kubernetes secret for git")

        # add node local DNS cache
        cmd2 = "kubectl get svc kube-dns -n kube-system -o jsonpath={.spec.clusterIP}"
        kubedns = run(cmd2)
        domain = "cluster.local"
        localdns = self.values_yaml["cluster"]["nodeLocalListenIP"]
        
        with open("init_cluster/nodelocaldns_template.yaml", "r") as f:
            yml = f.read()
            yml = yml.replace("__PILLAR__LOCAL__DNS__", localdns)
            yml = yml.replace("__PILLAR__DNS__DOMAIN__", domain)
            yml = yml.replace("__PILLAR__DNS__SERVER__", kubedns)
            
        with open("init_cluster/nodelocaldns.yaml", "w") as f:
            f.write(yml)
        
        cmd3 = "kubectl apply -f init_cluster/nodelocaldns.yaml"
        run(cmd3, capture_output=False)
        
        # add hardware info configmap
        # get number of cores
        cores = []
        out = self.s.run("grep -c ^processor /proc/cpuinfo", hide=True)
        for _, ans in out.items():
            cores.append(int(ans.stdout.strip()))
        
        # get number of GPUs
        gpus = []
        out = self.s.run("nvidia-smi --query-gpu=name --format=csv,noheader | wc -l", hide=True)
        for _, ans in out.items():
            gpus.append(int(ans.stdout.strip()))
        
        node_hardware_info = {}
        
        for i in range(1, self.num_workers+1):
            node_hardware_info["node" + str(i)] = {
                "num_cores": cores[i-1],
                "num_gpus": gpus[i-1]
            }
        
        path = "init_cluster/node_hardware_info.json"
        with open(path, "w") as f:
            json.dump(node_hardware_info, f)
            
        # create configmap
        cmd = "kubectl create -n {} configmap node-hardware-info --from-file=init_cluster/node_hardware_info.json".format(self.kube_namespace)
        run(cmd)
        
        run("rm {}".format(path))
        
        print("Created configmap for node hardware info")
        
        # add ingress rule for JupyterNotebook, Tensorboard and WebServer ports on security group
        jupyterNodePort = self.values_yaml["cluster"]["jupyterNodePort"]
        tensorboardNodePort = self.values_yaml["cluster"]["tensorboardNodePort"]
        webappNodePort = self.values_yaml["cluster"]["webappNodePort"]

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
        """
        
        out = run(cmd2.format(controller_sg_id, jupyterNodePort, haltException=False))
        out = run(cmd2.format(controller_sg_id, tensorboardNodePort, haltException=False))
        out = run(cmd2.format(controller_sg_id, webappNodePort, haltException=False))
    
    def deleteCluster(self):
        cluster_name = self.values_yaml["cluster"]["name"]

        cmd12 = "aws efs describe-file-systems --output json"
        out = json.loads(run(cmd12))
        fs_ids = []
        for i in out["FileSystems"]:
            fs_ids.append(i["FileSystemId"])
        print("Found file systems:", str(fs_ids))
        
        def _runCommands(fn, name):
            try:
                fn()
            except Exception as e:
                print("Command Failed for", name)
                print(str(e))
        
        # delete the cluster
        def _deleteCluster(): 
            cmd1 = "eksctl delete cluster -f ./init_cluster/eks_cluster.yaml"
            run(cmd1, capture_output=False)
            print("Deleted the cluster")
        _runCommands(_deleteCluster, "clusterDelete")
        time.sleep(3)
                
        # Delete MountTargets
        def _deleteMountTargets():
            mt_ids = []
            for fs_id in fs_ids:          
                cmd2 = """ aws efs describe-mount-targets \
                --file-system-id {} \
                --output json
                """.format(fs_id)
                out = json.loads(run(cmd2))
                mt_ids.extend([i["MountTargetId"] for i in out["MountTargets"]])
    
            cmd3 = """ aws efs delete-mount-target \
            --mount-target-id {}
            """
            for mt_id  in mt_ids:
                run(cmd3.format(mt_id))
                print("Deleted MountTarget:", mt_id)
            print("Deleted all MountTargets")
        _runCommands(_deleteMountTargets, "deleteMountTarget")
        time.sleep(5)
        
        # delete FileSystem
        def _deleteFileSystem():            
            for fs_id in fs_ids:
                cmd6 = """ aws efs delete-file-system \
                --file-system-id {}
                """.format(fs_id)
                run(cmd6)
                print("Deleted filesystem:", fs_id)
            print("Deleted all FileSystems")
        _runCommands(_deleteFileSystem, "deleteFileSystem")
        time.sleep(3)
        
        # delete SecurityGroup efs-nfs-sg
        def _deleteSecurityGroups():
            sg_gid = None
            cmd4 = "aws ec2 describe-security-groups"
            out = json.loads(run(cmd4))
            
            for sg in out["SecurityGroups"]:
                if sg["GroupName"] == "efs-nfs-sg":
                    sg_gid = sg["GroupId"]
                    break
            
            cmd5 = "aws ec2 delete-security-group --group-id {}".format(sg_gid)
            run(cmd5)
            print("Deleted SecurityGroup efs-nfs-sg")
        _runCommands(_deleteSecurityGroups, "deleteSecurityGroups")
        time.sleep(3)
        
        # delete Subnets
        def _deleteSubnets():
            cmd7 = " aws ec2 describe-subnets"
            cmd8 = "aws ec2 delete-subnet --subnet-id {}"
            out = json.loads(run(cmd7))
            for i in out["Subnets"]:
                if cluster_name in str(i["Tags"]):
                    run(cmd8.format(i["SubnetId"]))
                    print("Deleted Subnet:",i["SubnetId"])
            print("Deleted all Subnets")
        _runCommands(_deleteSubnets, "deleteSubnets")
        time.sleep(3)
                
        # delete VPC
        def _deleteVPC():
            cmd9 = " aws ec2 describe-vpcs"
            cmd10 = "aws ec2 delete-vpc --vpc-id {}"
            out = json.loads(run(cmd9))
            for i in out["Vpcs"]:
                if "Tags" in i and cluster_name in str(i["Tags"]):
                    run(cmd10.format(i["VpcId"]))
                    print("Deleted VPC:",i["VpcId"])
            print("Deleted all VPCs")
        _runCommands(_deleteVPC, "deleteVPC")
        time.sleep(3)
                
        # delete Cloudformation Stack
        def _deleteCloudFormationStack():
            stack_name = "eksctl-" + cluster_name + "-cluster"
            cmd11 = "aws cloudformation delete-stack --stack-name {}".format(stack_name)
            run(cmd11)
            print("Deleted CloudFormation Stack")
        _runCommands(_deleteCloudFormationStack, "deleteCloudFormationStack")
   
    def testing(self):
        # add ingress rule for ports on security group
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
        """
        
        out = run(cmd2.format(controller_sg_id, "31256"))
        # out = run(cmd2.format(controller_sg_id, prometheus_port), haltException=False)

    def initWebApp(self):
        pass
    
    # call the below functions from CLI
    def createCluster(self):
        from datetime import timedelta
         
        with open("init_cluster/eks_cluster_template.yaml", 'r') as yamlfile:
            eks_cluster_yaml = yamlfile.read()
            
        worker_instance_type = "\n  - " + "\n  - ".join(self.values_yaml["cluster"]["workerInstances"])
        
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ name }}", self.values_yaml["cluster"]["name"])
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ region }}", self.values_yaml["cluster"]["region"])
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ controller.instanceType }}", self.values_yaml["cluster"]["controllerInstance"])
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ worker.instanceType }}", worker_instance_type)
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ volumeSize }}", str(self.values_yaml["cluster"]["volumeSize"]))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ desiredCapacity }}", str(self.num_workers))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ workerSpot }}", str(self.values_yaml["cluster"]["workerSpotInstance"]))
        
        with open("init_cluster/eks_cluster.yaml", "w") as yamlfile:
            yamlfile.write(eks_cluster_yaml)

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

        # add EFS storage
        self.addStorage()

        # add Local DNS Cache
        self.addLocalDNSCache()

        # install Prometheus and Grafana
        self.installMetricsMonitor()

        # initialize basic cerebro components
        self.initCerebro()

if __name__ == '__main__':
    fire.Fire(CerebroInstaller)