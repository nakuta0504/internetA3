# git test

## receiver (外部)
```
python3 transport.py receiver --ip 0.0.0.0 --port 7000
```


## sender (mahimahi內)
### 啟動mahimahi
```
mm-delay 10 mm-link --meter-uplink --meter-uplink-delay \
--downlink-queue=infinite --uplink-queue=droptail \
--uplink-queue-args=bytes=30000 12mbps 12mbps
```
### 啟動sender
```
python3 transport.py sender --ip 192.168.1.103 --port 7000 --sendfile testfile.txt
```