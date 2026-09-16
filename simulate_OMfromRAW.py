import socket
import time
import struct
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("-f", "--file", help="File to send", required=True)
parser.add_argument("-i", "--ip", help="IP address to send to", default="127.0.0.1")
parser.add_argument("-p", "--port", help="Port to send to", default=8890)
parser.add_argument("-d", "--delay", help="Delay between each word", required=False, default=1)
args = parser.parse_args()

def send_words_udp(file_path, ip, port, delay=1):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    try:
        with open(file_path, 'rb') as file:
            # Read 4 bytes at a time
            while True:
                data = file.read(4)
                if not data:
                    break
                val = struct.unpack('<I', data)[0]
                # Convert to hex
                val = hex(val)
                val = val.replace('0x', '')
                print(f"Sending {val} to {ip}:{port}")
                sock.sendto(data, (ip, port))
                time.sleep(delay)
            # Go back to the beginning of the file
            file.seek(0)
    except FileNotFoundError:
        pass

if __name__ == "__main__":
    # Configuration
    TARGET_IP = "127.0.0.1"
    TARGET_PORT = 8890
    
    # Send the file
    send_words_udp(args.file, args.ip, int(args.port), int(args.delay))