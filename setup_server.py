import flask
import subprocess

app = flask.Flask(__name__)

def outer_inner(cmd):
    def inner():
        proc = subprocess.Popen(
            [cmd],
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        for line in iter(proc.stdout.readline, ''):
            s = str(line.rstrip().decode("utf-8"))
            if s == "":
                break
            yield s + '<br>\n'
    return inner

@app.route('/install-kube')
def install_kube():
    cmd = "python3 setup.py installkube"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/join-workers')
def join_workers():
    cmd = "python3 setup.py joinworkers"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/install-cerebro')
def install_cerebro():
    cmd = "python3 setup.py installcerebro"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/metrics-monitor')
def metrics_monitor():
    cmd = "python3 setup.py metricsmonitor"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/install-controller')
def install_controller():
    cmd = "python3 setup.py installcontroller"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/install-worker')
def install_worker():
    cmd = "python3 setup.py installworker"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/stop-etl-workers')
def stop_etl_workers():
    cmd = "python3 setup.py stopetlworkers"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/cleanup')
def cleanup():
    cmd = "python3 setup.py cleanup"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route('/testing')
def testing():
    cmd = "python3 setup.py testing"
    inner = outer_inner(cmd)
    return flask.Response(inner(), mimetype='text/html')  # text/html is required for most browsers to show th$

@app.route("/working")
def working():
    return "all okay boss"

def main():
    flask_port = 5000
    username = subprocess.run(
            "whoami", capture_output=True, text=True).stdout.strip("\n")
    port_forward = "ssh -N -L {}:localhost:{} {}@cloudlab_host_name".format(
            flask_port, flask_port, username)
    
    print("Run this command to port forward betweek cloulab and local machine")
    print(port_forward)
    print("\n\n")
    app.run(debug=False, port=flask_port, host='0.0.0.0')

main()