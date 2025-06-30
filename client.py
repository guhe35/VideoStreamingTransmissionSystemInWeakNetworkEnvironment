import asyncio
import logging
import ssl
import subprocess
import time
import datetime
import threading
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import StreamDataReceived

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-client")

# 定义ALPN协议
ALPN_PROTOCOLS = ["quic-demo"]


class VideoClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.video_info = None
        self.ffplay_process = None
        self.stream_port = 8889  # 视频流端口
        self._response_received = asyncio.Event()
        self.response = None
        self.video_file_path = None  # 视频文件路径
        self.streaming = False

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            data = event.data
            if self.streaming:
                if self.ffplay_process and self.ffplay_process.stdin:
                    try:
                        self.ffplay_process.stdin.write(data)
                        self.ffplay_process.stdin.flush()
                    except Exception as e:
                        logger.error(f"写入FFplay管道时出错: {e}")
                return
            try:
                message = data.decode()
                logger.info(f"收到服务器消息: {message}")
                if message == "FILE_EXISTS":
                    logger.info("文件存在，正在分析视频信息...")
                    ctrl_stream_id = self._quic.get_next_available_stream_id()
                    self._quic.send_stream_data(ctrl_stream_id, b"REQUEST_VIDEO_INFO")
                elif message == "FILE_NOT_FOUND":
                    logger.error("服务器找不到指定的视频文件")
                    self._response_received.set()
                elif message.startswith("VIDEO_INFO:"):
                    video_info_str = message.split(":", 1)[1]
                    self.video_info = video_info_str
                    self.parse_video_info(video_info_str)
                    ctrl_stream_id = self._quic.get_next_available_stream_id()
                    self._quic.send_stream_data(ctrl_stream_id, b"READY_FOR_STREAM")
                    logger.info("已请求开始视频流传输")
                elif message == "NO_VIDEO_INFO":
                    logger.error("服务器没有视频信息")
                    self._response_received.set()
                elif message == "START_STREAM":
                    logger.info("服务器开始传输视频流")
                    self.streaming = True
                    self.start_video_playback()
            except UnicodeDecodeError:
                pass

    def set_video_file(self, file_path):
        """设置视频文件路径"""
        self.video_file_path = file_path

    def parse_video_info(self, video_info_str):
        """解析视频信息"""
        try:
            info_parts = video_info_str.split(',')
            if len(info_parts) >= 9:
                width, height, codec_name = info_parts[0:3]
                fps = info_parts[3]
                audio_codec, audio_sample_rate, audio_channels = info_parts[4:7]
                file_size = float(info_parts[7])
                duration = float(info_parts[8])

                logger.info(f"视频信息: {width}x{height}, 编解码器: {codec_name}, 帧率: {fps}fps")
                logger.info(f"音频信息: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")
                logger.info(f"文件大小: {file_size / 1024 / 1024:.2f} MB, 时长: {duration:.2f}秒")

                # 保存解析的信息供播放使用
                self.video_width = width
                self.video_height = height
                self.video_codec = codec_name
                self.video_fps = fps
                self.audio_codec = audio_codec
                self.audio_sample_rate = audio_sample_rate
                self.audio_channels = audio_channels
                self.file_size = file_size
                self.duration = duration

        except Exception as e:
            logger.error(f"解析视频信息时出错: {e}")

    def start_video_playback(self):
        """启动视频播放"""
        def play_video():
            try:
                time.sleep(1)
                cmd = [
                    'ffplay', '-i', 'pipe:0',
                    '-fflags', 'nobuffer',
                    '-flags', 'low_delay',
                    '-framedrop'
                ]
                logger.info("启动FFplay: " + " ".join(cmd))
                self.ffplay_process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                self.ffplay_process.wait()
                # 播放结束后通知服务器
                try:
                    ctrl_stream_id = self._quic.get_next_available_stream_id()
                    self._quic.send_stream_data(ctrl_stream_id, b"STREAM_COMPLETE")
                    logger.info("已通知服务器播放完成")
                except Exception as e:
                    logger.error(f"通知服务器时出错: {e}")
                # 关闭ffplay进程
                if self.ffplay_process and self.ffplay_process.poll() is None:
                    self.ffplay_process.terminate()
                    self.ffplay_process.wait()
            except Exception as e:
                logger.error(f"播放视频时出错: {e}")
        playback_thread = threading.Thread(target=play_video)
        playback_thread.daemon = True
        playback_thread.start()


async def run_client(video_file_path=None):
    # 如果没有提供文件路径，使用文件浏览器选择
    if not video_file_path:
        print("\n" + "="*50)
        print("视频文件选择")
        print("="*50)
        print("正在打开文件浏览器...")
        print("="*50)
        
        # 使用文件浏览器选择文件
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()  # 隐藏主窗口
        video_file_path = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[
                ("视频文件", "*.mp4 *.avi *.mkv *.mov *.wmv *.flv"),
                ("所有文件", "*.*")
            ]
        )
        
        if not video_file_path:
            logger.error("未选择文件，程序退出")
            return
        
        print(f"已选择文件: {video_file_path}")

    # 创建QUIC配置
    config = QuicConfiguration(is_client=True)
    config.alpn_protocols = ALPN_PROTOCOLS
    config.verify_mode = ssl.CERT_NONE

    logger.info(f"正在连接到QUIC视频服务器，使用ALPN协议: {ALPN_PROTOCOLS}...")
    logger.info(f"选择的视频文件: {video_file_path}")

    # 连接重试机制
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            # 连接到服务器
            async with connect(
                    "127.0.0.1",  # 服务器地址
                    4433,  # QUIC端口
                    configuration=config,
                    create_protocol=VideoClient
            ) as client:
                logger.info("已连接到服务器")

                # 设置视频文件路径
                client.set_video_file(video_file_path)
                
                # 发送文件路径给服务器
                logger.info("发送视频文件路径给服务器...")
                stream_id = client._quic.get_next_available_stream_id()
                client._quic.send_stream_data(stream_id, f"SET_VIDEO_FILE:{video_file_path}".encode())

                # 等待服务器响应
                await asyncio.sleep(2)

                # 保持连接直到播放完成
                try:
                    while True:
                        await asyncio.sleep(1)
                        # 检查FFplay进程是否还在运行
                        if hasattr(client, 'ffplay_process') and client.ffplay_process:
                            if client.ffplay_process.poll() is not None:
                                logger.info("视频播放已完成")
                                break
                except KeyboardInterrupt:
                    logger.info("用户中断播放")
                    if hasattr(client, 'ffplay_process') and client.ffplay_process:
                        client.ffplay_process.terminate()
                    break
                
                # 如果正常完成，跳出重试循环
                break

        except ConnectionRefusedError:
            retry_count += 1
            logger.warning(f"连接被拒绝，服务器可能未启动。重试 {retry_count}/{max_retries}")
            if retry_count < max_retries:
                await asyncio.sleep(2)  # 等待2秒后重试
            else:
                logger.error("无法连接到服务器，请确保服务器已启动")
                break
                
        except Exception as e:
            retry_count += 1
            logger.error(f"连接错误 (尝试 {retry_count}/{max_retries}): {e}")
            if retry_count < max_retries:
                await asyncio.sleep(2)  # 等待2秒后重试
            else:
                logger.error("连接失败，已达到最大重试次数")
                import traceback
                logger.error(traceback.format_exc())
                break


if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        logger.info("客户端已关闭") 