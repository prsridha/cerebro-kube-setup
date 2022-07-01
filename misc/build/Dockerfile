FROM python:3.7

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git
RUN apt-get install -y python3-pip

COPY requirements.txt .

RUN pip install -r requirements.txt

RUN pip install --upgrade click==8.0.2