#!/bin/bash

JUPYTER_TOKEN=$(openssl rand -hex 16)
echo $JUPYTER_TOKEN > JUPYTER_TOKEN
echo $JUPYTER_TOKEN

# start notebook in user-repo dir
jupyter notebook --generate-config
sed -i "448s/.*/c.NotebookApp.notebook_dir = '\/user-repo'/" /root/.jupyter/jupyter_notebook_config.py

jupyter notebook --NotebookApp.token=$JUPYTER_TOKEN --NotebookApp.password=$JUPYTER_TOKEN --ip 0.0.0.0 --allow-root --no-browser