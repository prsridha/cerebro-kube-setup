FROM python:3.7

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y python3-pip
RUN pip install --upgrade click==8.0.2

RUN mkdir -p /home/cerebro-kube
WORKDIR /home/cerebro-kube

ENTRYPOINT ["tail", "-f", "/dev/null"]