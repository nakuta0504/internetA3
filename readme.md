# 測試用指令



## 生成測資
### 先開環境
```
source venv/bin/activate
```

```
python3 generate_bogus_text.py 10000 > testfile.txt
```

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
python3 transport.py sender --ip 10.0.0.1 --port 7000 --sendfile testfile.txt --recv_window 15000 --simloss 0
```
#### 兩個sender同時跑
```
python3 transport.py sender --ip 10.0.0.1 --port 7000 --sendfile testfile.txt --recv_window 15000 --simloss 0.1&
python3 transport.py sender --ip 10.0.0.1 --port 8000 --sendfile testfile.txt --recv_window 15000 --simloss 0.1
```


![image](https://github.com/user-attachments/assets/b4c04403-5220-403b-8bc1-1c5d2b9179df)
