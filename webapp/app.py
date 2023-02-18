import os
import json
import time
import zipfile
import utilities
from flask import Flask, request
from kubernetes import client, config

app = Flask(__name__)

def saveFile(request, allowedExts, relativePath):
    # check if the post request has the file part
    if 'file' not in request.files:
        resp = {
            "message": "File not included",
            "status": 400
        }
        return resp
    file = request.files['file']
    
    # Empty file without a filename.
    if file.filename == '':
        resp = {
            "message": "File not included",
            "status": 400
        }
        return resp
    if file and (file.filename.split(".")[-1] in allowedExts):
        file.save(os.path.join(utilities.ROOT_PATH, relativePath))
        resp = {
            "message": "File saved",
            "status": 200
        }
        return resp
    
def createController():
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
        out = utilities.run(cmd, capture_output=False)

    print("Created Controller deployment")
    
    label = "app=cerebro-controller"

    print("Waiting for pods to start...")
    while not utilities.checkPodStatus(label):
        time.sleep(1)

    print("Done")

def createWorkers():
    valuesYaml = utilities.readValuesYAML()
    numWorkers = valuesYaml["cluster"]["numWorkers"]

    # create ETL Workers
    cmds = [
        "mkdir -p charts",
        "helm create charts/cerebro-worker-etl",
        "rm -rf charts/cerebro-worker-etl/templates/*",
        "cp worker-etl/* charts/cerebro-worker-etl/templates/",
        "cp values.yaml charts/cerebro-worker-etl/values.yaml"
    ]
    c = "helm install --namespace={n} worker-etl-{id} charts/cerebro-worker-etl --set workerID={id}"
    
    for i in range(1, numWorkers + 1):
        cmds.append(
            c.format(id=i, n="cerebro"))
    
    for cmd in cmds:
        time.sleep(0.5)
        utilities.run(cmd, capture_output=False)
    utilities.run("rm -rf charts")
    
    print("Waiting for ETL Worker start-up")
    label = "type=cerebro-worker-etl"
    while not utilities.checkPodStatus(label):
        time.sleep(1)
    
    print("ETL-Workers created successfully")
    
    # Create MOP Workers
    cmds = [
        "mkdir -p charts",
        "helm create charts/cerebro-worker-mop",
        "rm -rf charts/cerebro-worker-mop/templates/*",
        "cp worker-mop/* charts/cerebro-worker-mop/templates/",
        "cp values.yaml charts/cerebro-worker-mop/values.yaml",
    ]
    c = "helm install --namespace={n} worker-mop-{id} charts/cerebro-worker-mop --set workerID={id}"
    
    for i in range(1, numWorkers + 1):
        cmds.append(
            c.format(id=i, n="cerebro"))
    
    for cmd in cmds:
        time.sleep(0.5)
        utilities.run(cmd, capture_output=False)
    utilities.run("rm -rf charts")
    
    print("Waiting for MOP Worker start-up")
    label = "type=cerebro-worker-mop"
    while not utilities.checkPodStatus(label):
        time.sleep(1)
    
    # write worker_ips to references
    cmd = "kubectl get service -o json -l {}".format(label)
    output = json.loads(run(cmd))
    ips = []
    for pod in output["items"]:
        ip = "http://" + str(pod["spec"]["clusterIPs"][0]) + ":7777"
        ips.append(ip)

    Path("reference").mkdir(parents=True, exist_ok=True)
    with open("reference/ml_worker_ips.txt", "w+") as f:
        f.write(
            "Model Hopper Worker IPs:\n")
        f.write("\n".join(ips))
        f.write("\n\n" + json.dumps(ips))

    print("Created the workers")

def cleanUp():
    podNames = utilities.getPodNames()
    valuesYaml = utilities.readValuesYAML
    numWorkers = valuesYaml["cluster"]["numWorkers"]
    
    config.load_kube_config()
    v1 = client.CoreV1Api()
    
    # clean up Workers
    cmd1 = "kubectl exec -t {} -- bash -c 'rm -rf /cerebro_data_storage_worker/*' "
    cmd2 = "kubectl exec -t {} -- bash -c 'rm -rf /cerebro-repo/*' "
    cmd3 = "kubectl exec -t {} -- bash -c 'rm -rf /user-repo/*' "
    for pod in podNames["mop_workers"]:
        utilities.run(cmd1.format(pod), haltException=False)
        utilities.run(cmd2.format(pod), haltException=False)
        utilities.run(cmd3.format(pod), haltException=False)
    helm_etl = ["worker-etl-" + str(i) for i in range(1, numWorkers + 1)]
    cmd4 = "helm delete " + " ".join(helm_etl)
    utilities.run(cmd4, capture_output=False, haltException=False)
    
    helm_mop = ["worker-mop-" + str(i) for i in range(1, numWorkers + 1)]
    cmd5 = "helm delete " + " ".join(helm_mop)
    utilities.run(cmd5, capture_output=False, haltException=False)
    
    print("Cleaned up workers")
    
    # clean up Controller
    cmd1 = "kubectl exec -t {} -c cerebro-controller-container -- bash -c 'rm -rf /data/cerebro_data_storage/*'".format(podNames["controller"])
    cmd2 = "kubectl exec -t {} -c cerebro-controller-container -- bash -c 'rm -rf /data/cerebro_config_storage/*'".format(podNames["controller"])
    cmd3 = "kubectl exec -t {} -c cerebro-controller-container -- bash -c 'rm -rf /data/cerebro_checkpoint_storage/*'".format(podNames["controller"])
    cmd4 = "kubectl exec -t {} -c cerebro-controller-container -- bash -c 'rm -rf /data/cerebro_metrics_storage/*'".format(podNames["controller"])
    for i in [cmd1, cmd2, cmd3, cmd4]:
        utilities.run(i, haltException=False)
    cmd5 = "helm delete controller"
    utilities.run(cmd5, haltException=False)
    print("Cleaned up Controller")
    
    pods_list = v1.list_namespaced_pod("cerebro")
    while pods_list.items != []:
        time.sleep(1)
        print("Waiting for pods to shutdown...")
        pods_list = v1.list_namespaced_pod("cerebro")

    print("Done")

@app.route("/initialize", methods=["POST"])
def initialize():
    resp = saveFile(request, ["yaml"], "values.yaml")
    return resp

@app.route("/params", methods=["POST"])
def saveParams():
    valuesYaml = utilities.readValuesYAML()
    params = request.json
    path = os.path.join(utilities.ROOT_PATH, "params.json")
    
    # add numWorkers field
    params["num_workers"] = valuesYaml["cluster"]["numWorkers"]
    
    with open(path, "w") as f:
        json.dump(params, f, indent=2)
        
    resp = {
        "message": "Saved params json file",
        "status": 200
    }
    return resp

@app.route("/save-code", methods=["POST"])
def saveCode():
    # save zip file
    resp = saveFile(request, ["zip"], "code.zip")
    if resp["status"] != 200:
        return resp
    
    # extract zip file contents
    extractPath = os.path.join(utilities.ROOT_PATH, "code")
    with zipfile.ZipFile("code.zip", "r") as f:
        f.extractall(extractPath)
    
    resp = {
        "message": "Extracted and saved code zip file",
        "status": 200
    }
    return resp

@app.route("/copy-files", methods=["GET"])
def copyFilesToPods():
    codeFromPath = os.path.join(utilities.ROOT_PATH, "code")    
    paramsFromPath = os.path.join(utilities.ROOT_PATH, "params.json")
    toPath = valuesYaml["controller"]["volumes"]["userRepoMountPath"]

    pods = []
    valuesYaml = utilities.readValuesYAML()
    cmd = "kubectl cp " + {} + " {}:" + toPath
    for i in utilities.getPodNames().values():
        pods.extend(i)
    
    # copy to pods
    for pod in pods:
        utilities.run(cmd.format(codeFromPath, pod))
        utilities.run(cmd.format(paramsFromPath, pod))
    
    resp = {
        "message": "Extracted and saved code zip file",
        "status": 200
    }
    return resp

@app.route("/create-controller", methods=["GET"])
def createControllerWrapper():
    try:
        createController()
        resp = {
            "message": "Successfully created controller",
            "status": 200
        }
    except Exception as e:
        s = "Error while creating contoller: " + str(e)
        resp = {
            "message": s,
            "status": 500
        }
    return resp

@app.route("/create-workers", methods=["GET"])
def createWorkersWrapper():
    try:
        createWorkers()
        resp = {
            "message": "Successfully created Workers",
            "status": 200
        }
    except Exception as e:
        s = "Error while creating Workers: " + str(e)
        resp = {
            "message": s,
            "status": 500
        }
    return resp

@app.route("/cleanup", methods=["GET"])
def cleanUpWrapper():
    try:
        cleanUp()
        resp = {
            "message": "Successfully deleted Controller and Workers",
            "status": 200
        }
    except Exception as e:
        s = "Error while deleting Controller and  Workers: " + str(e)
        resp = {
            "message": s,
            "status": 500
        }
    return resp

@app.route("/health", methods=["GET"])
def hello_world():
    return "Hello World! This is the Cerebro backend webserver"