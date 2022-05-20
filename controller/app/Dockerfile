FROM python:3.7

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano

# RUN curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 > get_helm.sh
# RUN chmod 700 get_helm.sh
# RUN ./get_helm.sh

RUN git clone https://github.com/prsridha/etl-wip.git -b kubernetes
WORKDIR etl-wip
ADD ./run_jupyter.sh .
RUN pip install -r requirements.txt
RUN python3 setup.py install --user 

# RUN JUPYTER_TOKEN=$(openssl rand -hex 16) && echo $JUPYTER_TOKEN > JUPYTER_TOKEN
# CMD ["jupyter", "notebook", "--NotebookApp.token=$JUPYTER_TOKEN", "--NotebookApp.password=$JUPYTER_TOKEN", "--ip=0.0.0.0", "--allow-root", "--no-browser"]
# CMD ["/bin/bash", "run_jupyter.sh"]

ENTRYPOINT ["tail", "-f", "/dev/null"]