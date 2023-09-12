import os
import json
import time
import fire
import subprocess
import oyaml as yaml
from kubernetes import client, config
from fabric2 import ThreadingGroup, Connection


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


def getPodNames(namespace="cerebro"):
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
        values_yaml["cluster"]["workerSpotInstance"] = user_yaml["workerSpotInstance"]
        values_yaml["cluster"]["numWorkers"] = user_yaml["numWorkers"]
        values_yaml["cluster"]["controllerInstance"] = user_yaml["controllerInstance"]
        values_yaml["cluster"]["workerInstances"] = user_yaml["workerInstances"]

        with open("values.yaml", "w") as f:
            yaml.safe_dump(values_yaml, f)

    def initializeFabric(self):
        # get controller and worker addresses
        cluster_name = self.values_yaml["cluster"]["name"]
        cmd_controller = """
        aws ec2 describe-instances \
            --filters "Name=tag:aws:eks:cluster-name,Values={}" "Name=tag:eks:nodegroup-name,Values=ng-controller" \
            --query 'Reservations[*].Instances[*].PublicDnsName' \
            --output json
        """.format(cluster_name)
        cmd_workers = """
        aws ec2 describe-instances \
            --filters "Name=tag:aws:eks:cluster-name,Values={}" "Name=tag:eks:nodegroup-name,Values=ng-worker" \
            --query 'Reservations[*].Instances[*].PublicDnsName' \
            --output json
        """.format(cluster_name)

        workers_out = json.loads(run(cmd_workers))
        controller_out = json.loads(run(cmd_controller))
        self.workers = []
        for i in workers_out:
            self.workers.extend(i)
        self.controller = controller_out[0][0]

        # load pem and initialize connections
        user = "ec2-user"
        pem_path = os.path.abspath(self.values_yaml["creds"]["pemPath"])
        connect_kwargs = {"key_filename": pem_path}

        self.conn = Connection(self.controller, user=user, connect_kwargs=connect_kwargs)
        self.s = ThreadingGroup(*self.workers, user=user,
                                connect_kwargs=connect_kwargs)

    def oneTime(self):
        # install AWS CLI
        # read auth paths
        region = self.values_yaml["cluster"]["region"]
        pem_path = os.path.abspath(self.values_yaml["creds"]["pemPath"])
        csv_path = os.path.abspath(self.values_yaml["creds"]["csvPath"])

        # AWS Auth
        cmds = [
            "rm -f awscliv2.zip",
            'curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"',
            "unzip awscliv2.zip",
            "sudo ./aws/install",
            "rm -f awscliv2.zip"
        ]
        for cmd in cmds:
            run(cmd, capture_output=False)
        print("Installed AWS CLI")

        # add default user name in CSV file
        with open(csv_path) as f:
            csv = f.read()
        csv = csv.split("\n")
        csv[0] = "User Name," + csv[0]
        csv[1] = "default," + csv[1]
        with open(csv_path, "w") as f:
            f.write("\n".join(csv))

        # configure AWS
        aws_configure = "aws configure import --csv file://{}".format(csv_path)
        run(aws_configure)
        print("Configured AWS CLI with given credentials")

        # set default region
        region = self.values_yaml["cluster"]["region"]
        aws_region = "aws configure set region {}".format(region)
        run(aws_region)
        print("Set default region to {}".format(region))

        # create key-value pair
        public_key_name = pem_path.split("/")[-1].split(".")[0]
        kp_exists_cmd = "aws ec2 describe-key-pairs --query 'KeyPairs[].KeyName'"
        names = json.loads(run(kp_exists_cmd))
        if public_key_name not in names:
            aws_kp = """
            aws ec2 create-key-pair \
            --key-name {} \
            --key-type rsa \
            --key-format pem \
            --query "KeyMaterial" \
            --output text > {}
            """.format(public_key_name, pem_path)
            run(aws_kp)
            print("Created Key-Value pair: {}".format(pem_path))
        else:
            print("Key-pair already exists")

        aws_kp_chmod = "chmod 400 {}".format(pem_path)
        run(aws_kp_chmod)

        # create IAM EFS Policy
        check_exists_cmd = """aws iam list-policies --query "Policies[?PolicyName=='AmazonEKS_EFS_CSI_Driver_Policy']" """
        out = json.loads(run(check_exists_cmd))
        if len(out) == 0:
            aws_iam_efs = """
            aws iam create-policy \
                --policy-name AmazonEKS_EFS_CSI_Driver_Policy \
                --policy-document file://init_cluster/iam-policy-eks-efs.json
            """
            run(aws_iam_efs)
            print("Created IAM policy for EFS")
        else:
            print("IAM policy for EFS already exists")

        # install other dependencies

        # install EKSCTL
        uname = run("uname -s")
        platform = uname + "_amd64"
        cmds = [
            'curl -sLO "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_{}.tar.gz"'.format(platform),
            'tar -xzf eksctl_{}.tar.gz -C /tmp && rm eksctl_{}.tar.gz'.format(platform, platform),
            'sudo mv /tmp/eksctl /usr/local/bin'
        ]
        for cmd in cmds:
            run(cmd)
        print("Installed eksctl")

        # install kubectl
        cmds = [
            "curl -sLO https://s3.us-west-2.amazonaws.com/amazon-eks/1.26.4/2023-05-11/bin/linux/amd64/kubectl",
            "sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl",
            "rm kubectl"
        ]
        for cmd in cmds:
            run(cmd)
        print("Installed kubectl")

        # install Helm
        cmds = [
            "curl -sLO https://get.helm.sh/helm-v3.12.1-linux-amd64.tar.gz",
            "tar -zxvf helm-v3.12.1-linux-amd64.tar.gz",
            "sudo mv linux-amd64/helm /usr/local/bin/helm",
            "rm helm-v3.12.1-linux-amd64.tar.gz",
            "rm -rf linux-amd64"
        ]
        for cmd in cmds:
            run(cmd)
        print("Installed helm")

        print("Done!")

    def addStorage(self):
        # load kubernetes config
        config.load_kube_config()

        region = self.values_yaml["cluster"]["region"]
        cluster_name = self.values_yaml["cluster"]["name"]
        image_registry = "602401143452.dkr.ecr.{}.amazonaws.com".format(region)

        # add OIDC to IAM role
        cmd4 = "eksctl utils associate-iam-oidc-provider --cluster {} --approve".format(cluster_name)
        run(cmd4, capture_output=False)

        # create service account
        cmd5 = "aws sts get-caller-identity"
        account_id = json.loads(run(cmd5))["Account"]
        print("AccountId:", account_id)

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
            --set controller.serviceAccount.name=efs-csi-controller-sa \
            --wait
        """.format(image_registry)
        run(cmd7, capture_output=False)
        print("Installed EFS CSI driver. CSI pods ready")

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

        # create the AWS EFS File System (unencrypted)
        cmd6 = """
            aws efs create-file-system \
            --region {} \
            --performance-mode maxIO \
            --query 'FileSystemId'
            """.format(region)
        out = run(cmd6)
        file_sys_id = json.loads(out)
        print("Created IO-Optimized EFS file system")
        self.values_yaml["cluster"]["efsFileSystemId"] = file_sys_id
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)
        print("Saved FileSystem ID in values.yaml")
        time.sleep(3)

        # get subnets for vpc
        cmd8 = "aws ec2 describe-subnets --filter Name=vpc-id,Values={} --query 'Subnets[?MapPublicIpOnLaunch==`false`].SubnetId'".format(vpc_id)
        out = run(cmd8)
        subnets_list = json.loads(out)
        print("Subnets list:", str(subnets_list))
        time.sleep(3)

        # create mount targets
        cmd9 = """
        for subnet in {}; do
        aws efs create-mount-target \
            --file-system-id {} \
            --security-group  {} \
            --subnet-id $subnet \
            --region {}
        done"""
        out = run(cmd9.format(" ".join(subnets_list), file_sys_id, sg_id, region))
        print("Created mount targets for all subnets for file system {}".format(file_sys_id))

        # create storage class
        sc = client.V1StorageClass(
            api_version="storage.k8s.io/v1",
            kind="StorageClass",
            metadata=client.V1ObjectMeta(name="efs-sc"),
            provisioner="efs.csi.aws.com",
            parameters={"provisioningMode": "efs-ap", "fileSystemId": str(file_sys_id), "directoryPerms": "700"}
        )
        api = client.StorageV1Api()
        api.create_storage_class(body=sc)

        print("Created Storage Class")

        # get OIDC arn
        cmd12 = "aws iam list-open-id-connect-providers \
            --query 'OpenIDConnectProviderList[].Arn' \
            --region {} \
            --output text".format(region)
        oidc_arn = run(cmd12)
        oidc_arn_partial = oidc_arn.split("oidc-provider/")[-1]

        # create serviceaccount
        v1 = client.CoreV1Api()
        metadata = client.V1ObjectMeta(name="s3-eks-sa")
        service_account = client.V1ServiceAccount(metadata=metadata)
        v1.create_namespaced_service_account(namespace=self.kube_namespace, body=service_account)
        print("Created service account")

        # create s3 role
        check_role_exists = """ aws iam list-roles --query "Roles[?RoleName=='eks-s3-role']" --output text """
        out = run(check_role_exists)
        if "eks-s3-role" in out:
            print("eks-s3-role exists. Skipping...")
        else:
            cmd13 = """
            aws iam create-role \
            --role-name eks-s3-role \
            --no-cli-pager \
            --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Federated": "%s"
                    },
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                        "%s:aud": "sts.amazonaws.com",
                        "%s:sub": "system:serviceaccount:%s:%s"
                        }
                    }
                }
            ]}'
            """ % (oidc_arn, oidc_arn_partial, oidc_arn_partial, self.kube_namespace, "s3-eks-sa")
            run(cmd13, capture_output=False)
            print("Created S3 role")

        # annotate service account for s3
        annotation_key = "eks.amazonaws.com/role-arn"
        annotation_value = "arn:aws:iam::{}:role/eks-s3-role".format(account_id)

        service_account = v1.read_namespaced_service_account("s3-eks-sa", self.kube_namespace)
        service_account.metadata.annotations = service_account.metadata.annotations or {}
        service_account.metadata.annotations[annotation_key] = annotation_value

        v1.patch_namespaced_service_account("s3-eks-sa", self.kube_namespace, service_account)
        print("Service account annotated with role for S3")

    def addLocalDNSCache(self):
        time.sleep(5)
        self.initializeFabric()
        self.s.run("mkdir -p /home/ec2-user/init_cluster")
        self.s.put("init_cluster/local_dns.sh", "/home/ec2-user/init_cluster")
        # run local_dns script
        self.s.sudo("/bin/bash /home/ec2-user/init_cluster/local_dns.sh")

        print("Created Local DNS Cache on worker nodes")

    def installMetricsMonitor(self):
        time.sleep(5)

        # load fabric connections
        self.initializeFabric()

        config.load_kube_config()
        v1 = client.CoreV1Api()

        prometheus_port = self.values_yaml["cluster"]["networking"]["prometheusNodePort"]
        grafana_port = self.values_yaml["cluster"]["networking"]["grafanaNodePort"]

        cmds = [
            "kubectl create namespace prom-metrics",
            "helm repo add prometheus-community https://prometheus-community.github.io/helm-charts",
            "helm repo update"
        ]

        for cmd in cmds:
            out = run(cmd)
            print(out)

        cmd1 = """
        helm install prometheus prometheus-community/kube-prometheus-stack \
        --namespace prom-metrics \
        --values init_cluster/kube-prometheus-stack.values \
        --set prometheus.service.nodePort={} \
        --set prometheus.service.type=NodePort \
        --version 45.6.0""".format(prometheus_port)

        cmd2 = "helm repo add grafana https://grafana.github.io/helm-charts"
        cmd3 = "helm upgrade --install loki grafana/loki-stack -n prom-metrics"

        print("Installing Prometheus and Grafana...")
        run(cmd1, capture_output=False)
        run(cmd2, capture_output=False)
        run(cmd3, capture_output=False)

        time.sleep(5)

        name = "prometheus-grafana"
        ns = "prom-metrics"
        body = v1.read_namespaced_service(namespace=ns, name=name)
        body.spec.type = "NodePort"
        body.spec.ports[0].node_port = grafana_port
        v1.patch_namespaced_service(name, ns, body)

        # install Nvidia DCGM-Exporter
        cmds = [
            "helm repo add gpu-dcgm https://nvidia.github.io/dcgm-exporter/helm-charts",
            "helm install dcgm-exporter gpu-dcgm/dcgm-exporter --namespace prom-metrics"
        ]
        for cmd in cmds:
            out = run(cmd)
            print(out)

        # increase DCGM DeamonSet LivenessProbe timeout
        cmd1 = """
            kubectl patch ds -n prom-metrics  dcgm-exporter -p \
            '{"spec":{"template":{"spec":{"containers":[{"name":"exporter", "livenessProbe":{"failureThreshold": 7}}]}}}}'
        """
        run(cmd1, capture_output=False)
        print("Installed Nvidia DCGM-Exporter")

        # add Cerebro Dashboard to Grafana
        cmd1 = "kubectl create configmap -n prom-metrics cerebro-dashboard --from-file='./misc/cerebro_dashboard.json'"
        cmd2 = "kubectl label configmap -n prom-metrics cerebro-dashboard grafana_dashboard=1"
        run(cmd1)
        run(cmd2)
        print("Created Cerebro Dashboard in Grafana")

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

        out = run(cmd2.format(controller_sg_id, prometheus_port), haltException=False)
        out = run(cmd2.format(controller_sg_id, grafana_port), haltException=False)

        print("Added Ingress rules in Controller SecurityGroup for Grafana and Prometheus ports")

        # change Grafana credentials
        cmd = """
        kubectl exec --namespace prom-metrics -c grafana -it \
        $(kubectl get pods --namespace prom-metrics -l "app.kubernetes.io/name=grafana" -o jsonpath="{.items[0].metadata.name}") -- \
        grafana-cli admin reset-admin-password cerebro
        """
        run(cmd)
        print("Changed Grafana credentials")

        print("Setup of Metrics Monitoring Complete.")

    def installKeyValueStore(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # create key-value-store namespace
        db_namespace = "key-value-store"
        metadata = client.V1ObjectMeta(name=db_namespace)
        spec = client.V1NamespaceSpec()
        namespace = client.V1Namespace(metadata=metadata, spec=spec)
        v1.create_namespace(namespace)
        print("Created key-value-store namespace")

        cmds = [
            "helm repo add bitnami https://charts.bitnami.com/bitnami",
            "helm repo update",
            "helm install redis bitnami/redis \
                --namespace {} \
                --set master.nodeSelector.role=controller \
                --set architecture=standalone \
                --set global.storageClass=efs-sc \
                --set global.redis.password=cerebro \
                --set disableCommands[0]= \
                --set disableCommands[1]= \
                --wait".format(db_namespace)
        ]

        for cmd in cmds:
            run(cmd)

        print("Installed Redis DB successfully")

    def initCerebro(self):
        # load fabric connections
        self.initializeFabric()

        config.load_kube_config()
        v1 = client.CoreV1Api()

        # save Cerebro publicDNS
        cluster_name = self.values_yaml["cluster"]["name"]
        cmd3 = """
        aws ec2 describe-instances \
            --filters "Name=tag:aws:eks:cluster-name,Values={}" "Name=tag:eks:nodegroup-name,Values=ng-controller" \
            --query 'Reservations[*].Instances[*].PublicDnsName' \
            --output json
        """.format(cluster_name)
        public_dns_name = json.loads(run(cmd3))[0][0]
        self.values_yaml["cluster"]["networking"]["publicDNSName"] = public_dns_name
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)

        # create namespace, set context and setup kube-config
        cmds1 = [
            # "kubectl create namespace {}".format(self.kube_namespace),
            "kubectl config set-context --current --namespace={}".format(
                self.kube_namespace),
            "kubectl create -n {} secret generic kube-config --from-file={}".format(
                self.kube_namespace, os.path.expanduser("~/.kube/config")),
            "kubectl create -n {} secret generic aws-config --from-file={}".format(
                self.kube_namespace, os.path.expanduser("~/.aws/credentials")),
            "kubectl create -f init_cluster/rbac_roles.yaml"
        ]

        for cmd in cmds1:
            out = run(cmd)
            print(out)
        print("Created Cerebro namespace, set context and added kube-config secret")

        # add EKS identity mapping for Role
        cmd1 = "aws sts get-caller-identity"
        cmd2 = """
        eksctl create iamidentitymapping \
            --cluster {} \
            --region={} \
            --arn arn:aws:iam::{}:role/copy-role \
            --group system:nodes \
            --no-duplicate-arns
        """
        account_id = json.loads(run(cmd1))["Account"]
        cluster_name = self.values_yaml["cluster"]["name"]
        cluster_region = self.values_yaml["cluster"]["region"]
        run(cmd2.format(cluster_name, cluster_region, account_id))

        # create kubernetes secret using ssh key and git server as known host
        kube_git_secret = "kubectl create secret generic git-creds --from-file=ssh=$HOME/.ssh/id_rsa"
        run(kube_git_secret)
        print("Created kubernetes secret for git")

        # add node local DNS cache
        cmd2 = "kubectl get svc kube-dns -n kube-system -o jsonpath={.spec.clusterIP}"
        kubedns = run(cmd2)
        domain = "cluster.local"
        localdns = self.values_yaml["cluster"]["networking"]["nodeLocalListenIP"]

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
        node_hardware_info = {}
        cores, gpus = [], []

        # get number of cores
        out = self.s.run("grep -c ^processor /proc/cpuinfo", hide=True)
        for _, ans in out.items():
            cores.append(int(ans.stdout.strip()))

        # get number of GPUs
        out = self.s.run("nvidia-smi --query-gpu=name --format=csv,noheader | wc -l", hide=True)
        for _, ans in out.items():
            gpus.append(int(ans.stdout.strip()))

        for i in range(self.num_workers):
            node_hardware_info["node" + str(i)] = {
                "num_cores": cores[i],
                "num_gpus": gpus[i]
            }

        # save min num_gpus in values.yaml
        num_gpus = min(gpus)
        self.values_yaml["cluster"]["numGPUs"] = num_gpus
        with open("values.yaml", "w") as f:
            yaml.safe_dump(self.values_yaml, f)

        # create node hardware info configmap
        configmap = client.V1ConfigMap(data={"data": json.dumps(node_hardware_info)}, metadata=client.V1ObjectMeta(name="node-hardware-info"))
        v1.create_namespaced_config_map(namespace=self.kube_namespace, body=configmap)
        print("Created configmap for node hardware info")

        # make configmap of select values.yaml values
        configmap_values = {
            "cluster_name": self.values_yaml["cluster"]["name"],
            "controller_data_path": self.values_yaml["controller"]["volumes"]["dataMountPath"],
            "worker_rpc_port": self.values_yaml["worker"]["rpcPort"],
            "user_repo_path": self.values_yaml["controller"]["volumes"]["userRepoMountPath"],
            "webapp_backend_port": self.values_yaml["webApp"]["backendPort"],
            "public_dns_name": self.values_yaml["cluster"]["networking"]["publicDNSName"],
            "jupyter_token_string": self.values_yaml["creds"]["jupyterTokenSting"],
            "jupyter_node_port": self.values_yaml["controller"]["services"]["jupyterNodePort"],
            "tensorboard_node_port": self.values_yaml["controller"]["services"]["tensorboardNodePort"],
            "grafana_node_port": self.values_yaml["cluster"]["networking"]["grafanaNodePort"],
            "prometheus_node_port": self.values_yaml["cluster"]["networking"]["prometheusNodePort"],
            "loki_port": self.values_yaml["cluster"]["networking"]["lokiPort"],
            "shard_multiplicity": self.values_yaml["worker"]["shardMultiplicity"],
            "sample_size": self.values_yaml["worker"]["sampleSize"]
        }

        # create cerebro info configmap
        configmap = client.V1ConfigMap(data={"data": json.dumps(configmap_values)}, metadata=client.V1ObjectMeta(name="cerebro-info"))
        v1.create_namespaced_config_map(namespace=self.kube_namespace, body=configmap)
        print("Created configmap for Cerebro values info")

        # add ingress rule for JupyterNotebook, Tensorboard and WebServer ports on security group
        jupyter_node_port = self.values_yaml["controller"]["services"]["jupyterNodePort"]
        tensorboard_node_port = self.values_yaml["controller"]["services"]["tensorboardNodePort"]
        ui_node_port = self.values_yaml["webApp"]["uiNodePort"]
        backend_node_port = self.values_yaml["webApp"]["backendNodePort"]
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

        out = run(cmd2.format(controller_sg_id, jupyter_node_port, haltException=False))
        out = run(cmd2.format(controller_sg_id, tensorboard_node_port, haltException=False))
        out = run(cmd2.format(controller_sg_id, ui_node_port, haltException=False))
        out = run(cmd2.format(controller_sg_id, backend_node_port, haltException=False))

    def createController(self):
        # load fabric connections
        self.initializeFabric()

        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-controller",
            "rm -rf charts/cerebro-controller/templates/*",
            "cp ./controller/* charts/cerebro-controller/templates/",
            "cp values.yaml charts/cerebro-controller/values.yaml",
            "helm install --namespace=cerebro controller charts/cerebro-controller/",
            "rm -rf charts/cerebro-controller"
        ]

        for cmd in cmds:
            time.sleep(1)
            run(cmd, capture_output=False)

        print("Created Controller deployment")

        config.load_kube_config()
        v1 = client.AppsV1Api()
        ready = False
        deployment = v1.list_namespaced_deployment(namespace="cerebro", label_selector="app=cerebro-controller").items[0]
        deployment_name = deployment.metadata.name

        while not ready:
            rollout = v1.read_namespaced_deployment_status(name=deployment_name, namespace=self.kube_namespace)
            if rollout.status.ready_replicas == rollout.status.replicas:
                print("Controller ready")
                ready = True
            else:
                time.sleep(1)

    def createWorkers(self):
        # load fabric connections
        self.initializeFabric()

        # create Workers
        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-worker",
            "rm -rf charts/cerebro-worker/templates/*",
            "cp worker/* charts/cerebro-worker/templates/",
            "cp values.yaml charts/cerebro-worker/values.yaml",
            "helm install --namespace={} worker charts/cerebro-worker".format(self.kube_namespace)
        ]

        for cmd in cmds:
            time.sleep(0.5)
            run(cmd, capture_output=False)
        run("rm -rf charts")

        print("Waiting for ETL Worker start-up")
        time.sleep(5)

        print("Workers created successfully")

    def createWebApp(self):
        # load fabric connections
        self.initializeFabric()

        cmds = [
            "mkdir -p charts",
            "helm create charts/cerebro-webapp",
            "rm -rf charts/cerebro-webapp/templates/*",
            "cp ./webapp/* charts/cerebro-webapp/templates/",
            "cp values.yaml charts/cerebro-webapp/values.yaml",
            "helm install --namespace=cerebro webapp charts/cerebro-webapp/",
            "rm -rf charts/cerebro-webapp"
        ]

        for cmd in cmds:
            time.sleep(1)
            _ = run(cmd, capture_output=False)

        print("Created WebApp deployment")

        config.load_kube_config()
        v1 = client.AppsV1Api()
        ready = False
        deployment = v1.list_namespaced_deployment(namespace=self.kube_namespace, label_selector='app=cerebro-webapp').items[0]
        deployment_name = deployment.metadata.name

        while not ready:
            rollout = v1.read_namespaced_deployment_status(name=deployment_name, namespace=self.kube_namespace)
            if rollout.status.ready_replicas == rollout.status.replicas:
                print("Webapp ready")
                ready = True
            else:
                time.sleep(1)

    def cleanUp(self):
        # TODO: reset all data from prometheus

        # load fabric connections
        self.initializeFabric()

        # load kubernetes config
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # clean up Workers
        try:
            cmd2 = "helm delete worker"
            run(cmd2, capture_output=False, haltException=False)

            for i in range(self.num_workers):
                v1.delete_namespaced_persistent_volume_claim(name="cerebro-data-storage-worker-cerebro-worker-{}".format(i), namespace=self.kube_namespace)
                print("Deleted PVC cerebro-data-storage-worker-cerebro-worker-{}".format(i))
        except Exception as e:
            print("Got error while cleaning up Workers: " + str(e))

        print("Cleaned up Workers")

        # wipe Key-Value Store
        keys = ["heartbeat", "kvs_init", "etl_task", "etl_func", "etl_worker_status", "etl_worker_progress"]
        keys2 = ["mop_spec", "mop_task", "mop_model_on_worker", "mop_model_parallelism_on_worker", "mop_worker_status", "mop_model_mapping", "mop_parallelism_mapping", "mop_sample_time"]
        cmd = "kubectl exec -it redis-master-0 -n key-value-store -- redis-cli -a cerebro DEL {}"
        try:
            for k in keys:
                run(cmd.format(k))
            for k in keys2:
                run(cmd.format(k))
        except Exception as e:
            print("Got error while cleaning up Key-Value Store: " + str(e))

        # clean up Controller
        try:
            cmd3 = "helm delete controller"
            run(cmd3, haltException=False)

            # delete PVCs
            v1.delete_namespaced_persistent_volume_claim(name="cerebro-repo-pvc", namespace=self.kube_namespace)
            v1.delete_namespaced_persistent_volume_claim(name="user-repo-pvc", namespace=self.kube_namespace)
            v1.delete_namespaced_persistent_volume_claim(name="cerebro-checkpoint-pvc", namespace=self.kube_namespace)
            v1.delete_namespaced_persistent_volume_claim(name="cerebro-config-pvc", namespace=self.kube_namespace)
            v1.delete_namespaced_persistent_volume_claim(name="cerebro-data-pvc", namespace=self.kube_namespace)
            v1.delete_namespaced_persistent_volume_claim(name="cerebro-metrics-pvc", namespace=self.kube_namespace)
        except Exception as e:
            print("Got error while cleaning up Controller: " + str(e))

        print("Cleaned up Controller")

        # clean up webapp
        try:
            run("helm delete webapp")
            print("Cleaned up Webapp")
        except Exception as e:
            print("Got error while cleaning up WebApp: " + str(e))

        # wait for all pods to shutdown
        pods_list = v1.list_namespaced_pod("cerebro")
        while pods_list.items != []:
            time.sleep(1)
            print("Waiting for pods to shutdown...")
            pods_list = v1.list_namespaced_pod("cerebro")

        print("Done")

    def deleteCluster(self):
        def _runCommands(fn, name):
            try:
                fn()
            except Exception as e:
                print("Ignoring command failure for - ", name)
                print(str(e))

        cluster_name = self.values_yaml["cluster"]["name"]

        cmd12 = "aws efs describe-file-systems --output json"
        out = json.loads(run(cmd12))
        fs_ids = []
        for i in out["FileSystems"]:
            fs_ids.append(i["FileSystemId"])
        print("Found file systems:", str(fs_ids))

        # delete all services
        def _delete_services():
            config.load_kube_config()
            v1 = client.CoreV1Api()
            services = v1.list_service_for_all_namespaces().items
            for service in services:
                v1.delete_namespaced_service(
                    name=service.metadata.name,
                    namespace=service.metadata.namespace,
                    body=client.V1DeleteOptions(propagation_policy='Foreground')
                )
                print("Deleted service: {} in namespace: {}".format(service.metadata.name, service.metadata.namespace))
        _runCommands(_delete_services, "servicesDelete")

        # delete the cluster
        def _deleteCluster():
            cmd1 = "eksctl delete cluster --name {}".format(cluster_name)
            run(cmd1, capture_output=False)
            print("Deleted the cluster")
        _runCommands(_deleteCluster, "clusterDelete")

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
            for mt_id in mt_ids:
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
                    print("Deleted Subnet:", i["SubnetId"])
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
                    print("Deleted VPC:", i["VpcId"])
            print("Deleted all VPCs")
        _runCommands(_deleteVPC, "deleteVPC")
        time.sleep(3)

        # delete the cluster and wait
        def _deleteClusterWait():
            cmd1 = "eksctl delete cluster --name {}".format(cluster_name)
            run(cmd1, capture_output=False)
            print("delete_cluster_wait complete")
        _runCommands(_deleteClusterWait, "clusterDeleteWait")

        # delete Cloudformation Stack
        def _deleteCloudFormationStack():
            stack_name = "eksctl-" + cluster_name + "-cluster"
            cmd11 = "aws cloudformation delete-stack --stack-name {}".format(stack_name)
            run(cmd11)
            print("Deleted CloudFormation Stack")
        _runCommands(_deleteCloudFormationStack, "deleteCloudFormationStack")

        # delete S3 policy
        def _delete_policy():
            cmd = "aws sts get-caller-identity"
            account_id = json.loads(run(cmd))["Account"]
            arn = "arn:aws:iam::{}:policy/eks-s3-policy".format(account_id)
            detach_cmds = [
                "aws iam detach-role-policy --role-name {} --policy-arn {}".format("eks-s3-role",arn),
                "aws iam delete-policy --policy-arn {}".format(arn)
            ]
            for cmd in detach_cmds:
                run(cmd)
            print("Deleted S3 policy")
        _runCommands(_delete_policy, "deletePolicy")

        # delete S3 role
        def _delete_role():
            role_delete_cmd = "aws iam delete-role --role-name {}".format("eks-s3-role")
            run(role_delete_cmd)
            print("Deleted S3 role")
        _runCommands(_delete_role, "deleteRole")

        # delete OIDC provider
        def _delete_OIDC():
            oidc_arn_cmd = "aws iam list-open-id-connect-providers \
                --query 'OpenIDConnectProviderList[].Arn' \
                --region {} \
                --output text".format(self.values_yaml["cluster"]["region"])
            oidc_delete_cmd = "aws iam delete-open-id-connect-provider --open-id-connect-provider-arn {}"
            oidc_arn = run(oidc_arn_cmd)
            run(oidc_delete_cmd.format(oidc_arn))
            print("Deleted OIDC provider")
        _runCommands(_delete_OIDC, "deleteOIDC")

        print("Cluster delete complete")

    def pauseCluster(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # cleanUp cluster
        print("Clearing Cerebro resources...")
        self.cleanUp()

        # scale down cluster to 0 nodes
        print("Scaling down cluster...")
        cluster_name = self.values_yaml["cluster"]["name"]
        cmd1 = "eksctl scale nodegroup --cluster {} --name ng-worker --nodes 0 --nodes-max 1 --nodes-min 0 --wait"
        cmd2 = "eksctl scale nodegroup --cluster {} --name ng-controller --nodes 0 --nodes-max 1 --nodes-min 0 --wait"
        run(cmd1.format(cluster_name), capture_output=False, haltException=False)
        run(cmd2.format(cluster_name), capture_output=False, haltException=False)

        # wait for desired number of nodes
        current_nodes = None
        while current_nodes != 0:
            nodes_list = v1.list_node().items
            current_nodes = len(nodes_list)
            time.sleep(1)

        print("Cluster paused!")

    def resumeCluster(self):
        config.load_kube_config()
        v1 = client.CoreV1Api()

        # scale up cluster to num_nodes
        print("Scaling up cluster...")
        cluster_name = self.values_yaml["cluster"]["name"]
        num_nodes = self.values_yaml["cluster"]["numWorkers"]
        cmd1 = "eksctl scale nodegroup --cluster {0} --name ng-controller --nodes 1 --nodes-max 1 --nodes-min 0 --wait"
        cmd2 = "eksctl scale nodegroup --cluster {0} --name ng-worker --nodes {1} --nodes-max {1} --nodes-min 0 --wait"
        run(cmd1.format(cluster_name), capture_output=False, haltException=False)
        run(cmd2.format(cluster_name, num_nodes), capture_output=False, haltException=False)

        # wait for desired num_nodes
        current_nodes = 0
        while current_nodes != num_nodes + 1:
            nodes_list = v1.list_node().items
            current_nodes = len(nodes_list)
            time.sleep(1)

        # install Cerebro
        print("Creating Cerebro objects...")
        # self.installCerebro()

        # load fabric connections
        self.initializeFabric()

        # cmds = [
        #     "iptables -P INPUT ACCEPT",
        #     "iptables -P FORWARD ACCEPT",
        #     "iptables -P OUTPUT ACCEPT",
        #     "iptables -F"
        # ]
        # for cmd in cmds:
        #     self.conn.sudo(cmd)
        #     self.s.sudo(cmd)

        print("Cluster resumed!")

    def testing(self):
        pass

    # call the below functions from CLI
    def createCluster(self):
        from datetime import timedelta

        region = self.values_yaml["cluster"]["region"]

        with open("init_cluster/eks_cluster_template.yaml", 'r') as yamlfile:
            eks_cluster_yaml = yamlfile.read()

        worker_instance_type = "\n  - " + "\n  - ".join(self.values_yaml["cluster"]["workerInstances"])
        public_key_name = str(os.path.abspath(self.values_yaml["creds"]["pemPath"])).split("/")[-1].split(".")[0]

        # get ami value for kubernetes version
        cmd = 'aws ssm get-parameter --name /aws/service/eks/optimized-ami/{}/amazon-linux-2-gpu/recommended/image_id --region {} --query "Parameter.Value" --output text'
        ami = run(cmd.format(self.values_yaml["cluster"]["kubernetesVersion"], region))

        # get set of availability zones that support all instance types
        cmd1 = "aws ec2 describe-instance-type-offerings --location-type availability-zone  --filters Name=instance-type,Values={} --region {} | jq -r '.InstanceTypeOfferings[].Location'"
        availbility_zones = []
        for i in self.values_yaml["cluster"]["workerInstances"]:
            zones = run(cmd1.format(i, region)).split()
            availbility_zones.append(set(zones))
        availbility_zones = "\n  - " + "\n  - ".join(set.intersection(*availbility_zones))

        eks_cluster_yaml = eks_cluster_yaml.replace("{{ ami }}", ami)
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ publicKeyName }}", public_key_name)
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ availabilityZones }}", availbility_zones)
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ desiredCapacity }}", str(self.num_workers))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ name }}", self.values_yaml["cluster"]["name"])
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ worker.instanceType }}", worker_instance_type)
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ region }}", self.values_yaml["cluster"]["region"])
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ volumeSize }}", str(self.values_yaml["cluster"]["volumeSize"]))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ workerSpot }}", str(self.values_yaml["cluster"]["workerSpotInstance"]))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ kubernetesVersion }}", str(self.values_yaml["cluster"]["kubernetesVersion"]))
        eks_cluster_yaml = eks_cluster_yaml.replace("{{ controller.instanceType }}", self.values_yaml["cluster"]["controllerInstance"])

        with open("init_cluster/eks_cluster.yaml", "w") as yamlfile:
            yamlfile.write(eks_cluster_yaml)

        try:
            start = time.time()
            cmd = "eksctl create cluster -f ./init_cluster/eks_cluster.yaml"
            subprocess.run(cmd, shell=True, text=True)
            end = time.time()
            print("Created cluster successfully")
            print("Time taken to create cluster:", str(timedelta(seconds=end - start)))
        except Exception as e:
            print("Couldn't create the cluster")
            print(str(e))

        # create Cerebro namespace
        config.load_kube_config()
        v1 = client.CoreV1Api()
        metadata = client.V1ObjectMeta(name=self.kube_namespace)
        spec = client.V1NamespaceSpec()
        namespace = client.V1Namespace(metadata=metadata, spec=spec)
        v1.create_namespace(namespace)
        print("Created Cerebro namespace")

        # add EFS storage
        self.addStorage()

        # add Local DNS Cache
        self.addLocalDNSCache()

        # install Prometheus and Grafana
        self.installMetricsMonitor()

        # install Key-Value Store
        self.installKeyValueStore()

        # initialize basic cerebro components
        self.initCerebro()

        # install Cerebro
        self.installCerebro()

    def installCerebro(self):
        # creates Controller
        self.createController()

        # create webapp
        self.createWebApp()

        # create Workers
        self.createWorkers()

        url = self.values_yaml["cluster"]["networking"]["publicDNSName"]
        port = self.values_yaml["webApp"]["uiNodePort"]

        time.sleep(5)
        print("You can access the cluster using this URL:")
        print("http://{}:{}".format(url, port))


if __name__ == '__main__':
    fire.Fire(CerebroInstaller)
