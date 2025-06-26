import ffmpeg
import socket
import os
import subprocess
import threading
import time
import datetime


def encode_video_server():
    # 视频路径和网络设置
    input_file = 'input.mp4'  # 确保文件存在
    host = '127.0.0.1'
    port = 8888

    # 检查文件是否存在
    if not os.path.exists(input_file):
        print(f"错误：文件 {input_file} 不存在")
        return

    # 获取文件大小
    file_size = os.path.getsize(input_file)
    print(f"视频文件大小: {file_size / 1024 / 1024:.2f} MB")

    # 获取原始视频信息
    try:
        probe = ffmpeg.probe(input_file)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream is None:
            print("错误：没有找到视频流")
            return

        # 提取原始视频参数
        width = int(video_stream['width'])
        height = int(video_stream['height'])
        codec_name = video_stream['codec_name']
        frame_rate = video_stream.get('r_frame_rate', '30/1').split('/')
        fps = float(int(frame_rate[0]) / int(frame_rate[1]))
        duration = float(probe['format'].get('duration', 0))

        print(f"原始视频: {width}x{height}, 编解码器: {codec_name}, 帧率: {fps}fps, 时长: {duration:.2f}秒")

        # 查找音频流
        audio_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'audio'), None)
        audio_codec = audio_stream['codec_name'] if audio_stream else "无"
        audio_sample_rate = audio_stream['sample_rate'] if audio_stream else "0"
        audio_channels = audio_stream['channels'] if audio_stream else "0"

        if audio_stream:
            print(f"原始音频: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")
    except Exception as e:
        print(f"获取视频信息时出错: {e}")
        return

    # 设置socket服务器
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((host, port))
    server_socket.listen(1)

    print(f"等待客户端连接在 {host}:{port}...")
    client_socket, addr = server_socket.accept()
    print(f"客户端已连接: {addr}")

    # 准备无损传输参数
    try:
        # 发送视频信息给客户端
        video_info = f"{width},{height},{codec_name},{fps},{audio_codec},{audio_sample_rate},{audio_channels},{file_size},{duration}"
        client_socket.sendall(video_info.encode('utf-8'))

        # 等待客户端确认
        client_socket.recv(1024)

        # 使用更高质量的FFmpeg参数
        cmd = [
            'ffmpeg', '-i', input_file,
            '-f', 'mpegts',
            # 视频设置 - 直接复制
            '-vcodec', 'copy',
            # 音频设置 - 直接复制
            '-acodec', 'copy',
            '-flush_packets', '1',
            'tcp://' + host + ':' + str(port + 1) + '?listen=1'
        ]

        print("启动FFmpeg服务...")
        print(" ".join(cmd))

        process = subprocess.Popen(cmd)

        # 等待客户端连接准备就绪的信号
        data = client_socket.recv(1024)
        if data.decode('utf-8') == 'READY':
            print("客户端准备就绪，开始传输...")
            client_socket.sendall(b'START')

            # 显示传输开始时间
            start_time = datetime.datetime.now()
            print(f"传输开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

        # 保持主线程运行，直到ffmpeg完成
        process.wait()

        # 显示传输结束时间
        end_time = datetime.datetime.now()
        elapsed = (end_time - start_time).total_seconds()
        print(f"传输结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"总传输时间: {elapsed:.2f}秒")
        print(f"平均传输速度: {file_size / elapsed / 1024 / 1024:.2f} MB/s")

        # 发送传输完成信号
        try:
            client_socket.sendall(b'TRANSFER_COMPLETE')
        except:
            pass

        print("视频传输完成")

    except Exception as e:
        print(f"错误: {e}")
    finally:
        client_socket.close()
        server_socket.close()
        print("服务器关闭")


if __name__ == "__main__":
    encode_video_server()
