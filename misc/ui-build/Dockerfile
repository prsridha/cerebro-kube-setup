FROM ubuntu:20.04

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git
RUN apt-get install -y python3-pip

COPY requirements.txt .

# install base requirements
RUN pip install -r requirements.txt

# install angular
RUN apt install nodejs
RUN npm install -g @angular/cli