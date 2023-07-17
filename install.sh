#!/bin/bash
sudo apt update

# install Linux dependencies
sudo apt install -y jq
sudo apt install -y unzip
sudo apt install -y python3-pip
pip3 install pyopenssl --upgrade

# install requirements
pip3 install -r requirements.txt

# call oneTime
python3 setup-aws.py oneTime