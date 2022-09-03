import os
import json
import site
import time
import yaml
import subprocess
from pathlib import Path
from importlib import reload
from argparse import ArgumentParser

# need to have password sans cloudlab.pem copied to scheduler
# sudo ssh-keygen -p -N "" -f ./cloudlab.pem

#TODO: add namespace param to all kubectl commands


def import_or_install(package):
    try:
        __import__(package)
    except ImportError:
        import pip
        pip.main(['install', package])
    finally:
        reload(site)


def check_pod_status(label, namespace):
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

def get_pod_names(namespace):
    from kubernetes import client, config

    label = "app=cerebro-controller"
    config.load_kube_config()
    v1 = client.CoreV1Api()
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    controller = pods_list.items[0].metadata.name

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
    def __init__(self, root_path, workers, kube_params):
        self.w = workers
        self.root_path = root_path

        self.kube_namespace = kube_params["kube_namespace"]
        self.cerebro_checkpoint_hostpath = kube_params["cerebro_checkpoint_hostpath"]
        self.cerebro_config_hostpath = kube_params["cerebro_config_hostpath"]
        self.cerebro_data_hostpath = kube_params["cerebro_data_hostpath"]
        self.cerebro_worker_data_hostpath = kube_params["cerebro_worker_data_hostpath"]

        self.s = None
        self.conn = None
        self.username = None

    def init_fabric(self):
        from fabric2 import ThreadingGroup, Connection

        nodes = ["node"+str(i) for i in range(1, self.w)]
        host = "node0"

        self.username = subprocess.run(
            "whoami", capture_output=True, text=True).stdout.strip("\n")
        self.root_path = self.root_path.format(self.username)

        #initialize fabric
        user = self.username
        pem_path = "/users/{}/cloudlab.pem".format(self.username)
        connect_kwargs = {"key_filename": pem_path}
        self.conn = Connection(host, user=user, connect_kwargs=connect_kwargs)
        self.s = ThreadingGroup(*nodes, user=user,
                                connect_kwargs=connect_kwargs)

    def init(self):
        subprocess.call(["sudo", "apt", "update"])
        subprocess.call(["sudo", "apt", "install", "-y", "python3-pip"])
        self.username = subprocess.run(
            "whoami", capture_output=True, text=True).stdout.strip("\n")
        self.root_path = self.root_path.format(self.username)

        import_or_install("fabric2")
        import_or_install("dask[complete]")
        subprocess.call(
            ["bash", "{}/misc/path.sh".format(self.root_path)])

        self.init_fabric()

        # create /mnt and run save space using the cloudlab command
        self.conn.sudo("sudo /usr/local/etc/emulab/mkextrafs.pl /mnt")
        self.s.sudo("sudo /usr/local/etc/emulab/mkextrafs.pl /mnt")

        self.s.run("rm -rf /users/{}/cerebro-kube-setup".format(self.username))

        # change permissions of pem file
        home = str(Path.home())
        cmd = "sudo chmod 400 {}/cloudlab.pem".format(home)
        self.conn.sudo(cmd)
        
        # add all nodes to known_hosts
        Path(home + "/.ssh").mkdir(parents=True, exist_ok=True)
        cmd = "ssh-keyscan -H {} >> {}/.ssh/known_hosts"
        for i in range(1, self.w):
            self.conn.run(cmd.format("node" + str(i), home))

        # copy repo to all nodes
        cmd = "scp -i {}/cloudlab.pem -r {} {}:{}"
        for i in range(1, self.w):
            self.conn.run(cmd.format(home, self.root_path, "node" + str(i), self.root_path))

        self.conn.sudo(
            "sudo /bin/bash {}/misc/save_space.sh".format(self.root_path))
        self.s.sudo(
            "sudo /bin/bash {}/misc/save_space.sh".format(self.root_path))

    def kubernetes_install(self):
        self.conn.sudo(
            "bash {}/init_cluster/kubernetes_install.sh {}".format(self.root_path, self.username))

    def kubernetes_join_workers(self):
        reload(site)
        
        from kubernetes import client, config

        join = self.conn.sudo("sudo kubeadm token create --print-join-command")
        node0_ip = "10.10.1.1"

        self.s.sudo(
            "bash {}/init_cluster/kubernetes_join.sh {}".format(self.root_path, node0_ip))

        self.s.sudo(join.stdout)


        config.load_kube_config()
        v1 = client.CoreV1Api()
        
        # check if nodes are ready
        notReady = "NotReady" in [i.status for i in v1.list_node().items]
        while notReady:
            print("Waiting for newly joined nodes to be ready...")
            notReady = "NotReady" in [i.status for i in v1.list_node().items]
            time.sleep(1)

        # add new label to nodes
        node_list = v1.list_node()
        for n in node_list.items:
            body = n
            name = body.metadata.labels["kubernetes.io/hostname"].split(".")[0]
            body.metadata.labels["cerebro/nodename"] = name

            v1.patch_node(body.metadata.name, body)

        self.conn.run("kubectl get nodes")

    def install_nfs(self):

        from kubernetes import client, config

        cmds = []

        cmds.append("helm create ~/nfs-config")
        cmds.append("rm -rf ~/nfs-config/templates/*")
        cmds.append(
            "cp {}/nfs-config/* ~/nfs-config/templates/".format(self.root_path))
        cmds.append(
            "cp {}/values.yaml ~/nfs-config/values.yaml".format(self.root_path))
        cmds.append("helm install --namespace={} nfs-config ~/nfs-config/".format(
            self.kube_namespace))

        for cmd in cmds:
            time.sleep(1)
            self.conn.run(cmd)

        label = "role=nfs-server"

        while not check_pod_status(label, self.kube_namespace):
            time.sleep(1)

        config.load_kube_config()
        v1 = client.CoreV1Api()
        pods_list = v1.list_namespaced_pod(
            self.kube_namespace, label_selector=label, watch=False)
        nfs_podname = pods_list.items[0].metadata.name

        cmds = []

        cmds.append('kubectl exec {} -n {} -- /bin/bash -c "rm -rf /exports/*"'.format(
            nfs_podname, self.kube_namespace))
        cmds.append('kubectl exec {} -n {} -- /bin/bash -c "mkdir {}"'.format(
            nfs_podname, self.kube_namespace, self.cerebro_checkpoint_hostpath))
        cmds.append('kubectl exec {} -n {} -- /bin/bash -c "mkdir {}"'.format(
            nfs_podname, self.kube_namespace, self.cerebro_config_hostpath))
        cmds.append('kubectl exec {} -n {} -- /bin/bash -c "mkdir {}"'.format(
            nfs_podname, self.kube_namespace, self.cerebro_data_hostpath))

        with open("{}/nfs-config/export.txt".format(self.root_path), "w") as f:
            permissions = "*(rw,insecure,no_root_squash,no_subtree_check)"
            s = "/exports" + "\t" + \
                "*(rw,insecure,fsid=0,no_root_squash,no_subtree_check)" + "\n"
            s += self.cerebro_checkpoint_hostpath + "\t" + permissions + "\n"
            s += self.cerebro_config_hostpath + "\t" + permissions + "\n"
            s += self.cerebro_data_hostpath + "\t" + permissions + "\n"
            f.write(s)
            for i in range(1, self.w - 1):
                path = self.cerebro_worker_data_hostpath.format(i)
                cmd = 'kubectl exec {} -n {} -- /bin/bash -c "mkdir {}"'.format(
                    nfs_podname, self.kube_namespace, path)
                cmds.append(cmd)
                s = path + "\t" + permissions + "\n"
                f.write(s)

        copy_exports = "kubectl cp -n {} {}/nfs-config/export.txt {}:/etc/exports".format(
            self.kube_namespace, self.root_path, nfs_podname)
        reset_exportfs = 'kubectl exec {} -n {} -- /bin/bash -c "exportfs -a"'.format(
            nfs_podname, self.kube_namespace)

        cmds.append(copy_exports)
        cmds.append(reset_exportfs)

        for cmd in cmds:
            self.conn.run(cmd)

        service = v1.read_namespaced_service(
            'nfs-server-service', self.kube_namespace)
        nfs_ip = service.spec.cluster_ip
        with open('{}/values.yaml'.format(self.root_path), 'r') as yamlfile:
            values_yaml = yaml.safe_load(yamlfile)
        values_yaml["nfs"]["ip"] = nfs_ip
        with open('{}/values.yaml'.format(self.root_path), 'w') as yamlfile:
            yaml.safe_dump(values_yaml, yamlfile)

    def install_metrics_monitor(self):
        from kubernetes import client, config

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
            self.conn.run(cmd)

        name = "prom-grafana"
        ns = "prom-metrics"
        body = v1.read_namespaced_service(namespace=ns, name=name)
        body.spec.type = "NodePort"
        v1.patch_namespaced_service(name, ns, body)

        svc = v1.read_namespaced_service(namespace=ns, name=name)
        port = svc.spec.ports[0].node_port

        print(
            "Access Grafana with this link:\nhttp://<Cloudlab Host Name>: {}".format(port))
        print("username: {}\npassword: {}".format("admin", "prom-operator"))

        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/metrics_monitor_credentials.txt", "w+") as f:
            f.write(
                "Access Grafana with this link:\nhttp://<Cloudlab Host Name>: {}\n".format(port))
            f.write("username: {}\npassword: {}".format(
                "admin", "prom-operator"))

    def init_cerebro_kube(self):
        cmds = [
            "kubectl create -f {}/other-configs/rbac_clusterroles.yaml".format(
                self.root_path),
            "kubectl create namespace {}".format(self.kube_namespace),
            "kubectl create -n {} secret generic kube-config --from-file={}".format(
                self.kube_namespace, os.path.expanduser("~/.kube/config")),
            "kubectl config set-context --current --namespace={}".format(
                self.kube_namespace),
            "kubectl create -n {} configmap cerebro-properties".format(self.kube_namespace),
            "kubectl create -n {} configmap hyperparameter-properties".format(self.kube_namespace)
        ]

        for cmd in cmds:
            self.conn.run(cmd)

        # install nfs server
        self.install_nfs()
        # install prometheus + grafana
        self.install_metrics_monitor()


        # security
        home = str(Path.home())
        # read details from values.yaml
        with open(self.root_path + "/values.yaml") as f:
            values_yaml = yaml.safe_load(f)
        print(values_yaml)

        # generate ssh key
        ssh_cmd = ' ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa -q -N "" '
        self.conn.run(ssh_cmd)

        # add git server to known hosts
        known_hosts_cmd = "ssh-keyscan {} > /tmp/known_hosts".format(values_yaml["creds"]["gitServer"])
        self.conn.run(known_hosts_cmd)
        
        # write public key to a file
        get_pub_key = 'cat ~/.ssh/id_rsa.pub'
        pub_key = self.conn.run(get_pub_key).stdout.rstrip()
        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/ssh_public_key.txt".format(self.root_path), "w+") as f:
            f.write(pub_key)
        
        # add public key to git
        git_cmd = """ curl -H "Authorization: token {git_token}" --data '{{"title":"node0","key":"{ssh_pub}"}}' https://api.github.com/user/keys """
        formatted_git_cmd = git_cmd.format(git_token=values_yaml["creds"]["gitToken"], ssh_pub=pub_key)
        self.conn.run(formatted_git_cmd)
        
        # create kubernetes secret using ssh key and git server as known host
        kube_git_secret = "kubectl create secret generic git-creds --from-file=ssh=$HOME/.ssh/id_rsa --from-file=known_hosts=/tmp/known_hosts"
        self.conn.run(kube_git_secret)

        # login to docker using tokens
        docker_cmd = "sudo docker login -u {} -p {}".format(values_yaml["creds"]["dockerUser"], values_yaml["creds"]["dockerToken"])
        docker_cmd2 = "sudo chmod -R 777 {}/.docker".format(home)
        self.conn.sudo(docker_cmd)
        self.conn.sudo(docker_cmd2)

        # create docker secret
        docker_secret_cmd = "kubectl create secret generic regcred --from-file=.dockerconfigjson=$HOME/.docker/config.json --type=kubernetes.io/dockerconfigjson"
        self.conn.run(docker_secret_cmd)

        self.conn.run("mkdir {}/cerebro-repo".format(home))
        self.conn.run("mkdir {}/user-repo".format(home))

    def add_jupyter_meta(self):
        from kubernetes import client, config

        with open('{}/values.yaml'.format(self.root_path), 'r') as yamlfile:
            values_yaml = yaml.safe_load(yamlfile)
        users_port = values_yaml["controller"]["jupyterUserPort"]

        controller = get_pod_names(self.kube_namespace)["controller"]

        jyp_check_cmd = "kubectl exec -t {} -- ls".format(controller)
        ls_out = self.conn.run(jyp_check_cmd).stdout
        while "JUPYTER_TOKEN" not in ls_out:
            time.sleep(1)
            ls_out = self.conn.run(jyp_check_cmd, hide=True).stdout
            
        cmd = "kubectl exec -t {} -- cat JUPYTER_TOKEN".format(controller)
        jupyter_token = self.conn.run(cmd).stdout
        
        namespace = "cerebro"
        label = "serviceApp=jupyter"
        config.load_kube_config()
        v1 = client.CoreV1Api()
        names = []
        jupyter_svc = v1.list_namespaced_service(
            namespace, label_selector=label, watch=False)
        jupyter_svc = jupyter_svc.items[0]
        node_port = jupyter_svc.spec.ports[0].node_port

        user_pf_command = "ssh -N -L {}:localhost:{} {}@cloudlab_host_name".format(
            users_port, node_port, self.username)
        s = "Run this command on your local machine to access Jupyter Notebook : \n{}".format(
            user_pf_command) + "\n" + "http://localhost:{}/?token={}".format(users_port, jupyter_token)
        
        print(s)
        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/jupyter_command.txt".format(self.root_path), "w+") as f:
            f.write(s)
            
            
        # add tensorboard port
        
        namespace = "cerebro"
        label = "serviceApp=tensorboard"
        config.load_kube_config()
        v1 = client.CoreV1Api()
        names = []
        tensorboard_svc = v1.list_namespaced_service(
            namespace, label_selector=label, watch=False)
        tensorboard_svc = tensorboard_svc.items[0]
        node_port = tensorboard_svc.spec.ports[0].node_port

        user_pf_command = "ssh -N -L {}:localhost:{} {}@cloudlab_host_name".format(
            users_port, node_port, self.username)
        s = "Run this command on your local machine to access Tensorboard Dashboard : \n{}".format(
            user_pf_command) + "\n" + "http://localhost:{}".format(users_port)
        
        print(s)
        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/tensorboard_command.txt".format(self.root_path), "w+") as f:
            f.write(s)

    def install_controller(self):
        from kubernetes import client, config

        cmds = [
            "helm create ~/cerebro-controller",
            "rm -rf ~/cerebro-controller/templates/*",
            "cp {}/controller/* ~/cerebro-controller/templates/".format(
                self.root_path),
            "cp {}/values.yaml ~/cerebro-controller/values.yaml".format(
                self.root_path),
            "helm install --namespace=cerebro controller ~/cerebro-controller/"
        ]

        for cmd in cmds:
            time.sleep(1)
            self.conn.run(cmd)

        label = "app=cerebro-controller"

        while not check_pod_status(label, self.kube_namespace):
            time.sleep(1)

        time.sleep(5)

        # add all permissions to repos
        cmd1 = "sudo chmod -R 777 ~/cerebro-repo/cerebro-kube"
        cmd2 = "sudo chmod -R 777 ~/user-repo/*"
        self.conn.sudo(cmd1)
        self.conn.sudo(cmd2)

        self.add_dask_meta()
        self.add_jupyter_meta()

        print("Done")

    def install_worker(self):
        from kubernetes import client, config

        self.s.run("mkdir -p ~/user-repo")
        self.s.run("mkdir -p ~/cerebro-repo")

        cmds = [
            "helm create ~/cerebro-worker-etl".format(self.root_path),
            "rm -rf ~/cerebro-worker-etl/templates/*".format(self.root_path),
            "cp {}/worker-etl/* ~/cerebro-worker-etl/templates/".format(self.root_path),
            "cp {}/values.yaml ~/cerebro-worker-etl/values.yaml".format(self.root_path)
        ]
        c = "helm install --namespace={n} worker-etl-{id} ~/cerebro-worker-etl --set workerID={id}"

        # node0 for controller + metrics
        # node1 for nfs
        # all other nodes for workers
        for i in range(1, self.w - 1):
            cmds.append(
                c.format(id=i, n=self.kube_namespace))

        for cmd in cmds:
            time.sleep(0.5)
            self.conn.run(cmd)

        label = "type=cerebro-worker-etl"
        while not check_pod_status(label, self.kube_namespace):
            time.sleep(1)

        print("ETL-Workers created successfully")

        cmds = [
            "helm create ~/cerebro-worker-mop".format(self.root_path),
            "rm -rf ~/cerebro-worker-mop/templates/*".format(self.root_path),
            "cp {}/worker-mop/* ~/cerebro-worker-mop/templates/".format(self.root_path),
            "cp {}/values.yaml ~/cerebro-worker-mop/values.yaml".format(self.root_path)
        ]
        c = "helm install --namespace={n} worker-mop-{id} ~/cerebro-worker-mop --set workerID={id}"

        for i in range(1, self.w - 1):
            cmds.append(
                c.format(id=i, n=self.kube_namespace))

        for cmd in cmds:
            time.sleep(0.5)
            self.conn.run(cmd)

        label = "type=cerebro-worker-mop"
        while not check_pod_status(label, self.kube_namespace):
            time.sleep(1)
        
        # write worker_ips to references
        cmd = "kubectl get service -o json -l {}".format(label)
        out = self.conn.run(cmd)
        output = json.loads(out.stdout)
        ips = []
        for pod in output["items"]:
            ip = "http://" + str(pod["spec"]["clusterIPs"][0]) + ":7777"
            ips.append(ip)
        
        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/ml_worker_ips.txt", "w+") as f:
            f.write(
                "Model Hopper Worker IPs:\n")
            f.write("\n".join(ips))
            f.write("\n" + str(ips))
        print("Created the workers")

    def add_dask_meta(self):
        from kubernetes import client, config

        values_yaml = None
        with open('{}/values.yaml'.format(self.root_path), 'r') as yamlfile:
            values_yaml = yaml.safe_load(yamlfile)

        # write scheduler IP to yaml file
        config.load_kube_config()
        v1 = client.CoreV1Api()
        svc_name = "cerebro-controller-service"
        svc = v1.read_namespaced_service(
            namespace=self.kube_namespace, name=svc_name)
        controller_ip = svc.spec.cluster_ip

        values_yaml["workerETL"]["schedulerIP"] = controller_ip
        
        with open('{}/values.yaml'.format(self.root_path), 'w') as yamlfile:
            yaml.safe_dump(values_yaml, yamlfile)

        print("cerebro-controller's IP: ", controller_ip)
        
        # add Dask Dashboard info to references
        
        namespace = "cerebro"
        label = "serviceApp=dask"
        config.load_kube_config()
        v1 = client.CoreV1Api()
        jupyter_svc = v1.list_namespaced_service(
            namespace, label_selector=label, watch=False)
        jupyter_svc = jupyter_svc.items[0]
        node_port = jupyter_svc.spec.ports[0].node_port
        
        daskDashboardPort = values_yaml["controller"]["daskDashboardPort"]

        user_pf_command = "ssh -N -L {}:localhost:{} {}@cloudlab_host_name".format(
            daskDashboardPort, node_port, self.username)
        s = "Run this command on your local machine to access the Dask Dashboard : \n{}".format(
            user_pf_command) + "\n" + "http://localhost:{}".format(daskDashboardPort)
        
        print(s)
        home = str(Path.home())
        Path(home + "/reference").mkdir(parents=True, exist_ok=True)
        with open(home + "/reference/dask_dashboard_command.txt".format(self.root_path), "w+") as f:
            f.write(s)

    def copy_module(self):
        from kubernetes import client, config

        # ignore controller as it's replicated to node0
        all_pods = get_pod_names(self.kube_namespace)
        pods = all_pods["etl_workers"] + all_pods["mop_workers"]

        self.conn.run(
            "cd ~/cerebro-repo/cerebro-kube && zip cerebro.zip cerebro/*".format(self.root_path))

        for pod in pods:
            self.conn.run(
                "kubectl exec -t {} -- rm -rf /cerebro-repo/cerebro-kube/cerebro".format(pod))

        cmds = [
            "kubectl cp ~/cerebro-repo/cerebro-kube/cerebro.zip {}:/cerebro-repo/cerebro-kube/",
            "kubectl cp ~/cerebro-repo/cerebro-kube/requirements.txt {}:/cerebro-repo/cerebro-kube/",
            "kubectl cp ~/cerebro-repo/cerebro-kube/setup.py {}:/cerebro-repo/cerebro-kube/"
        ]
        for pod in pods:
            for cmd in cmds:
                self.conn.run(cmd.format(pod))

        for pod in pods:
            self.conn.run(
                "kubectl exec -t {} -- unzip -o /cerebro-repo/cerebro-kube/cerebro.zip".format(pod))
            self.conn.run(
                "kubectl exec -t {} -- python3 /cerebro-repo/cerebro-kube/setup.py install --user".format(pod))

        self.conn.run("rm ~/cerebro-repo/cerebro-kube/cerebro.zip")
        self.conn.run("cd ~/cerebro-repo/cerebro-kube && pip install -v -e .")

        # install pycoco module
        all_pods = get_pod_names(self.kube_namespace)
        pods = [all_pods["controller"]] + all_pods["etl_workers"] + all_pods["mop_workers"]

        cmd = "kubectl exec -t {} -- pip install -v -e /user-repo/coco-mop"
        for pod in pods:
            self.conn.run(cmd.format(pod))

    def download_coco(self):
        from fabric2 import ThreadingGroup, Connection

        host = "node1"

        user = self.username
        pem_path = "/users/{}/cloudlab.pem".format(self.username)
        connect_kwargs = {"key_filename": pem_path}
        conn = Connection(host, user=user, connect_kwargs=connect_kwargs)

        cmds = [
            # "mkdir -p /mnt/temp/coco",
            # "wget -P /mnt/temp/ http://images.cocodataset.org/zips/train2017.zip",
            # "wget -P /mnt/temp/ http://images.cocodataset.org/zips/val2017.zip",
            # "wget -P /mnt/temp/ http://images.cocodataset.org/zips/test2017.zip",
            # "wget -P /mnt/temp/ http://images.cocodataset.org/annotations/annotations_trainval2017.zip",
            # "unzip -d /mnt/temp/coco/ /mnt/temp/annotations_trainval2017.zip",
            # "unzip -d /mnt/temp/coco/ /mnt/temp/val2017.zip",
            # "unzip -d /mnt/temp/coco/ /mnt/temp/train2017.zip",
            
            "rm -rf /mnt/nfs/cerebro-data/coco",
            "mkdir -p /mnt/nfs/cerebro-data/coco",
            "cp -r /mnt/temp/coco /mnt/nfs/cerebro-data/"
        ]

        for cmd in cmds:
            conn.sudo(cmd)

        conn.close()

        # ssh-keygen
        # cat ~/.ssh/id_rsa.pub
        # nano ~/.ssh/authorized_keys
        # chmod 777 .

    def delete_worker_data(self):
        from fabric2 import ThreadingGroup, Connection

        user = self.username
        pem_path = "/users/{}/cloudlab.pem".format(self.username)
        connect_kwargs = {"key_filename": pem_path}

        for i in range(2, self.w):
            host = "node" + str(i)
            conn = Connection(host, user=user, connect_kwargs=connect_kwargs)
            try:
                conn.sudo("rm -rf /mnt/cerebro_data_storage_worker/*")
                conn.close()
            except:
                print("Failed to delete in worker" + str(i-1))

    def testing(self):
        pass
        
    def close(self):
        self.s.close()
        self.conn.close()

    def clean_up(self):
        home = str(Path.home())
        
        def run_cmd(handle, cmd, err_msg):
            try:
                handle(cmd)
                print("Ran command '{}' ".format(cmd))
            except Exception as e:
                print(err_msg, str(e))

        # clean up controller
        home = str(Path.home())
        cmd1 = "helm delete controller"
        cmd2 = "sudo rm -rf {home}/cerebro-controller {home}/cerebro-repo {home}/user-repo".format(home=home)
        run_cmd(self.conn.run, cmd1, "Cleaning up controller failed: ")
        run_cmd(self.conn.run, cmd2, "Cleaning up controller failed: ")
        print("Controller clean up done!")

        # clean up workers
        s = " ".join(["worker-etl-"+str(i) for i in range(1, self.w-1)])
        cmd1 = "helm delete " + s
        s = " ".join(["worker-mop-"+str(i) for i in range(1, self.w-1)])
        cmd2 = "helm delete " + s
        cmd3 = "sudo rm -rf {}/cerebro-worker-etl".format(home)
        cmd4 = "sudo rm -rf {}/cerebro-worker-mop".format(home)
        cmd5 = "sudo rm -rf /mnt/cerebro_data_storage_worker/coco/"
        cmd6 = "sudo rm -rf /users/prsridha/cerebro-repo"
        cmd7 = "sudo rm -rf /users/prsridha/user-repo"

        run_cmd(self.conn.run, cmd1, "Cleaning up workers failed: ")
        run_cmd(self.conn.run, cmd2, "Cleaning up workers failed: ")
        run_cmd(self.conn.sudo, cmd3, "Cleaning up workers failed: ")
        run_cmd(self.conn.sudo, cmd4, "Cleaning up workers failed: ")
        run_cmd(self.s.sudo, cmd5, "Cleaning up workers failed: ")
        run_cmd(self.s.sudo, cmd6, "Cleaning up workers failed: ")
        run_cmd(self.s.sudo, cmd7, "Cleaning up workers failed: ")
        print("Worker clean up done!")
        
        # post clean up
        cmd1 = "mkdir -p {}/cerebro-repo".format(home)
        cmd2 = "mkdir -p {}/user-repo".format(home)
        run_cmd(self.conn.run, cmd1, "Post clean up failed: ")
        run_cmd(self.conn.run, cmd2, "Post clean up failed: ")
        print("Post clean up done!")

def main():
    root_path = "/users/{}/cerebro-kube-setup"

    kube_params = {
        "kube_namespace": "cerebro",
        "cerebro_checkpoint_hostpath": "/exports/cerebro-checkpoint",
        "cerebro_config_hostpath": "/exports/cerebro-config",
        "cerebro_data_hostpath": "/exports/cerebro-data",
        "cerebro_worker_data_hostpath": "/exports/cerebro-data-{}"
    }
    
    cmd = "cat /etc/hosts | grep node | wc -l"
    out = subprocess.getoutput(cmd)
    num_workers = int(out)

    parser = ArgumentParser()
    parser.add_argument("cmd", help="install dependencies")

    args = parser.parse_args()

    installer = CerebroInstaller(root_path, num_workers, kube_params)
    if args.cmd == "installkube":
        installer.init()
        installer.kubernetes_install()
    else:
        installer.init_fabric()
        if args.cmd == "joinworkers":
            installer.kubernetes_join_workers()
            time.sleep(5)
        elif args.cmd == "installcerebro":
            installer.init_cerebro_kube()
            time.sleep(3)
            installer.install_controller()
            installer.install_worker()
        elif args.cmd == "downloadcoco":
            installer.download_coco()
        elif args.cmd == "metricsmonitor":
            installer.install_metrics_monitor()
        elif args.cmd == "installnfs":
            installer.install_nfs()
        elif args.cmd == "installcontroller":
            installer.install_controller()
        elif args.cmd == "installworkers":
            installer.install_worker()
        elif args.cmd == "daskmeta":
            installer.add_dask_meta()
        elif args.cmd == "jupytermeta":
            installer.add_jupyter_meta()
        elif args.cmd == "copymodule":
            installer.copy_module()
        elif args.cmd == "delworkerdata":
            installer.delete_worker_data()
        elif args.cmd == "testing":
            installer.testing()
        elif args.cmd == "cleanup":
            installer.clean_up()
        else:
            print("Wrong option")

    installer.close()


main()

# OTHER COMMANDS
# cat >> ~/.inputrc <<'EOF'
# "\e[A": history-search-backward
# "\e[B": history-search-forward
# EOF
# bind -f  ~/.inputrc

# source <(kubectl completion bash)
# echo "source <(kubectl completion bash)" >> ~/.bashrc
# alias k=kubectl
# complete -F __start_kubectl k
