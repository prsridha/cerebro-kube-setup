FROM ubuntu:latest

RUN apt-get update -y
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git
RUN apt-get install -y python3
RUN apt-get install -y python3-pip

COPY git-sync.sh /tmp

ENTRYPOINT ["bash", "/tmp/git-sync.sh"]