# Your Name: 林仲威
# Your ID: B092010020

# mahimahi
# mm-delay 10 mm-link --meter-uplink --meter-uplink-delay \--downlink-queue=infinite --uplink-queue=droptail \--uplink-queue-args=bytes=30000 12mbps 12mbps

# 生成測資
# source venv/bin/activate
# python3 generate_bogus_text.py 1000000 > test_file.txt

# 啟動receiver
# python3 transport.py --ip localhost --port 7000 receiver

# 啟動sender
# python3 transport.py --ip localhost --port 7000 --sendfile test_file.txt sender --simloss 0 

import argparse
import json
from typing import Dict, List, Optional, Tuple
import random
import socket
import time

# The maximum size of the data contained within one packet
payload_size = 1200
# The maximum size of a packet including all the JSON formatting
packet_size = 1500


class Receiver:
    def __init__(self):
        # 緩衝區，用於儲存亂序封包：seq range -> 資料
        self.buffer = {}
        # 下一個預期的sequence number
        self.next_expected_seq = 0
        # 已接收的sequense number，用於生成 SACKs
        self.received_ranges = []

    def data_packet(self, seq_range: Tuple[int, int], data: str) -> Tuple[List[Tuple[int, int]], str]:
        '''
        處理接收到的資料封包。
        參數：
            seq_range: 封包的範圍，格式為 (start, end)。
            data: 封包的資料內容。
        return ：
            裝有已接收的sequence number的list與資料的字串。
        '''

        # 將seq_range拆成start_seq和end_seq
        start_seq, end_seq = seq_range

        # 驗證sequence number的有效性
        # 如果 start_seq >= end_seq，或資料長度不符合預期，則返回空字串
        if start_seq >= end_seq or len(data) != end_seq - start_seq:
            return (self.received_ranges, '')

        # 檢查是否為重複封包
        # 如果 start_seq 和 end_seq 在已接收的範圍內，則返回空字串
        for existing_range in self.received_ranges:
            existing_start, existing_end = existing_range
            if start_seq >= existing_start and end_seq <= existing_end:
                return (self.received_ranges, '')

        # 將封包儲存到buffer中
        self.buffer[(start_seq, end_seq)] = data

        # 更新 SACKs 的接收範圍
        self._update_received_ranges(start_seq, end_seq)

        # 檢查是否可以按序傳遞資料給應用程式
        app_data = ''
        while True:
            found = False
            # 尋找可以傳遞的資料
            # 如果 buffer 中的封包的start_seq等於next_expected_seq，則將資料傳遞給應用層
            for (s, e), data in list(self.buffer.items()):
                if s == self.next_expected_seq:
                    app_data += data
                    self.next_expected_seq = e
                    del self.buffer[(s, e)]
                    found = True
                    break
            if not found:
                break

        return (self.received_ranges, app_data)

    def _update_received_ranges(self, start_seq: int, end_seq: int):
        '''
        更新已接收的seq範圍，合併重疊或相鄰的範圍。
        參數：
            start_seq
            end_seq
        '''
        # 增加新範圍
        self.received_ranges.append((start_seq, end_seq))

        # 如果只有一個範圍，無需合併
        if len(self.received_ranges) == 1:
            return

        # 依照 start_seq 排序
        self.received_ranges.sort(key=lambda x: x[0])

        # 合併重疊或相鄰範圍
        merged = []
        current_start, current_end = self.received_ranges[0]
        for start, end in self.received_ranges[1:]:
            if start <= current_end:
                # 重疊或相鄰範圍，更新 current_end
                current_end = max(current_end, end)
            else:
                # 非重疊範圍，添加當前範圍並開始新範圍
                merged.append((current_start, current_end))
                current_start, current_end = start, end
        # 添加最後一個範圍
        merged.append((current_start, current_end))
        self.received_ranges = merged

    def finish(self):
        '''
        當發送方發送 FIN 封包時調用。
        檢查是否所有資料都已傳遞（用來DEBUG）。
        '''
        if self.buffer:
            print(f"DEBUG：結束時buffer仍有 {len(self.buffer)} 個封包未處理")
            for seq_range, data in self.buffer.items():
                print(f"未處理封包：{seq_range}, 資料長度：{len(data)}")


class Sender:
    def __init__(self, data_len: int):
        '''
        初始化發送方。
        參數：
            data_len: 要發送的資料總長度。
        '''
        self.data_len = data_len
        self.next_seq = 0
        # 儲存已發送的封包：一個dict，key 為封包 ID，value 為 (seq範圍, 發送時間, 是否已確認)
        self.sent_packets: Dict[int, Tuple[Tuple[int, int], float, bool]] = {}

        # 儲存已確認的bytes：一個集合，包含所有已確認的sequence number
        self.acked_bytes = set()
        self.last_acked = -1
        self.timeout_interval = 0.5
        # 用於fast retransmit：追蹤重複 SACKs
        # key 為 (start_seq, end_seq) 的 tuple，value 為該 SACK 連續出現的次數
        self.dup_acks: Dict[Tuple[int, int], int] = {}



        # congestion control 相關的
        self.cwnd = payload_size # initial congestion window size
        self.cwnd_increment = payload_size # 每次增加的大小
        self.fixed_cwnd = None

        # RTO 相關
        self.alpha = 1/64 # EWMA alpha
        self.estimated_rtt = None # 初始估計的 RTT
        self.dev_rtt = None # 初始 RTT 的偏差
        self.min_rto = 0.001 # 最小 RTO

    def timeout(self):
        '''
        timeout的時候使用，將未確認的封包標記為需要重新傳輸。
        '''
        print("sender timeout，重新傳輸未確認的封包")

        # timeoout時 cwnd 設為 payload_size
        self.cwnd = payload_size

        # 將所有未確認的封包的發送時間設置為 0，表示需要重新傳輸
        # 這裡的 self.sent_packets 是一個字典，key 為封包 ID，value 為 (seq範圍, 發送時間, 是否已確認)
        for packet_id, (seq_range, _, acked) in list(self.sent_packets.items()):
            if not acked:
                self.sent_packets[packet_id] = (seq_range, 0, False)

    def _check_duplicate_acks(self, sacks: List[Tuple[int, int]]):

        '''
        參數：
        sacks: 已確認的seq範圍list。
        檢查重複確認，根據連續相同 SACKs 觸發fast retransmit，可以處理多個 gap。
        '''
        # 將 sacks 按照start_seq 排序
        # 並檢查是否有重疊的範圍
        sorted_sacks = sorted(sacks, key=lambda x: x[0])

        # highest_contiguous 用於追蹤當前連續的最高sequence number
        highest_contiguous = -1
        
        # 檢查 sacks 是否有重疊的範圍
        # 如果有重疊的範圍，則將highest_contiguous 更新為當前的 end
        # 如果出現不連續的範圍，則跳出循環
        for start, end in sorted_sacks:
            if start <= highest_contiguous + 1:
                highest_contiguous = max(highest_contiguous, end)
            else:
                break

        
        # sacks：一個list，包含 Receiver 已接收的seq範圍，例如 [(0, 1200), (2400, 3600)]。
        # 把sacks轉換成tuple，並檢查是否已經存在於dup_acks中
        sack_key = tuple(tuple(sack) for sack in sacks)

        # self.dup_acks：一個dict，key是 sack_key，值是該 SACK 連續出現的次數。
        # 如果 sack_key 不在 dup_acks 中，則初始化為 0
        # 然後將key的值加1(重複的也一樣)
        if sack_key not in self.dup_acks:
            self.dup_acks[sack_key] = 0
        self.dup_acks[sack_key] += 1

        # 如果連續出現的次數 >= 3，則觸發fast retransmit
        if self.dup_acks[sack_key] >= 3:

            # fast retransmit 時 cwnd 減半
            self.cwnd = max(payload_size, self.cwnd // 2)
            print(f"fast retransmit triggered, cwnd reduced to {self.cwnd}")

            gaps = []
            prev_end = -1
            # 如果start_seq > prev_end，則表示有 gap
            for start, end in sorted_sacks:
                if prev_end != -1 and start > prev_end:
                    gaps.append((prev_end, start))
                prev_end = end
            # 檢查最後一個範圍是否有 gap
            if prev_end < self.next_seq:
                gaps.append((prev_end, self.next_seq))

            # 找出並重傳所有未確認的 gap 封包
            retransmitted = False
            for gap_start, gap_end in gaps:
                for packet_id, (seq_range, sent_time, acked) in list(self.sent_packets.items()):
                    # 確保 seq_range 在 gap 內，並且該封包沒被ack
                    if not acked and seq_range[0] >= gap_start and seq_range[1] <= gap_end:
                        print("*********************************************")
                        print(f"3次duplicate ack ， 啟動fast retransmit ，seq：{seq_range}")
                        print("*********************************************")
                        # 標成未確認，並將發送時間設置為 0
                        # 這樣就會在下次發送時重新傳輸
                        self.sent_packets[packet_id] = (seq_range, 0, False)
                        retransmitted = True
            # 最後將該 sack_key 從 dup_acks 中刪除
            if retransmitted:
                del self.dup_acks[sack_key]

    def ack_packet(self, sacks: List[Tuple[int, int]], packet_id: int) -> int:
        '''
        處理接收到的確認。
        參數：
            sacks: 已確認的seq範圍list。
            packet_id: 觸發此確認的封包 ID。
        返回：
            freed_bytes
        '''
        freed_bytes = 0
        for start, end in sacks:
            for seq in range(start, end):
                if seq not in self.acked_bytes:
                    self.acked_bytes.add(seq)
                    freed_bytes += 1
                    for pid, (seq_range, sent_time, acked) in self.sent_packets.items():
                        if not acked and seq_range[0] <= seq < seq_range[1]:
                            self.sent_packets[pid] = (seq_range, sent_time, True)

        # AIMD: 更新cwnd
        if freed_bytes > 0 and self.fixed_cwnd is None:  # 只有非固定 cwnd 時才更新
            self.cwnd += self.cwnd_increment
            print(f"AIMD觸發: cwnd 增加到 {self.cwnd}")


        print(f"Debug: acked_bytes size = {len(self.acked_bytes)}, expected data_len = {self.data_len}")
        seq = 0
        while seq in self.acked_bytes:
            seq += 1
        self.last_acked = seq - 1

        self._check_duplicate_acks(sacks)
        return freed_bytes

    def update_rtt(self, sample_rtt: float):
            if self.estimated_rtt is None:
                self.estimated_rtt = rtt = sample_rtt
                self.dev_rtt = 0
            else:
                self.estimated_rtt = self.alpha * sample_rtt + (1 - self.alpha) * self.estimated_rtt
                self.dev_rtt = self.alpha * abs(sample_rtt - self.estimated_rtt) + (1 - self.alpha) * self.dev_rtt

    def send(self, packet_id: int) -> Optional[Tuple[int, int]]:
        '''
        決定下一個要發送的序列範圍。
        參數：
            packet_id: 新封包的 ID。
        返回：
            要發送的seq範圍，若所有資料已發送且確認則返回 None。
        '''

        # 檢查是否所有資料都已發送且確認
        if self.next_seq >= self.data_len and len(self.acked_bytes) == self.data_len:
            print(f"Debug: All data sent and acked, next_seq={self.next_seq}, acked_bytes={len(self.acked_bytes)}")
            return None

        # 檢查是否有需要重新傳輸的封包
        for pid, (seq_range, sent_time, acked) in list(self.sent_packets.items()):
            # fast retransmit 或 timeout
            if not acked and (sent_time == 0 or time.time() - sent_time >= self.timeout_interval):
                print(f"重新傳輸sequence：{seq_range}, ID: {pid}")
                self.sent_packets[pid] = (seq_range, time.time(), False)
                return seq_range

        # 正常發送新的封包
        if self.next_seq < self.data_len:
            start_seq = self.next_seq
            # 計算要發送的end seq (+1200)
            end_seq = min(start_seq + payload_size, self.data_len)
            self.sent_packets[packet_id] = ((start_seq, end_seq), time.time(), False)
            self.next_seq = end_seq
            return (start_seq, end_seq)

        return (0, 0)
   
    def get_cwnd(self) -> int:
        return max(payload_size, self.cwnd)
    
    def get_rto(self) -> float:
        if self.estimated_rtt is None or self.dev_rtt is None:
            return 1.0 # 初始RTO是1.0 
        rto = self.estimated_rtt + 4 * self.dev_rtt
        return max(self.min_rto, rto)  # 確保RTO不小於1ms

def start_receiver(ip: str, port: int):
    receivers: Dict[str, Tuple[Receiver, Any]] = {}
    received_data = {} # 每個 addr 對應一個接收資料
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server_socket:
        server_socket.bind((ip, port))

        while True:
            print("======= Waiting =======")
            data, addr = server_socket.recvfrom(packet_size)
            if addr not in receivers:
                outfile = open(f'rcvd-{addr[0]}-{addr[1]}', 'w')
                receivers[addr] = (Receiver(), outfile)
                received_data[addr] = ''  # 初始化該來源地址的接收區
                
            received = json.loads(data.decode())
            if received["type"] == "data":
                # Format check. Real code will have much more
                # carefully designed checks to defend against
                # attacks. Can you think of ways to exploit this
                # transport layer and cause problems at the receiver?
                # This is just for fun. It is not required as part of
                # the assignment.
                assert type(received["seq"]) is list
                assert type(received["seq"][0]) is int and type(received["seq"][1]) is int
                assert type(received["payload"]) is str
                assert len(received["payload"]) <= payload_size

                # Deserialize the packet. Real transport layers use
                # more efficient and standardized ways of packing the
                # data. One option is to use protobufs (look it up)
                # instead of json. Protobufs can automatically design
                # a byte structure given the data structure. However,
                # for an internet standard, we usually want something
                # more custom and hand-designed.
                sacks, app_data = receivers[addr][0].data_packet(tuple(received["seq"]), received["payload"])
                # Note: we immediately write the data to file
                if app_data:
                    receivers[addr][1].write(app_data)
                    receivers[addr][1].flush()
                    received_data[addr] += app_data  # 累加到對應addr的接收區
                print(f"Received seq: {received['seq']}, id: {received['id']}, sending sacks: {sacks}")

                 # Send the ACK
                server_socket.sendto(json.dumps({"type": "ack", "sacks": sacks, "id": received["id"]}).encode(), addr)
            elif received["type"] == "fin":
                receivers[addr][0].finish()
                # Check if the file is received and send fin-ack

                if received_data and addr in received_data:
                    data_str = received_data[addr]
                    print("received data (summary): ", data_str[:100], "...", len(received_data))
                    print("received file is saved into: ", receivers[addr][1].name)
                    server_socket.sendto(json.dumps({"type": "fin"}).encode(), addr)
                    received_data[addr] = ''

                receivers[addr][1].close()
                del receivers[addr]
                del received_data[addr]  # 清除該來源地址的接收區
                break # 退出迴圈 
            else:
                assert False


def start_sender(ip: str, port: int, data: str, recv_window: int, simloss: float, fixed_cwnd: Optional[int] = None):
    sender = Sender(len(data))

    if fixed_cwnd is not None:
        sender.set_fixed_cwnd(fixed_cwnd)


    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        # So we can receive messages
        client_socket.connect((ip, port))
        # When waiting for packets when we call receivefrom, we
        # shouldn't wait more than 500ms
        client_socket.settimeout(0.5)


        # Number of bytes that we think are inflight. We are only
        # including payload bytes here, which is different from how
        # TCP does things
        inflight = 0
        packet_id = 0
        wait = False
        send_buf = []

        while True:
            # Get the congestion condow
            cwnd = sender.get_cwnd()


            # Do we have enough room in recv_window to send an entire
            # packet?
            if inflight + packet_size <= min(recv_window, cwnd) and not wait:
                seq = sender.send(packet_id)
                got_fin_ack = False
                if seq is None:
                    # We are done sending
                    # print("#######send_buf#########: ", len(send_buf))
                    if send_buf:
                        random.shuffle(send_buf)
                        for p in send_buf:
                            client_socket.send(p)
                        send_buf = []
                    client_socket.send('{"type": "fin"}'.encode())
                    try:
                        print("======= Final Waiting =======")
                        received = client_socket.recv(packet_size)
                        received = json.loads(received.decode())
                        if received["type"] == "ack":
                            client_socket.send('{"type": "fin"}'.encode())
                            continue
                        elif received["type"] == "fin":
                            print(f"Got FIN-ACK")
                            got_fin_ack = True
                            break
                    except socket.timeout:
                        inflight = 0
                        print("Timeout in final waiting")
                        sender.timeout()
                        continue
                    if got_fin_ack:
                        break
                    else:
                        continue

                elif seq[1] == seq[0]:
                    # No more packets to send until loss happens. Wait
                    wait = True
                    continue

                assert seq[1] - seq[0] <= payload_size
                assert seq[1] <= len(data)
                print(f"Sending seq: {seq}, id: {packet_id}, cwnd: {cwnd}, inflight: {inflight}, data length: {len(data)}")

                # Simulate random loss before sending packets
                if random.random() < simloss:
                    print("Dropped!")
                else:
                    pkt_str = json.dumps(
                        {"type": "data", "seq": seq, "id": packet_id, "payload": data[seq[0]:seq[1]]}
                    ).encode()
                   

                inflight += seq[1] - seq[0]
                packet_id += 1

            else:
                wait = False
                # Wait for ACKs
                try:
                    print("======= Waiting =======")
                    received = client_socket.recv(packet_size)
                    received = json.loads(received.decode())
                    assert received["type"] == "ack"

                    print(f"Got ACK sacks: {received['sacks']}, id: {received['id']}")
                    if random.random() < simloss:
                        print("Dropped ack!")
                        continue

                    inflight -= sender.ack_packet(received["sacks"], received["id"])
                    assert inflight >= 0
                except socket.timeout:
                    inflight = 0
                    print("Timeout")
                    sender.timeout()


def main():
    parser = argparse.ArgumentParser(description="Transport assignment")
    parser.add_argument("role", choices=["sender", "receiver"], help="Role to play: 'sender' or 'receiver'")
    parser.add_argument("--ip", type=str, required=True, help="IP address to bind/connect to")
    parser.add_argument("--port", type=int, required=True, help="Port number to bind/connect to")
    parser.add_argument("--sendfile", type=str, required=False,
                        help="If role=sender, the file that contains data to send")
    parser.add_argument("--recv_window", type=int, default=15000, help="Receive window size in bytes")
    parser.add_argument("--simloss", type=float, default=0.0,
                        help="Simulate packet loss. Provide the fraction of packets (0-1) that should be randomly dropped")
    parser.add_argument("--fixed-cwnd", type=int, default=None, help="Fixed cwnd in bytes (for testing)")


    args = parser.parse_args()

    if args.role == "receiver":
        start_receiver(args.ip, args.port)
    else:
        if args.sendfile is None:
            print("No file to send")
            return

        with open(args.sendfile, 'r', encoding='utf-8') as f:
            data = f.read()
            start_sender(args.ip, args.port, data, args.recv_window, args.simloss, args.fixed_cwnd)


if __name__ == "__main__":
    main()