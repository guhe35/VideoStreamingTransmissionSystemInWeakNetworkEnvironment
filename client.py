import socket
import subprocess
import time
import os
import datetime


def decode_video_client():
    # 服务器设置
    server_host = '127.0.0.1'
    server_port = 8888
    stream_port = server_port + 1  # 视频流端口

    try:
        # 连接到控制socket
        control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print(f"连接到控制服务器 {server_host}:{server_port}...")
        control_socket.connect((server_host, server_port))

        # 接收视频信息
        video_info = control_socket.recv(1024).decode('utf-8').split(',')
        if len(video_info) >= 9:
            width, height, codec_name = video_info[0:3]
            fps = video_info[3]
            audio_codec, audio_sample_rate, audio_channels = video_info[4:7]
            file_size = float(video_info[7])
            duration = float(video_info[8])

            print(f"接收视频信息: {width}x{height}, 编解码器: {codec_name}, 帧率: {fps}fps")
            print(f"接收音频信息: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")
            print(f"文件大小: {file_size / 1024 / 1024:.2f} MB, 时长: {duration:.2f}秒")

            # 确认接收
            control_socket.sendall(b'INFO_RECEIVED')
        else:
            print("接收到的视频信息不完整")
            return

        # 告诉服务器我们准备好了
        control_socket.sendall(b'READY')

        # 等待服务器开始信号
        data = control_socket.recv(1024)
        if data.decode('utf-8') == 'START':
            print("服务器已准备就绪，开始接收视频...")

            # 使用FFplay播放流 (带声音)
            stream_url = f'tcp://{server_host}:{stream_port}'
            print(f"打开视频流: {stream_url}")

            # 记录开始时间
            start_time = datetime.datetime.now()
            print(f"播放开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

            # 先等待一会儿，确保服务器已经开始发送
            time.sleep(1)

            # 使用FFplay播放，设置高质量
            cmd = [
                'ffplay',
                '-i', stream_url,
                '-autoexit',
                '-window_title', f'Video Stream (无损质量 {width}x{height})',
                # 添加额外的FFplay参数以确保高质量播放
                '-vf', f'scale={width}:{height}',  # 确保尺寸匹配
                '-fflags', 'nobuffer',  # 减少缓冲
                '-sync', 'audio',  # 以音频为基准同步
                '-stats',  # 显示播放统计信息
                '-framedrop',  # 允许丢帧以保持同步
                '-infbuf'  # 无限缓冲区
            ]
            print("启动FFplay: " + " ".join(cmd))

            # 启动FFplay进程
            ffplay_process = subprocess.Popen(cmd)

            # 等待播放结束，但保持与控制服务器的连接
            try:
                while ffplay_process.poll() is None:
                    # 检查是否有来自服务器的消息
                    control_socket.settimeout(0.5)
                    try:
                        msg = control_socket.recv(1024)
                        if msg == b'TRANSFER_COMPLETE':
                            print("服务器通知：传输完成")
                    except socket.timeout:
                        pass
                    except Exception as e:
                        print(f"接收服务器消息时出错: {e}")
                        break

                    time.sleep(0.5)
            except KeyboardInterrupt:
                print("用户中断播放")
            finally:
                # 确保FFplay进程结束
                if ffplay_process.poll() is None:
                    ffplay_process.terminate()
                    ffplay_process.wait()

            # 记录结束时间
            end_time = datetime.datetime.now()
            elapsed = (end_time - start_time).total_seconds()
            print(f"\n播放结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"总播放时间: {elapsed:.2f}秒")

    except ConnectionRefusedError:
        print("连接被拒绝，请确保服务器正在运行")
    except Exception as e:
        print(f"错误: {e}")
    finally:
        try:
            control_socket.close()
        except:
            pass
        print("客户端关闭")


if __name__ == "__main__":
    decode_video_client()
