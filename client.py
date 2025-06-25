import socket
import subprocess
import json
import time
import sys
import threading
import logging
import os
from collections import deque

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Buffer settings
JITTER_BUFFER_SIZE = 1000  # milliseconds
MAX_PACKET_LOSS = 5  # Maximum consecutive packet loss


def send_network_stats(socket, address):
    """Send network statistics back to server"""
    while True:
        try:
            data, addr = socket.recvfrom(1024)
            if data == b'PING':
                socket.sendto(b'PONG', addr)
        except:
            pass
        time.sleep(0.1)


def start_client(server_host, server_port):
    """Start client to receive and play video stream"""
    # Create UDP socket
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind to a port for receiving responses
    client_socket.bind(('0.0.0.0', 0))
    client_port = client_socket.getsockname()[1]

    logging.info(f"Client started on port {client_port}")

    try:
        # Send connection request
        server_address = (server_host, server_port)
        client_socket.sendto(b'CONNECT', server_address)

        # Start network stats thread
        stats_thread = threading.Thread(target=send_network_stats, args=(client_socket, server_address), daemon=True)
        stats_thread.start()

        # Receive video info
        client_socket.settimeout(5.0)
        data, _ = client_socket.recvfrom(4096)
        video_info = json.loads(data.decode())

        logging.info(f"Received video info: {video_info}")

        # Send ready signal
        client_socket.sendto(b'READY', server_address)

        # Create RTP socket for receiving video stream
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rtp_port = client_port + 1
        rtp_socket.bind(('0.0.0.0', rtp_port))

        logging.info(f"Listening for RTP stream on port {rtp_port}")

        # Wait for stream start signal
        data, _ = client_socket.recvfrom(1024)

        if data == b'STREAM_STARTED':
            logging.info("Stream is starting")

            # Create a temporary SDP file for FFplay
            sdp_content = f"""v=0
o=- 0 0 IN IP4 127.0.0.1
s=No Name
c=IN IP4 {server_host}
t=0 0
m=video {rtp_port} RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1
"""
            sdp_file = "temp_stream.sdp"
            with open(sdp_file, "w") as f:
                f.write(sdp_content)

            # Start FFplay with error resilience and jitter buffer
            cmd = [
                'ffplay',
                '-protocol_whitelist', 'file,rtp,udp',
                '-fflags', 'nobuffer',  # Reduce latency
                '-flags', 'low_delay',
                '-framedrop',  # Allow frame dropping for smoother playback
                '-i', sdp_file,
                '-probesize', '32',
                '-analyzeduration', '0',
                '-sync', 'ext',  # External clock sync
                '-max_delay', str(JITTER_BUFFER_SIZE * 1000),  # Convert to microseconds
                '-vf', 'setpts=PTS-STARTPTS',  # Reset timestamps
                '-err_detect', 'aggressive',  # Aggressive error detection
                '-max_error_rate', '0.5',  # Allow up to 50% errors before giving up
                '-stats'  # Show stats
            ]

            try:
                # Start FFplay process
                process = subprocess.Popen(cmd)

                # Monitor for stream control messages
                while True:
                    try:
                        client_socket.settimeout(1.0)
                        data, _ = client_socket.recvfrom(1024)

                        if data == b'STREAM_ENDED':
                            logging.info("Stream ended normally")
                            break
                        elif data == b'STREAM_RESTARTING':
                            logging.info("Stream is restarting with new parameters")
                            # No need to restart FFplay, it will adapt
                    except socket.timeout:
                        # Check if FFplay is still running
                        if process.poll() is not None:
                            logging.error("FFplay process ended unexpectedly")
                            break

                # Wait for FFplay to finish
                process.wait()

            except KeyboardInterrupt:
                logging.info("Client stopped by user")
            except Exception as e:
                logging.error(f"Error during playback: {e}")
            finally:
                # Clean up
                if 'process' in locals() and process.poll() is None:
                    process.terminate()
                if os.path.exists(sdp_file):
                    os.remove(sdp_file)
        else:
            logging.error("Unexpected response from server")

    except Exception as e:
        logging.error(f"Error: {e}")
    finally:
        client_socket.close()
        if 'rtp_socket' in locals():
            rtp_socket.close()
        logging.info("Client closed")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python client.py <server_host> [server_port]")
        sys.exit(1)

    server_host = sys.argv[1]
    server_port = int(sys.argv[2]) if len(sys.argv) > 2 else 12345

    start_client(server_host, server_port)
