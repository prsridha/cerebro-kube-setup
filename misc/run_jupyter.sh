#!/bin/bash

JUPYTER_TOKEN=$(openssl rand -hex 16)
echo $JUPYTER_TOKEN > JUPYTER_TOKEN
echo $JUPYTER_TOKEN
jupyter notebook --NotebookApp.token=$JUPYTER_TOKEN --NotebookApp.password=$JUPYTER_TOKEN --ip 0.0.0.0 --allow-root --no-browser