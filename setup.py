import os
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

    label = "type=cerebro-worker"
    pods_list = v1.list_namespaced_pod(
        namespace, label_selector=label, watch=False)
    workers = [i.metadata.name for i in pods_list.items]

    return [controller] + workers

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

    def runbg(self, cmd, sockname="dtach"):
        return self.conn.run('dtach -n `mktemp -u /tmp/%s.XXXX` %s' % (sockname, cmd))

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
        self.s.run(
            "git clone https://github.com/prsridha/cerebro-kube-setup.git")

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
            "helm install --namespace=prom-metrics prom prometheus-community/kube-prometheus-stack --set nodeSelector.'cerebro/nodename'={}".format(
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
        with open(home + "/metrics_monitor_credentials.txt", "w+") as f:
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

    def start_jupyter(self):
        users_port = 9999
        kube_port = 23456

        from kubernetes import client, config

        controller = get_pod_names(self.kube_namespace)[0]

        self.conn.run("kubectl cp {}/misc/run_jupyter.sh {}:/home/cerebro-kube/".format(self.root_path, controller))
        self.runbg(
            "kubectl exec -t {} -- /bin/bash run_jupyter.sh".format(controller))

        cmd = "kubectl exec -t {} -- cat JUPYTER_TOKEN".format(controller)
        jupyter_token = self.conn.run(cmd).stdout

        cmd = "kubectl port-forward --address 127.0.0.1 {} {}:8888 &".format(
            controller, kube_port)
        out = self.runbg(cmd)
        user_pf_command = "ssh -N -L {}:localhost:{} {}@<CloudLab host name>".format(
            users_port, kube_port, self.username)
        s = "Run this command on your local machine to access Jupyter Notebook : \n{}".format(
            user_pf_command) + "\n" + "http://localhost:{}/?token={}".format(users_port, jupyter_token)
        
        print(s)
        home = str(Path.home())
        with open(home + "/jupyter_command.txt".format(self.root_path), "w+") as f:
            f.write(s)

    def install_controller(self):
        from kubernetes import client, config

        cmds = [
            "helm create ~/cerebro-controller",
            "rm -rf ~/cerebro-controller/templates/*",
            "cp {}/controller/config/* ~/cerebro-controller/templates/".format(
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

        controller = get_pod_names(self.kube_namespace)[0]
        
        cmds = [
            "git clone https://github.com/prsridha/cerebro-kube.git /home/cerebro-kube",
            "pip install -r requirements.txt",
            "python3 setup.py install --user"
        ]
        for cmd in cmds:
            self.conn.run("kubectl exec -t {} -- {}".format(controller, cmd))

        # grant permission to cerebro-kube folder
        self.conn.sudo("chmod -R 777 ~/cerebro-kube".format(self.root_path))

        self.start_jupyter()

        print("Done")

    def install_worker(self):
        from kubernetes import client, config

        cmds = [
            "helm create ~/cerebro-worker".format(self.root_path),
            "rm -rf ~/cerebro-worker/templates/*".format(self.root_path),
            "cp {}/worker/config/* ~/cerebro-worker/templates/".format(self.root_path),
            "cp {}/values.yaml ~/cerebro-worker/values.yaml".format(self.root_path)
        ]
        c = "helm install --namespace={n} worker{id} ~/cerebro-worker --set workerID={id}"

        # node0 for nfs + metrics
        # node1 for controller
        # all other nodes for workers
        for i in range(1, self.w - 1):
            cmds.append(
                c.format(id=i, n=self.kube_namespace))

        for cmd in cmds:
            time.sleep(0.5)
            self.conn.run(cmd)

        label = "type=cerebro-worker"

        while not check_pod_status(label, self.kube_namespace):
            time.sleep(1)


    def run_dask(self):
        from kubernetes import client, config

        num_dask_processes = 16

        pods = get_pod_names(self.kube_namespace)
        controller = pods[0]
        workers = pods[1:]

        # for pod in pods:
        #     self.conn.run(
        #         "kubectl exec -t {} -- pip install --upgrade click==8.0.2".format(pod))

        scheduler_cmd = "kubectl exec -t {} -- dask-scheduler --host=0.0.0.0 &".format(
            controller)
        out = self.runbg(scheduler_cmd)

        config.load_kube_config()
        v1 = client.CoreV1Api()
        svc_name = "cerebro-controller-service"
        svc = v1.read_namespaced_service(
            namespace=self.kube_namespace, name=svc_name)
        controller_ip = svc.spec.cluster_ip

        print(controller_ip)
        worker_cmd = "kubectl exec -t {} -- dask-worker tcp://{}:8786 --nworkers {} --name {} &"
        for worker in workers:
            name = worker.split("-")[2]
            self.runbg(worker_cmd.format(worker, controller_ip, num_dask_processes, name))

        print("cerebro-controller's IP: ", controller_ip)

    def stop_jupyter(self):
        from kubernetes import client, config

        pods = get_pod_names(self.kube_namespace)
        controller = pods[0]

        out = self.conn.run(
            "kubectl exec -t {} -- ps -ef | grep jupyter-notebook".format(controller))
        notebook_pid = out.stdout.split()[1]
        print(notebook_pid)

        try:
            self.conn.run(
                "kubectl exec -t {} -- kill -9 {} || true".format(controller, notebook_pid))
            print("Killed Jupyter Notebook: {}".format(notebook_pid))
        except Exception as e:
            print("Couldn't kill jupyter in controller: ", str(e))

    def stop_dask(self):
        from kubernetes import client, config

        pods = get_pod_names(self.kube_namespace)
        controller = pods[0]
        workers = pods[1:]
        
        try: 
            out = self.conn.run(
                "kubectl exec -t {} -- ps -ef | grep dask-scheduler".format(controller))
            scheduler_pid = out.stdout.split()[1]
            self.conn.run(
                "kubectl exec -t {} -- kill {} || true".format(controller, scheduler_pid))
            print("Killed Dask Scheduler: {}".format(scheduler_pid))
        except Exception as e:
            print("Couldn't kill dask in controller: ", str(e))

        #TODO: fix this 
        for worker in workers:
            try:
                out = self.conn.run(
                    "kubectl exec -t {} -- ps -ef | grep dask-worker".format(worker))
                worker_pid = out.stdout.split()[1]
                self.conn.run(
                    "kubectl exec -t {} -- kill {} || true".format(controller, worker_pid))
                print("Killed Dask in Worker: {}".format(worker_pid))
            except Exception as e:
                print("Couldn't kill dask in {}: {}".format(worker, str(e)))

    def copy_module(self):
        from kubernetes import client, config

        # ignore controller as it's replicated to node0
        pods = get_pod_names(self.kube_namespace)[1:]

        # self.conn.run("rm -rf ~/cerebro-kube")
        # self.conn.run("cd ~ && git clone https://github.com/prsridha/cerebro-kube.git")

        self.conn.run(
            "cd ~/cerebro-kube/cerebro-kube && zip cerebro.zip cerebro/*".format(self.root_path))

        for pod in pods:
            self.conn.run(
                "kubectl exec -t {} -- rm -rf /cerebro-kube/cerebro".format(pod))

        cmds = [
            "kubectl cp ~/cerebro-kube/cerebro-kube/cerebro.zip {}:/cerebro-kube/",
            "kubectl cp ~/cerebro-kube/cerebro-kube/requirements.txt {}:/cerebro-kube/",
            "kubectl cp ~/cerebro-kube/cerebro-kube/setup.py {}:/cerebro-kube/"
        ]
        for pod in pods:
            for cmd in cmds:
                self.conn.run(cmd.format(pod))

        for pod in pods:
            self.conn.run(
                "kubectl exec -t {} -- unzip -o /cerebro-kube/cerebro.zip".format(pod))
            self.conn.run(
                "kubectl exec -t {} -- python3 /cerebro-kube/setup.py install --user".format(pod))

        self.conn.run("rm ~/cerebro-kube/cerebro-kube/cerebro.zip")

        #TODO: need to check dask worker numbers
        self.stop_dask()
        self.stop_jupyter()
        time.sleep(3) 
        # self.run_dask()
        # self.start_jupyter()
        # time.sleep(5)

    def download_coco(self):
        from fabric2 import ThreadingGroup, Connection

        host = "node1"

        user = self.username
        pem_path = "/users/{}/cloudlab.pem".format(self.username)
        connect_kwargs = {"key_filename": pem_path}
        conn = Connection(host, user=user, connect_kwargs=connect_kwargs)

        cmds = [
            "wget -P /mnt/nfs/cerebro-data/ http://images.cocodataset.org/zips/val2014.zip",
            "wget -P /mnt/nfs/cerebro-data/ http://images.cocodataset.org/annotations/annotations_trainval2014.zip",
            "mkdir /mnt/nfs/cerebro-data/coco",
            "unzip -d /mnt/nfs/cerebro-data/coco/ /mnt/nfs/cerebro-data/annotations_trainval2014.zip",
            "unzip -d /mnt/nfs/cerebro-data/coco/ /mnt/nfs/cerebro-data/val2014.zip"
        ]

        for cmd in cmds:
            conn.sudo(cmd)

        conn.close()

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
        self.s.run(
            "rm -rf cerebro-kube")

    def close(self):
        self.s.close()
        self.conn.close()


def main():
    root_path = "/users/{}/cerebro-kube-setup"

    kube_params = {
        "kube_namespace": "cerebro",
        "cerebro_checkpoint_hostpath": "/exports/cerebro-checkpoint",
        "cerebro_config_hostpath": "/exports/cerebro-config",
        "cerebro_data_hostpath": "/exports/cerebro-data",
        "cerebro_worker_data_hostpath": "/exports/cerebro-data-{}"
    }

    parser = ArgumentParser()
    parser.add_argument("cmd", help="install dependencies")
    parser.add_argument("-w", "--workers", dest="workers", type=int, required=True,
                        help="number of workers")

    args = parser.parse_args()

    installer = CerebroInstaller(root_path, args.workers, kube_params)
    if args.cmd == "installkube":
        installer.init()
        installer.kubernetes_install()
    else:
        installer.init_fabric()
        if args.cmd == "joinworkers":
            installer.kubernetes_join_workers()
            time.sleep(5)
            installer.init_cerebro_kube()
        elif args.cmd == "installcerebro":
            installer.install_controller()
            time.sleep(1)
            installer.install_worker()
            time.sleep(1)
            installer.run_dask()
        elif args.cmd == "downloadcoco":
            installer.download_coco()

        elif args.cmd == "installcontroller":
            installer.install_controller()
        elif args.cmd == "installworkers":
            installer.install_worker()
        elif args.cmd == "rundask":
            installer.run_dask()
        elif args.cmd == "startjupyter":
            installer.start_jupyter()
        elif args.cmd == "copymodule":
            installer.copy_module()
        elif args.cmd == "stopdask":
            installer.stop_dask()
        elif args.cmd == "stopjupyter":
            installer.stop_jupyter()
        elif args.cmd == "delworkerdata":
            installer.delete_worker_data()
        elif args.cmd == "testing":
            installer.testing()
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
