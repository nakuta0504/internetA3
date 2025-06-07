# Your Name: 林仲威
# Your ID: B092010020

# A3 作業版本
# 移除 reorder 功能，保留 SACK、fast retransmit 和 timeout 處理
# 支援多個 gap 的 fast retransmit

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
        # 下一個預期的 sequence number
        self.next_expected_seq = 0
        # 已接收的 sequence number，用於生成 SACKs
        self.received_ranges = []

    def data_packet(self, seq_range: Tuple[int, int], data: str) -> Tuple[List[Tuple[int, int]], str]:
        '''
        處理接收到的資料封包。
        參數：
            seq_range: 封包的範圍，格式為 (start, end)。
            data: 封包的資料內容。
        返回：
            裝有已接收的 sequence number 的 list 與資料的字串。
        '''
        start_seq, end_seq = seq_range

        # 驗證 sequence number 的有效性
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

        # 檢查是否可以按序傳遞資料給應用程式
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
        '''
        更新已接收的 seq 範圍，合併重疊或相鄰的範圍。
        參數：
            start_seq
            end_seq
        '''
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
        '''
        當發送方發送 FIN 封包時調用。
        檢查是否所有資料都已傳遞（用來 DEBUG）。
        '''
        if self.buffer:
            print(f"DEBUG：結束時 buffer 仍有 {len(self.buffer)} 個封包未處理")
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
        # 儲存已發送的封包：{packet_id: (seq_range, sent_time, acked)}
        self.sent_packets: Dict[int, Tuple[Tuple[int, int], float, bool]] = {}
        # 儲存已確認的 bytes
        self.acked_bytes = set()
        self.last_acked = -1
        # 超時間隔（RTO），初始值設為 1 秒（根據作業建議）
        self.timeout_interval = 1.0
        # 用於計算 RTO 的變數
        self.estimated_rtt = 0.0
        self.dev_rtt = 0.0
        # 用於 fast retransmit：追蹤重複 SACKs
        self.dup_acks: Dict[Tuple[Tuple[int, int], ...], int] = {}

    def timeout(self):
        '''
        處理超時，將未確認的封包標記為需要重新傳輸。
        '''
        print("Sender timeout，重新傳輸未確認的封包")
        for packet_id, (seq_range, _, acked) in list(self.sent_packets.items()):
            if not acked:
                self.sent_packets[packet_id] = (seq_range, 0, False)

    def _check_duplicate_acks(self, sacks: List[Tuple[int, int]]):
        '''
        檢查重複確認，根據連續相同 SACKs 觸發 fast retransmit，可處理多個 gap。
        參數：
            sacks: 已確認的 seq 範圍 list。
        '''
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

        if self.dup_acks[sack_key] >= 3:
            gaps = []
            prev_end = -1
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
                        print("*********************************************")
                        print(f"3次 duplicate ack，啟動 fast retransmit，seq：{seq_range}")
                        print("*********************************************")
                        self.sent_packets[packet_id] = (seq_range, 0, False)
                        retransmitted = True
            if retransmitted:
                del self.dup_acks[sack_key]

    def ack_packet(self, sacks: List[Tuple[int, int]], packet_id: int) -> int:
        '''
        處理接收到的確認。
        參數：
            sacks: 已確認的 seq 範圍 list。
            packet_id: 觸發此確認的封包 ID。
        返回：
            freed_bytes: 新確認的字節數。
        '''
        freed_bytes = 0
        current_time = time.time()

        # 計算 SampleRTT 並更新 RTO
        if packet_id in self.sent_packets:
            sent_time = self.sent_packets[packet_id][1]
            sample_rtt = current_time - sent_time
            if self.estimated_rtt == 0.0:
                # 第一次計算，初始化值
                self.estimated_rtt = sample_rtt
                self.dev_rtt = sample_rtt / 2
            else:
                # 使用 EWMA 計算 EstimatedRTT 和 DevRTT，α = 1/64
                alpha = 1/64
                self.estimated_rtt = (1 - alpha) * self.estimated_rtt + alpha * sample_rtt
                self.dev_rtt = (1 - alpha) * self.dev_rtt + alpha * abs(sample_rtt - self.estimated_rtt)
            # 更新 RTO，最小值為 1 毫秒
            self.timeout_interval = max(0.001, self.estimated_rtt + 4 * self.dev_rtt)
            print(f"SampleRTT: {sample_rtt:.3f}s, EstimatedRTT: {self.estimated_rtt:.3f}s, DevRTT: {self.dev_rtt:.3f}s, New RTO: {self.timeout_interval:.3f}s")

        # 處理確認的字節
        for start, end in sacks:
            for seq in range(start, end):
                if seq not in self.acked_bytes:
                    self.acked_bytes.add(seq)
                    freed_bytes += 1
                    for pid, (seq_range, sent_time, acked) in self.sent_packets.items():
                        if not acked and seq_range[0] <= seq < seq_range[1]:
                            self.sent_packets[pid] = (seq_range, sent_time, True)

        print(f"Debug: acked_bytes size = {len(self.acked_bytes)}, expected data_len = {self.data_len}")
        seq = 0
        while seq in self.acked_bytes:
            seq += 1
        self.last_acked = seq - 1

        self._check_duplicate_acks(sacks)
        return freed_bytes

    def send(self, packet_id: int) -> Optional[Tuple[int, int]]:
        '''
        決定下一個要發送的序列範圍。
        參數：
            packet_id: 新封包的 ID。
        返回：
            要發送的 seq 範圍，若所有資料已發送且確認則返回 None。
        '''
        if self.next_seq >= self.data_len and len(self.acked_bytes) == self.data_len:
            print(f"Debug: All data sent and acked, next_seq={self.next_seq}, acked_bytes={len(self.acked_bytes)}")
            return None

        # 檢查是否有需要重新傳輸的封包
        for pid, (seq_range, sent_time, acked) in list(self.sent_packets.items()):
            if not acked and (sent_time == 0 or time.time() - sent_time >= self.timeout_interval):
                print(f"重新傳輸 sequence：{seq_range}, ID: {pid}")
                self.sent_packets[pid] = (seq_range, time.time(), False)
                return seq_range

        # 正常發送新的封包
        if self.next_seq < self.data_len:
            start_seq = self.next_seq
            end_seq = min(start_seq + payload_size, self.data_len)
            self.sent_packets[packet_id] = ((start_seq, end_seq), time.time(), False)
            self.next_seq = end_seq
            return (start_seq, end_seq)

        return (0, 0)

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

def start_sender(ip: str, port: int, data: str, recv_window: int, simloss: float):
    sender = Sender(len(data))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client_socket:
        client_socket.connect((ip, port))
        client_socket.settimeout(0.1)  # 設置 socket 超時為 0.1 秒，與 RTO 一致

        inflight = 0
        packet_id = 0
        wait = False

        while True:
            if inflight + packet_size < recv_window and not wait:
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
        start_sender(args.ip, args.port, data, args.recv_window, args.simloss)

if __name__ == "__main__":
    main()