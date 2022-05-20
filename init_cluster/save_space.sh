################## create more space
# sudo /usr/local/etc/emulab/mkextrafs.pl /mnt
##################

migrate_a2b (){
    dir_a="$1"
    dir_b="$2"
    sudo mkdir -p $dir_b
    sudo rsync -avr $dir_a/ $dir_b/
    sudo rm -rvf $dir_a/*
    sudo mount -o bind $dir_b/ $dir_a/
    echo "$dir_b/    $dir_a/    none    bind    0    0" | sudo tee -a /etc/fstab
}

space_saver (){
    ori_dir=$1
    new_dir=$2
    dir_b="/mnt/$new_dir"
    dir_a="$ori_dir"
    migrate_a2b $dir_a $dir_b

}

space_saver "/home" "home"
space_saver "/tmp" "tmp"
sudo chmod 1777 /tmp
space_saver "/var" "var"
sudo dpkg --configure -a