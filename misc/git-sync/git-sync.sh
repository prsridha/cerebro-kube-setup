#!/bin/bash

# create .ssh dir under home and copy the git credentials from /etc/git-secret
mkdir $HOME/.ssh
cp /etc/git-secret/* $HOME/.ssh/
mv $HOME/.ssh/ssh $HOME/.ssh/id_rsa.git
touch $HOME/.ssh/config

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
if [ -z "$(ls -A $GIT_SYNC_ROOT)" ]; then
   git clone $GIT_SYNC_REPO
else
   echo "Directory not empty, skipping git clone"
fi

# run init.sh to initialize any repo-specific stuff
cd */
if [ -f init.sh ]; then
    /bin/bash init.sh
else
    echo "init.sh not found, skipping"
fi