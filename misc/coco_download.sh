# downloads coco dataset

cd /mydata/nfs/cerebro-data/
wget http://images.cocodataset.org/zips/val2014.zip
wget http://images.cocodataset.org/annotations/annotations_trainval2014.zip
mkdir coco
unzip -d ./coco/ annotations_trainval2014.zip
unzip -d ./coco/ val2014.zip