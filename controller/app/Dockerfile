FROM python:3.7

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano

RUN git clone https://github.com/prsridha/cerebro-kube.git
WORKDIR cerebro-kube
ADD ./run_jupyter.sh .
RUN pip install -r requirements.txt
RUN python3 setup.py install --user

ENTRYPOINT ["tail", "-f", "/dev/null"]