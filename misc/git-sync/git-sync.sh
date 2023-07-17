#!/bin/bash

# create .ssh dir under home and copy the git credentials from /etc/git-secret
mkdir $HOME/.ssh
cp /etc/git-secret/* $HOME/.ssh/
mv $HOME/.ssh/ssh $HOME/.ssh/id_rsa.git
touch $HOME/.ssh/config
ssh-keyscan -t rsa $GIT_SYNC_SERVER >> ~/.ssh/known_hosts

# create git config file in .ssh
echo "
Host $GIT_SYNC_SERVER
    Hostname $GIT_SYNC_SERVER
    IdentityFile $HOME/.ssh/id_rsa.git
    IdentitiesOnly yes
" > $HOME/.ssh/config

# clone the repo in the required directory
mkdir -p $GIT_SYNC_ROOT
cd $GIT_SYNC_ROOT
if [ -z "${GIT_SYNC_BRANCH}" ]; then
   git clone $GIT_SYNC_REPO $GIT_SYNC_DIR
else
   git clone $GIT_SYNC_REPO -b $GIT_SYNC_BRANCH $GIT_SYNC_DIR
fi

# check status of git clone command
if [ $? -eq 0 ]; then
  echo "Successfully cloned the repository."
else
  echo "An error occurred while running git clone."
fi