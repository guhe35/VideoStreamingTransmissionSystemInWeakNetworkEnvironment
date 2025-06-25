import socket
import subprocess
import json
import time
import os
import sys
import threading
import logging
from rtplib import RtpPacket

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# FEC parameters
FEC_RATIO = 0.2  # 20% redundancy for Forward Error Correction


def get_video_info(video_path):
    """Get video information using ffprobe"""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,r_frame_rate,bit_rate',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        info = json.loads(result.stdout)
        stream = info['streams'][0]

        # Parse frame rate (which comes as a fraction like '30000/1001')
        fps_parts = stream['r_frame_rate'].split('/')
        fps = float(fps_parts[0]) / float(fps_parts[1]) if len(fps_parts) > 1 else float(fps_parts[0])

        return {
            'width': stream.get('width', 1280),
            'height': stream.get('height', 720),
            'fps': round(fps, 2),
            'bitrate': stream.get('bit_rate', '2000000')
        }
    except Exception as e:
        logging.error(f"Error getting video info: {e}")
        # Default values if ffprobe fails
        return {'width': 1280, 'height': 720, 'fps': 30.0, 'bitrate': '2000000'}


def adaptive_bitrate(connection_quality):
    """Adjust bitrate based on connection quality (0-100)"""
    if connection_quality > 80:
        return "2M"
    elif connection_quality > 50:
        return "1M"
    elif connection_quality > 30:
        return "800k"
    else:
        return "500k"


def network_monitor(client_socket):
    """Monitor network conditions and return connection quality score (0-100)"""
    try:
        # Send ping packet
        start_time = time.time()
        client_socket.sendto(b'PING', client_socket.getpeername())

        # Wait for response with timeout
        client_socket.settimeout(1.0)
        try:
            data, _ = client_socket.recvfrom(1024)
            if data == b'PONG':
                rtt = (time.time() - start_time) * 1000  # RTT in ms

                # Calculate quality score (lower RTT = higher score)
                # 0ms -> 100, 500ms -> 0
                quality = max(0, 100 - (rtt / 5))
                return quality
        except socket.timeout:
            return 20  # Low quality if timeout
    except:
        return 30  # Default moderate-low quality

    return 50  # Default moderate quality


def start_streaming(video_path, host, port):
    """Start streaming video using RTP over UDP"""
    # Get video information
    video_info = get_video_info(video_path)
    logging.info(f"Video info: {video_info}")

    # Create UDP socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))

    logging.info(f"Server started on {host}:{port}")
    logging.info("Waiting for client connection...")

    # Wait for client to connect (first message)
    data, client_address = server_socket.recvfrom(1024)
    if data == b'CONNECT':
        logging.info(f"Client connected from {client_address}")

        # Send video info to client
        server_socket.sendto(json.dumps(video_info).encode(), client_address)

        # Wait for client acknowledgment
        data, _ = server_socket.recvfrom(1024)
        if data == b'READY':
            logging.info("Client is ready to receive video stream")

            # Start network monitoring thread
            connection_quality = 80  # Initial quality assumption

            def monitor_loop():
                nonlocal connection_quality
                while True:
                    connection_quality = network_monitor(server_socket)
                    time.sleep(2)  # Check every 2 seconds

            monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
            monitor_thread.start()

            # Start streaming with adaptive bitrate
            try:
                while True:
                    # Adjust parameters based on network conditions
                    bitrate = adaptive_bitrate(connection_quality)
                    gop_size = max(int(video_info['fps'] * 2), 30)  # Adjust GOP size

                    # Create FFmpeg command for RTP streaming
                    cmd = [
                        'ffmpeg',
                        '-re',  # Read input at native frame rate
                        '-i', video_path,
                        '-c:v', 'libx264',
                        '-preset', 'ultrafast',  # Faster encoding for real-time
                        '-tune', 'zerolatency',  # Minimize latency
                        '-b:v', bitrate,
                        '-maxrate', bitrate,
                        '-bufsize', f"{int(bitrate[:-1]) // 2}M",
                        '-g', str(gop_size),  # GOP size
                        '-keyint_min', str(gop_size // 2),
                        '-sc_threshold', '0',  # Disable scene detection
                        '-f', 'rtp',
                        f'rtp://{client_address[0]}:{client_address[1] + 1}?pkt_size=1316'
                    ]

                    logging.info(f"Starting FFmpeg with bitrate {bitrate}, connection quality: {connection_quality}")

                    # Start FFmpeg process
                    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

                    # Send start signal to client
                    server_socket.sendto(b'STREAM_STARTED', client_address)

                    # Wait for process to finish or network conditions to change significantly
                    last_quality = connection_quality
                    while process.poll() is None:
                        time.sleep(1)
                        # If network quality changes significantly, restart with new parameters
                        if abs(connection_quality - last_quality) > 20:
                            logging.info(
                                f"Network quality changed significantly: {last_quality} -> {connection_quality}")
                            process.terminate()
                            break
                        last_quality = connection_quality

                    # Check if process ended normally
                    if process.returncode == 0:
                        logging.info("FFmpeg process completed successfully")
                        server_socket.sendto(b'STREAM_ENDED', client_address)
                        break

                    logging.info("Restarting stream with adjusted parameters")
                    server_socket.sendto(b'STREAM_RESTARTING', client_address)

            except KeyboardInterrupt:
                logging.info("Server stopped by user")
            except Exception as e:
                logging.error(f"Error during streaming: {e}")
            finally:
                if 'process' in locals() and process.poll() is None:
                    process.terminate()
                server_socket.close()
                logging.info("Server closed")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python server.py <video_path> [host] [port]")
        sys.exit(1)

    video_path = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else '0.0.0.0'
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 12345

    if not os.path.exists(video_path):
        print(f"Error: Video file '{video_path}' not found")
        sys.exit(1)

    start_streaming(video_path, host, port)
