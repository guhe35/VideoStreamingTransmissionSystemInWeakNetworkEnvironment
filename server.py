import asyncio
import logging
import ffmpeg
import os
import subprocess
import datetime
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import StreamDataReceived

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-server")

# 定义ALPN协议
ALPN_PROTOCOLS = ["quic-demo"]

# 全局变量用于控制服务器
server_running = False
server_task = None


class VideoServer(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.video_info = None
        self.ffmpeg_process = None
        self.stream_port = 8889  # 视频流端口
        self.video_file_path = None  # 视频文件路径

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            message = event.data.decode()
            logger.info(f"收到客户端消息: {message}")

            if message.startswith("SET_VIDEO_FILE:"):
                # 客户端设置视频文件路径
                file_path = message.split(":", 1)[1]
                self.video_file_path = file_path
                logger.info(f"客户端设置视频文件路径: {file_path}")
                
                # 验证文件是否存在
                if os.path.exists(file_path):
                    response = "FILE_EXISTS"
                    self._quic.send_stream_data(event.stream_id, response.encode())
                    logger.info("文件存在，已确认")
                    
                    # 异步分析视频信息
                    async def analyze_video_async():
                        try:
                            self.video_info = await analyze_video(file_path)
                            if self.video_info:
                                logger.info("视频信息分析完成")
                            else:
                                logger.error("视频信息分析失败")
                        except Exception as e:
                            logger.error(f"分析视频信息时出错: {e}")
                    
                    # 创建异步任务
                    asyncio.create_task(analyze_video_async())
                else:
                    response = "FILE_NOT_FOUND"
                    self._quic.send_stream_data(event.stream_id, response.encode())
                    logger.error(f"文件不存在: {file_path}")

            elif message == "REQUEST_VIDEO_INFO":
                # 发送视频信息给客户端
                if self.video_info:
                    response = f"VIDEO_INFO:{self.video_info}"
                    self._quic.send_stream_data(event.stream_id, response.encode())
                    logger.info("已发送视频信息给客户端")
                else:
                    response = "NO_VIDEO_INFO"
                    self._quic.send_stream_data(event.stream_id, response.encode())
                    logger.error("没有视频信息或文件路径")

            elif message == "READY_FOR_STREAM":
                # 客户端准备就绪，开始视频流传输
                self._quic.send_stream_data(event.stream_id, b"START_STREAM")
                logger.info("客户端准备就绪，开始视频流传输")
                self.start_video_stream()

            elif message == "STREAM_COMPLETE":
                # 客户端通知流传输完成
                logger.info("客户端确认视频流传输完成")
                if self.ffmpeg_process:
                    self.ffmpeg_process.terminate()

    def start_video_stream(self):
        """启动FFmpeg视频流传输"""
        try:
            if not self.video_file_path:
                logger.error("没有设置视频文件路径")
                return
                
            input_file = self.video_file_path
            host = '127.0.0.1'
            
            # 使用FFmpeg进行视频流传输
            cmd = [
                'ffmpeg', '-i', input_file,
                '-f', 'mpegts',
                '-vcodec', 'copy',  # 直接复制视频编码
                '-acodec', 'copy',  # 直接复制音频编码
                '-flush_packets', '1',
                f'tcp://{host}:{self.stream_port}?listen=1'
            ]

            logger.info("启动FFmpeg视频流服务...")
            logger.info(" ".join(cmd))

            self.ffmpeg_process = subprocess.Popen(cmd)
            logger.info(f"FFmpeg进程已启动，PID: {self.ffmpeg_process.pid}")

        except Exception as e:
            logger.error(f"启动视频流时出错: {e}")


async def analyze_video(input_file):
    """分析视频文件并返回视频信息"""
    if not os.path.exists(input_file):
        logger.error(f"错误：文件 {input_file} 不存在")
        return None

    try:
        # 获取文件大小
        file_size = os.path.getsize(input_file)
        logger.info(f"视频文件大小: {file_size / 1024 / 1024:.2f} MB")

        # 获取视频信息
        probe = ffmpeg.probe(input_file)
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream is None:
            logger.error("错误：没有找到视频流")
            return None

        # 提取视频参数
        width = int(video_stream['width'])
        height = int(video_stream['height'])
        codec_name = video_stream['codec_name']
        frame_rate = video_stream.get('r_frame_rate', '30/1').split('/')
        fps = float(int(frame_rate[0]) / int(frame_rate[1]))
        duration = float(probe['format'].get('duration', 0))

        # 查找音频流
        audio_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'audio'), None)
        audio_codec = audio_stream['codec_name'] if audio_stream else "无"
        audio_sample_rate = audio_stream['sample_rate'] if audio_stream else "0"
        audio_channels = audio_stream['channels'] if audio_stream else "0"

        # 构建视频信息字符串
        video_info = f"{width},{height},{codec_name},{fps},{audio_codec},{audio_sample_rate},{audio_channels},{file_size},{duration}"

        logger.info(f"原始视频: {width}x{height}, 编解码器: {codec_name}, 帧率: {fps}fps, 时长: {duration:.2f}秒")
        if audio_stream:
            logger.info(f"原始音频: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")

        return video_info

    except Exception as e:
        logger.error(f"分析视频时出错: {e}")
        return None


async def start_server():
    """启动服务器的函数，可以被其他程序调用"""
    global server_running, server_task

    # 创建QUIC配置
    config = QuicConfiguration(is_client=False)
    config.alpn_protocols = ALPN_PROTOCOLS

    # 加载证书和密钥
    config.load_cert_chain("cert.pem", "key.pem")

    # 创建服务器实例
    def create_protocol(*args, **kwargs):
        protocol = VideoServer(*args, **kwargs)
        return protocol

    # 启动服务器
    server = await serve(
        "0.0.0.0",  # 监听所有网络接口
        4433,  # QUIC端口
        configuration=config,
        create_protocol=create_protocol
    )

    logger.info(f"QUIC视频服务器已启动，监听端口4433，支持的ALPN协议: {ALPN_PROTOCOLS}")
    logger.info("等待客户端连接...")
    
    server_running = True
    
    # 保持服务器运行直到被停止
    try:
        while server_running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("服务器任务被取消")
    finally:
        server_running = False
        logger.info("服务器已停止")
    
    return True


async def stop_server():
    """停止服务器"""
    global server_running
    server_running = False
    logger.info("正在停止服务器...")


async def main():
    """主函数，用于独立运行服务器"""
    try:
        await start_server()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭服务器...")
        await stop_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务器已关闭") 