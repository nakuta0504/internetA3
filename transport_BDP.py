# Your Name: 林仲威
# Your ID: B092010020

# A3版本
# 移除reorder 
# 可以跑loss 0.1了

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

# BDP = product of link rate and base delay 12,000,000 bits/s × 0.02 s(20ms RTT) = 140,000 bits = 30000 bytes
BDP = 30000  # 30000 bytes
MULT = 2

class Receiver:
    def __init__(self):
        # buffer，用於儲存亂序封包：seq range -> 資料
        self.buffer = {}
        # 下一個預期的 sequence number
        self.next_expected_seq = 0
        # 已接收的 sequence number，用於生成 SACKs
        self.received_ranges = []

    def data_packet(self, seq_range: Tuple[int, int], data: str) -> Tuple[List[Tuple[int, int]], str]:
        start_seq, end_seq = seq_range

        # 驗證 sequence number
        if start_seq >= end_seq or len(data) != end_seq - start_seq:
            return (self.received_ranges, '')

        # 檢查是否為重複封包
        for existing_range in self.received_ranges:
            existing_start, existing_end = existing_range
            if start_seq >= existing_start and end_seq <= existing_end:
                return (self.received_ranges, '')

        # 將封包儲存到 buffer 中
        self.buffer[(start_seq, end_seq)] = data

        # 更新 SACKs 的接收範圍
        self._update_received_ranges(start_seq, end_seq)

        # 檢查是否可以按序傳遞資料給應用層
        app_data = ''
        while True:
            found = False
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
        self.received_ranges.append((start_seq, end_seq))
        if len(self.received_ranges) == 1:
            return

        self.received_ranges.sort(key=lambda x: x[0])
        merged = []
        current_start, current_end = self.received_ranges[0]
        for start, end in self.received_ranges[1:]:
            if start <= current_end:
                current_end = max(current_end, end)
            else:
                merged.append((current_start, current_end))
                current_start, current_end = start, end
        merged.append((current_start, current_end))
        self.received_ranges = merged

    def finish(self):
        if self.buffer:
            print(f"DEBUG：結束時 buffer 仍有 {len(self.buffer)} 個封包未處理")
            for seq_range, data in self.buffer.items():
                print(f"未處理封包：{seq_range}, 資料長度：{len(data)}")

class Sender:
    def __init__(self, data_len: int,fixed_cwnd: int = None):
        self.data_len = data_len
        self.next_seq = 0
        self.sent_packets: Dict[int, Tuple[Tuple[int, int], float, bool]] = {}
        # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
        self.acked_packet_ids = set()

        self.acked_bytes = set()
        self.last_acked = -1
        self.timeout_interval = 1.0  # 初始 RTO 為 1 秒
        self.estimated_rtt = 0.0
        self.dev_rtt = 0.0
        self.dup_acks: Dict[Tuple[Tuple[int, int], ...], int] = {}
        self.cwnd = fixed_cwnd if fixed_cwnd is not None else packet_size  # 使用固定 cwnd 或預設成 packet_size
        self.alpha = 1/64  # EWMA 參數
        self.ssthresh = 64000  # ssthresh 初始值
        self.state = "slow_start"  # 初始狀態為 slow start
        self.dupACKcount = 0 # 用於計算重複 ACK 的次數

    def timeout(self):
        print(f"Timeout in {self.state}, 進入 slow start")
        self.state = "slow_start"
        # self.ssthresh = max(packet_size, self.cwnd // 2)  # ssthresh 重置為 cwnd 的一半，但至少為一個封包大小
        # self.cwnd = packet_size  # 重置 cwnd 為一個封包大小
        # self.dupACKcount = 0  # 重置 dup ACK 計數
        print(f"新的 ssthresh: {self.ssthresh}, 新的 cwnd: {self.cwnd}")


        for packet_id, (seq_range, _, acked) in list(self.sent_packets.items()):
            if not acked:
                self.sent_packets[packet_id] = (seq_range, 0, False)

        # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
        self.acked_packet_ids = set()

    def _check_duplicate_acks(self, sacks: List[Tuple[int, int]]):
        sorted_sacks = sorted(sacks, key=lambda x: x[0])
        highest_contiguous = -1
        for start, end in sorted_sacks:
            if start <= highest_contiguous + 1:
                highest_contiguous = max(highest_contiguous, end)
            else:
                break

        sack_key = tuple(tuple(sack) for sack in sacks)
        if sack_key not in self.dup_acks:
            self.dup_acks[sack_key] = 0
        self.dup_acks[sack_key] += 1


        # 增加 dupACKcount 
        if self.dup_acks[sack_key] > 1:  # 第二個及以上的相同 SACK 視為重複
            self.dupACKcount += 1
            print(f"檢測到重複 ACK，dupACKcount 增加至 {self.dupACKcount}")

        # dupACKcount 超過 3 次，進入 fast recovery
        # if self.dupACKcount == 3:
        #     print("dupACKcount 達到 3，啟動 fast recovery")
        #     self.ssthresh = max(packet_size, self.cwnd // 2)
        #     self.cwnd = self.ssthresh + 3 * packet_size  
        #     self.state = "fast_recovery" # 進入 fast recovery
        #     self.dupACKcount = 0  # 重置 dup ACK 計數
        #     print(f"新的 ssthresh: {self.ssthresh}, 新的 cwnd: {self.cwnd}")

        # 檢查是否達到 3 次重複 ACK
        if self.dup_acks[sack_key] >= 3:

            gaps = []
            prev_end = -1

            # 重送封包
            for start, end in sorted_sacks:
                if prev_end != -1 and start > prev_end:
                    gaps.append((prev_end, start))
                prev_end = end
            if prev_end < self.next_seq:
                gaps.append((prev_end, self.next_seq))

            retransmitted = False
            for gap_start, gap_end in gaps:
                for packet_id, (seq_range, sent_time, acked) in list(self.sent_packets.items()):
                    if not acked and seq_range[0] >= gap_start and seq_range[1] <= gap_end:
                        print(f"3次 duplicate ack，啟動 fast retransmit，seq：{seq_range}")
                        self.sent_packets[packet_id] = (seq_range, 0, False)
        # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
        self.acked_packet_ids = set()
        retransmitted = True
        if retransmitted:
            del self.dup_acks[sack_key]

    def ack_packet(self, sacks: List[Tuple[int, int]], packet_id: int) -> int:
        # [解決inflight] 檢查是否已處理過此封包 ID
        if packet_id in self.acked_packet_ids:
            return 0
        self.acked_packet_ids.add(packet_id)
        freed_bytes = 0
        current_time = time.time()

        # EstimatedRTT = α × SampleRTT + (1 - α) × EstimatedRTT
        # DevRTT = α × |SampleRTT - EstimatedRTT| + (1 - α) × DevRTT
        # RTO = EstimatedRTT + 4 × DevRTT

        # 計算 SampleRTT 並更新 RTO
        if packet_id in self.sent_packets:
            sent_time = self.sent_packets[packet_id][1]
            if sent_time > 0:  # 確保 sent_time > 0
                sample_rtt = current_time - sent_time
                if self.estimated_rtt == 0.0:
                    self.estimated_rtt = sample_rtt # EstimatedRTT 初始值是 SampleRTT
                    self.dev_rtt = sample_rtt / 2 # DevRTT 初始值設定成 SampleRTT 的一半
                else:
                    self.estimated_rtt = self.alpha * sample_rtt + (1 - self.alpha) * self.estimated_rtt # EstimatedRTT公式
                    self.dev_rtt = self.alpha * abs(sample_rtt - self.estimated_rtt) +  (1 - self.alpha) * self.dev_rtt # DevRTT公式

                # Make sure rto is never smaller than your machine’s ability to measure time, say 1 ms (or 0.001seconds).
                self.timeout_interval = max(0.001, self.estimated_rtt + 4 * self.dev_rtt) # RTO 公式
                print(f"SampleRTT: {sample_rtt:.3f}s, EstimatedRTT: {self.estimated_rtt:.3f}s, "
                      f"DevRTT: {self.dev_rtt:.3f}s, New RTO: {self.timeout_interval:.3f}s, cwnd: {self.cwnd}")

        # 處理確認的 bytes 並增加 cwnd
        for start, end in sacks:
            for seq in range(start, end):
                if seq not in self.acked_bytes:
                    self.acked_bytes.add(seq)
                    freed_bytes += 1
                    for pid, (seq_range, sent_time, acked) in self.sent_packets.items():
                        if not acked and seq_range[0] <= seq < seq_range[1]:
                            self.sent_packets[pid] = (seq_range, sent_time, True)
        # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
        self.acked_packet_ids = set()

        # 更新 cwnd

        # if self.state == "slow_start":
        #     if self.cwnd < self.ssthresh:
        #         self.cwnd += packet_size
        #         print(f"[slow start] ACK 增加 cwnd 至 {self.cwnd}")
        #     else:
        #         self.state = "congestion_avoidance"
        #         print(f"[slow start] cwnd 超過 ssthresh，切換到 congestion avoidance")

        # elif self.state == "congestion_avoidance": 
        #     self.cwnd += packet_size * packet_size / self.cwnd
        #     print(f"[congestion avoidance] ACK 增加 cwnd 至 {self.cwnd}")

        # elif self.state == "fast_recovery": 
        #     self.cwnd = max(packet_size, self.ssthresh)
        #     self.state = "congestion_avoidance"
        #     print(f"[fast recovery] ACK 增加 cwnd 至 {self.cwnd}")
        #     print("切換到 congestion avoidance ")
                                

        seq = 0
        while seq in self.acked_bytes:
            seq += 1
        self.last_acked = seq - 1

        self._check_duplicate_acks(sacks)
        return freed_bytes

    def send(self, packet_id: int) -> Optional[Tuple[int, int]]:
        if self.next_seq >= self.data_len and len(self.acked_bytes) == self.data_len:
            print(f"Debug: All data sent and acked, next_seq={self.next_seq}, acked_bytes={len(self.acked_bytes)}")
            return None

        # 檢查是否有需要重新傳輸的封包
        for pid, (seq_range, sent_time, acked) in list(self.sent_packets.items()):
            if not acked and (sent_time == 0 or time.time() - sent_time >= self.timeout_interval):
                print(f"重新傳輸 sequence：{seq_range}, ID: {pid}")
                self.sent_packets[pid] = (seq_range, time.time(), False)
                # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
                self.acked_packet_ids = set()
                return seq_range

        # 正常發送新的封包
        if self.next_seq < self.data_len:
            start_seq = self.next_seq
            end_seq = min(start_seq + payload_size, self.data_len)
            self.sent_packets[packet_id] = ((start_seq, end_seq), time.time(), False)
            # [解決inflight] 初始化 acked_packet_ids 以避免重複處理封包
            self.acked_packet_ids = set()
            self.next_seq = end_seq
            return (start_seq, end_seq)

        return (0, 0)

    def get_cwnd(self) -> int:
        return max(packet_size, int(self.cwnd))  # 確保 cwnd 不小於一個封包大小

    def get_rto(self) -> float:
        return self.timeout_interval

def start_receiver(ip: str, port: int):
    receivers: Dict[str, Tuple[Receiver, Any]] = {}
    received_data = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server_socket:
        server_socket.bind((ip, port))

        while True:
            print("======= Waiting =======")
            data, addr = server_socket.recvfrom(packet_size)
            if addr not in receivers:
                outfile = open(f'rcvd-{addr[0]}-{addr[1]}', 'w', encoding='utf-8')
                receivers[addr] = (Receiver(), outfile)
                received_data[addr] = ''

            received = json.loads(data.decode())
            if received["type"] == "data":
                assert type(received["seq"]) is list
                assert type(received["seq"][0]) is int and type(received["seq"][1]) is int
                assert type(received["payload"]) is str
                assert len(received["payload"]) <= payload_size

                sacks, app_data = receivers[addr][0].data_packet(tuple(received["seq"]), received["payload"])
                if app_data:
                    receivers[addr][1].write(app_data)
                    receivers[addr][1].flush()
                    received_data[addr] += app_data
                print(f"Received seq: {received['seq']}, id: {received['id']}, sending sacks: {sacks}")

                server_socket.sendto(json.dumps({"type": "ack", "sacks": sacks, "id": received["id"]}).encode(), addr)

            elif received["type"] == "fin":
                receivers[addr][0].finish()
                if received_data and addr in received_data:
                    data_str = received_data[addr]
                    print("received data (summary): ", data_str[:100], "...", len(received_data))
                    print("received file is saved into: ", receivers[addr][1].name)
                    server_socket.sendto(json.dumps({"type": "fin"}).encode(), addr)
                    received_data[addr] = ''

                receivers[addr][1].close()
                del receivers[addr]
                del received_data[addr]
                break
            else:
                assert False

def start_sender(ip: str, port: int, data: str, recv_window: int, simloss: float, fixed_cwnd: int):
    sender = Sender(len(data), fixed_cwnd)
    start_time = time.time()  # 記錄傳輸開始時間 用於計算goodput

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        client_socket.connect((ip, port))
        client_socket.settimeout(0.1)

        inflight = 0
        packet_id = 0
        wait = False

        while True:
            cwnd = sender.get_cwnd()
            if inflight + packet_size <= min(recv_window, cwnd) and not wait:
                seq = sender.send(packet_id)
                got_fin_ack = False
                if seq is None:
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
                    wait = True
                    continue

                assert seq[1] - seq[0] <= payload_size
                assert seq[1] <= len(data)
                print(f"Sending seq: {seq}, id: {packet_id}")

                if random.random() < simloss:
                    print("Dropped!")
                else:
                    pkt_str = json.dumps(
                        {"type": "data", "seq": seq, "id": packet_id, "payload": data[seq[0]:seq[1]]}
                    ).encode()
                    client_socket.send(pkt_str)

                inflight += seq[1] - seq[0]
                packet_id += 1

            else:
                wait = False
                try:
                    print("======= Waiting =======")
                    received = client_socket.recv(packet_size)
                    received = json.loads(received.decode())
                    assert received["type"] == "ack"

                    if random.random() < simloss:
                        print("Dropped ack!")
                        continue

                    print(f"Got ACK sacks: {received['sacks']}, id: {received['id']}")
                    inflight -= sender.ack_packet(received['sacks'], received["id"])
                    # [解決inflight] 避免 inflight 為負值
                    if inflight < 0:
                        inflight = 0
                    assert inflight >= 0
                except socket.timeout:
                    inflight = 0  
                    print("Timeout")
                    sender.timeout()
        # 傳輸結束，計算 goodput
        end_time = time.time()  # 記錄傳輸結束時間
        total_unique_bytes = len(sender.acked_bytes)  # 唯一確認的字節數
        duration = end_time - start_time  # 傳輸總時間（秒）
        goodput = total_unique_bytes / duration if duration > 0 else 0  # 計算 goodput
        print(f"結算 - Total unique bytes: {total_unique_bytes}, Duration: {duration:.2f}s, "f"Goodput: {goodput:.2f} bytes/s")

        with open("goodput_results.txt", "a") as f:  # "a" 不會覆蓋原有內容
            f.write(f"cwnd: {sender.cwnd}, Total unique bytes: {total_unique_bytes}, "f"Duration: {duration:.2f}s, Goodput: {goodput:.2f} bytes/s\n")
            f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")  # 紀錄時間

def main():
        
    parser = argparse.ArgumentParser(description="Transport assignment")
    parser.add_argument("role", choices=["sender", "receiver"], help="Role to play: 'sender' or 'receiver'")
    parser.add_argument("--ip", type=str, required=True, help="IP address to bind/connect to")
    parser.add_argument("--port", type=int, required=True, help="Port number to bind/connect to")
    parser.add_argument("--sendfile", type=str, required=False, help="If role=sender, the file that contains data to send")
    parser.add_argument("--recv_window", type=int, default=15000000, help="Receive window size in bytes")
    parser.add_argument("--simloss", type=float, default=0.0, help="Simulate packet loss. Provide the fraction of packets (0-1) that should be randomly dropped")

    args = parser.parse_args()

    if args.role == "receiver":
        start_receiver(args.ip, args.port)
    else:
        if args.sendfile is None:
            print("No file to send")
            return

        with open(args.sendfile, 'r', encoding='utf-8') as f:
            data = f.read().replace('\r\n', '\n')
        
        cwnd = BDP * MULT  # 計算 cwnd
        print(f"\nRunning test with cwnd = {cwnd} bytes ({MULT}x BDP)")
        start_sender(args.ip, args.port, data, args.recv_window, args.simloss, cwnd)

if __name__ == "__main__":
    main()