FROM python:3.7

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y python3-pip
RUN pip install --upgrade click==8.0.2

RUN git clone https://github.com/prsridha/cerebro-kube.git
WORKDIR /cerebro-kube
RUN pip install -r requirements.txt
RUN python3 setup.py install --user

ENTRYPOINT ["tail", "-f", "/dev/null"]