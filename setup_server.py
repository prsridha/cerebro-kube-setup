import flask
from flask import jsonify
import subprocess

app = flask.Flask(__name__)

def stop_etl_workers():
    cmd = "cat /etc/hosts | grep node | wc -l"
    out = subprocess.getoutput(cmd)
    w = int(out)
    
    s = ["cerebro-worker-etl-" + str(i) for i in range(1, w-1)]
    cmd = "helm delete " + s
    out = subprocess.run(
            cmd, capture_output=True, text=True).stdout.strip("\n")
    return out

@app.route('/stop-etl-workers')
def stop_etl_workers_serve():
    out = stop_etl_workers()
    return jsonify({
        'status': True,
        'output': out
    })

@app.route("/status")
def working():
    return True

def main():
    flask_port = 5000
    app.run(debug=False, port=flask_port, host='0.0.0.0')

main()