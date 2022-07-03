FROM ubuntu:latest

RUN apt-get update
RUN apt-get install -y curl
RUN apt-get install -y nano
RUN apt-get install -y git

COPY git-sync.sh /tmp

ENTRYPOINT ["bash", "/tmp/git-sync.sh"]