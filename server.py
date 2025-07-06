import asyncio
import logging
import ffmpeg
import os
import subprocess
import datetime
import time
import socket
import threading
import random
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import StreamDataReceived

# 导入BBR拥塞控制
from bbr_congestion_control import BBRCongestionControl, BBRMetrics
from quic_bbr_integration import BBRQuicProtocol, create_bbr_quic_configuration, BBRNetworkMonitor

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-server")

# 定义ALPN协议
ALPN_PROTOCOLS = ["quic-demo"]

# 全局变量用于控制服务器
server_running = False
server_task = None

# 网络质量等级定义
NETWORK_QUALITY = {
    "HIGH": {"video_bitrate": "4M", "video_scale": "854:480", "audio_bitrate": "128k"},
    "MEDIUM": {"video_bitrate": "2M", "video_scale": "640:360", "audio_bitrate": "96k"},
    "LOW": {"video_bitrate": "800k", "video_scale": "426:240", "audio_bitrate": "64k"},
    "VERY_LOW": {"video_bitrate": "400k", "video_scale": "256:144", "audio_bitrate": "32k"}
}

# 网络测速结果缓存
network_speed_cache = {"timestamp": 0, "speed": 0}

# 缓冲区大小（字节）
INITIAL_BUFFER_SIZE = 512 * 1024  # 初始缓冲区大小：512KB，与客户端保持一致


class VideoServer(BBRQuicProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.video_info = None
        self.ffmpeg_process = None
        self.stream_port = 8889  # 视频流端口
        self.video_file_path = None  # 视频文件路径
        self.network_quality = "MEDIUM"  # 默认网络质量
        self.current_bitrate = NETWORK_QUALITY["MEDIUM"]["video_bitrate"]
        self.network_monitor_thread = None
        self.stop_monitor = False
        self.buffer = bytearray()  # 初始化缓冲区
        self.is_streaming = False  # 流传输状态
        self.stream_start_time = None
        
        # BBR网络监控器 - 改进的实现
        self.bbr_monitor = BBRNetworkMonitor()
        self.bbr_monitor.start_monitoring(self)

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
                
                # 启动网络监控
                self.stop_monitor = False
                self.network_monitor_thread = threading.Thread(target=self.monitor_network_quality)
                self.network_monitor_thread.daemon = True
                self.network_monitor_thread.start()
                
                # 开始视频流传输
                asyncio.create_task(self.start_video_stream(event.stream_id))

            elif message == "STREAM_COMPLETE":
                logger.info("客户端确认视频流传输完成")
                self.stop_monitor = True
                self.is_streaming = False
                if self.ffmpeg_process:
                    try:
                        self.ffmpeg_process.terminate()
                        self.ffmpeg_process.wait(timeout=5)
                        logger.info("FFmpeg进程已关闭")
                    except Exception as e:
                        logger.error(f"关闭FFmpeg进程时出错: {e}")
            
            elif message.startswith("NETWORK_FEEDBACK:"):
                # 接收客户端的网络反馈
                try:
                    feedback = message.split(":", 1)[1]
                    quality_level = feedback.strip()
                    if quality_level in NETWORK_QUALITY:
                        self.network_quality = quality_level
                        logger.info(f"根据客户端反馈调整网络质量为: {quality_level}")
                except Exception as e:
                    logger.error(f"处理网络反馈时出错: {e}")

    def monitor_network_quality(self):
        """监控网络质量并调整编码参数 - 改进的实现"""
        last_quality_change_time = time.time()
        quality_stability_period = 10  # 10秒内不再调整质量
        
        while not self.stop_monitor:
            try:
                # 确保BBR已初始化
                self._ensure_bbr_initialized()
                
                # 强制更新BBR状态（即使没有新数据）
                if self.bbr:
                    self.bbr.update_state()
                
                # 获取BBR算法指标 - 改进的指标获取
                bbr_metrics = self.get_bbr_metrics()
                bbr_state = self.get_bbr_state_info()
                
                if bbr_metrics and bbr_state:
                    # 使用BBR带宽估算替代传统网络测速
                    bbr_bandwidth = bbr_metrics.bandwidth
                    bbr_rtt = bbr_metrics.rtt
                    
                    # 根据BBR带宽调整质量 - 改进的质量调整逻辑
                    new_quality = self._determine_quality_from_bbr(bbr_bandwidth, bbr_rtt)
                    
                    current_time = time.time()
                    # 只有当质量变化且上次调整已经超过稳定期时才进行调整
                    if new_quality != self.network_quality and current_time - last_quality_change_time > quality_stability_period:
                        logger.info(f"BBR网络质量变化: {self.network_quality} -> {new_quality} "
                                  f"(BBR带宽: {bbr_bandwidth/1000000:.2f} Mbps, RTT: {bbr_rtt*1000:.2f}ms)")
                        self.network_quality = new_quality
                        last_quality_change_time = current_time
                    
                    # 记录BBR状态
                    logger.info(f"BBR状态: {bbr_state['state']}, "
                               f"带宽: {bbr_bandwidth/1024/1024:.2f}MB/s, "
                               f"RTT: {bbr_rtt*1000:.2f}ms, "
                               f"拥塞窗口: {bbr_metrics.cwnd/1024:.1f}KB, "
                               f"在途数据: {bbr_metrics.inflight/1024:.1f}KB")
                else:
                    logger.warning("BBR指标不可用，使用传统网络测速")
                    # 如果BBR指标不可用，回退到传统方法
                    speed = self.measure_network_speed()
                    
                    # 根据网络速度调整质量
                    new_quality = self._determine_quality_from_speed(speed)
                    
                    current_time = time.time()
                    # 只有当质量变化且上次调整已经超过稳定期时才进行调整
                    if new_quality != self.network_quality and current_time - last_quality_change_time > quality_stability_period:
                        logger.info(f"传统网络质量变化: {self.network_quality} -> {new_quality} (速度: {speed/1000000:.2f} Mbps)")
                        self.network_quality = new_quality
                        last_quality_change_time = current_time
                
                # 每5秒检测一次
                time.sleep(5)
            except Exception as e:
                logger.error(f"监控网络质量时出错: {e}")
                time.sleep(5)
    
    def _determine_quality_from_bbr(self, bandwidth: float, rtt: float) -> str:
        """
        根据BBR指标确定网络质量 - 改进的质量判断逻辑
        
        Args:
            bandwidth: BBR带宽 (bytes/second)
            rtt: BBR RTT (seconds)
            
        Returns:
            网络质量等级
        """
        # 考虑带宽和RTT的综合影响
        if bandwidth > 8000000:  # 8 Mbps
            return "HIGH"
        elif bandwidth > 3000000:  # 3 Mbps
            return "MEDIUM"
        elif bandwidth > 1000000:  # 1 Mbps
            return "LOW"
        else:
            return "VERY_LOW"
    
    def _determine_quality_from_speed(self, speed: float) -> str:
        """
        根据传统网络测速确定网络质量
        
        Args:
            speed: 网络速度 (bytes/second)
            
        Returns:
            网络质量等级
        """
        if speed > 8000000:  # 8 Mbps
            return "HIGH"
        elif speed > 3000000:  # 3 Mbps
            return "MEDIUM"
        elif speed > 1000000:  # 1 Mbps
            return "LOW"
        else:
            return "VERY_LOW"
    
    def measure_network_speed(self):
        """测量当前网络速度 - 改进的实现"""
        global network_speed_cache
        
        # 如果缓存的测速结果不超过30秒，直接使用缓存
        current_time = time.time()
        if current_time - network_speed_cache["timestamp"] < 30:
            return network_speed_cache["speed"]
        
        try:
            # 使用更小的测试数据，避免缓冲区溢出
            test_size = 64 * 1024  # 64KB测试数据，减小数据包大小
            test_data = b'0' * test_size
            
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            
            # 设置发送缓冲区大小
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            
            # 分块发送数据，避免一次发送过多
            chunks = 10
            chunk_size = test_size // chunks
            
            start_time = time.time()
            for i in range(chunks):
                s.sendto(test_data[i*chunk_size:(i+1)*chunk_size], ('127.0.0.1', 12345))
            end_time = time.time()
            
            s.close()
            
            # 计算速度 (bytes/second)
            elapsed = end_time - start_time
            if elapsed > 0:
                speed = test_size / elapsed
                
                # 更新缓存
                network_speed_cache = {
                    "timestamp": current_time,
                    "speed": speed
                }
                
                return speed
            return 2000000  # 默认2Mbps
        except Exception as e:
            logger.error(f"测量网络速度时出错: {e}")
            return 2000000  # 默认2Mbps

    async def start_video_stream(self, stream_id):
        """通过 QUIC stream 发送视频流数据 - 改进的实现"""
        try:
            if not self.video_file_path:
                logger.error("没有设置视频文件路径")
                return
                
            input_file = self.video_file_path
            self.is_streaming = True
            
            # 根据网络质量选择编码参数
            quality = NETWORK_QUALITY[self.network_quality]
            video_bitrate = quality["video_bitrate"]
            video_scale = quality["video_scale"]
            audio_bitrate = quality["audio_bitrate"]
            
            logger.info(f"使用编码参数: 视频码率={video_bitrate}, 分辨率={video_scale}, 音频码率={audio_bitrate}")
            
            # 计算当前播放位置（秒）
            current_position = 0
            try:
                # 估算当前播放位置
                if hasattr(self, 'stream_start_time') and self.stream_start_time:
                    elapsed = time.time() - self.stream_start_time
                    current_position = max(0, elapsed - 2)  # 减去2秒缓冲时间
                    logger.info(f"估算当前播放位置: {current_position:.2f}秒")
            except Exception as e:
                logger.error(f"计算播放位置时出错: {e}")
            
            # 启动新的FFmpeg进程，保持相同的优化参数
            cmd = [
                'ffmpeg', 
                '-loglevel', 'info',  # 增加日志级别，帮助调试
                # 如果有播放位置，从该位置开始
                '-ss', f"{current_position:.2f}" if current_position > 0 else "0",
                '-i', input_file,
                '-f', 'mpegts',
                '-vcodec', 'libx264', 
                '-preset', 'ultrafast', 
                '-tune', 'zerolatency',
                '-b:v', video_bitrate, 
                '-vf', f'scale={video_scale}',
                '-pix_fmt', 'yuv420p',  # 确保使用兼容的像素格式
                '-g', '30',  # 设置GOP大小，每30帧一个关键帧
                '-x264-params', 'keyint=30:min-keyint=30',  # 强制关键帧间隔
                '-acodec', 'aac', 
                '-b:a', audio_bitrate,
                '-ar', '48000',  # 固定音频采样率
                '-ac', '2',      # 固定音频通道数
                '-async', '1',   # 音频同步
                '-vsync', '1',   # 使用简单的视频同步模式
                '-max_muxing_queue_size', '4096',  # 增加复用队列大小
                '-fflags', '+genpts',  # 生成PTS时间戳
                '-flags', '+global_header',   # 添加全局头信息
                '-flush_packets', '1',
                '-strict', 'experimental',    # 允许实验性编码器
                'pipe:1'
            ]
            
            logger.info("启动FFmpeg视频流服务... (pipe)")
            logger.info(" ".join(cmd))
            self.ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            
            # 记录流开始时间
            self.stream_start_time = time.time()
            
            # 定期检查网络质量并调整编码参数
            last_quality = self.network_quality
            
            # 初始缓冲区填充
            logger.info(f"正在填充初始缓冲区 ({INITIAL_BUFFER_SIZE/1024:.1f} KB)...")
            self.buffer = bytearray()
            
            # 填充初始缓冲区
            while len(self.buffer) < INITIAL_BUFFER_SIZE and self.is_streaming:
                chunk = self.ffmpeg_process.stdout.read(8192)
                if not chunk:
                    break
                self.buffer.extend(chunk)
            
            # 确保初始缓冲区从TS同步点开始
            if len(self.buffer) > 0:
                # 查找MPEG-TS同步字节
                ts_sync_byte = 0x47
                sync_pos = -1
                
                for i in range(len(self.buffer)):
                    if self.buffer[i] == ts_sync_byte:
                        # 验证是否是有效的TS包起始位置
                        if i + 188 <= len(self.buffer) and self.buffer[i + 188] == ts_sync_byte:
                            sync_pos = i
                            break
                
                if sync_pos > 0:
                    logger.info(f"调整初始缓冲区到TS同步点，跳过 {sync_pos} 字节")
                    self.buffer = self.buffer[sync_pos:]
            
            # 发送初始缓冲区数据
            if self.buffer and self.is_streaming:
                logger.info(f"发送初始缓冲区数据: {len(self.buffer)/1024:.1f} KB")
                # 分块发送大缓冲区数据，避免一次性发送过多
                chunk_size = 32 * 1024  # 32KB 是一个较好的数据块大小
                for i in range(0, len(self.buffer), chunk_size):
                    end = min(i + chunk_size, len(self.buffer))
                    self._quic.send_stream_data(stream_id, bytes(self.buffer[i:end]))
                    await asyncio.sleep(0.001)  # 短暂暂停，让接收方有时间处理
                self.buffer = bytearray()  # 清空缓冲区
                
                # 确保数据发送后，发送一个明确的通知
                try:
                    notify_stream_id = self._quic.get_next_available_stream_id()
                    self._quic.send_stream_data(notify_stream_id, b"DATA_SENT")
                    logger.info("已发送数据通知")
                except Exception as e:
                    logger.error(f"发送数据通知时出错: {e}")
            
            # 继续发送剩余数据
            data_count = 0
            last_send_time = time.time()
            read_buffer = bytearray()
            
            while self.is_streaming:
                # 读取数据到缓冲区
                chunk = self.ffmpeg_process.stdout.read(16384)  # 增加读取块大小
                if not chunk:
                    logger.info("FFmpeg数据流结束")
                    break
                
                # 直接发送数据，不再进行TS包检查
                try:
                    self._quic.send_stream_data(stream_id, chunk)
                    
                    # 更新BBR算法（基于发送的数据）
                    if len(chunk) > 0:
                        self.update_bbr_from_data(len(chunk))
                    
                    data_count += 1
                    
                    # 控制发送速率
                    current_time = time.time()
                    if current_time - last_send_time > 1.0:  # 每1秒记录一次
                        logger.info(f"已发送 {data_count} 个数据包")
                        last_send_time = current_time
                        data_count = 0
                        
                    # 短暂暂停，避免发送过快导致客户端缓冲区溢出
                    await asyncio.sleep(0.001)
                except Exception as e:
                    logger.error(f"发送数据时出错: {e}")
                    break
            
            logger.info("视频流发送完毕")
            
            # 发送一个明确的流结束标记，不仅仅是空数据包
            try:
                # 发送流结束标记
                self._quic.send_stream_data(stream_id, b"STREAM_END_MARKER")
                logger.info("已发送明确的流结束标记")
                
                # 短暂等待确保数据发送完成
                await asyncio.sleep(0.5)
                
                # 再发送一个空数据包作为结束标记
                self._quic.send_stream_data(stream_id, b"")
                logger.info("已发送流结束标记")
            except Exception as e:
                logger.error(f"发送流结束标记时出错: {e}")
            
            # 推流结束后关闭进程
            if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=5)
                logger.info("FFmpeg进程已关闭")
                
            # 停止网络监控
            self.stop_monitor = True
            
        except Exception as e:
            logger.error(f"启动视频流时出错: {e}")

    def cleanup(self):
        """清理服务器端资源 - 改进的实现"""
        logger.info("正在清理VideoServer资源...")
        
        # 停止监控
        self.stop_monitor = True
        
        # 停止网络监控线程
        if self.network_monitor_thread and self.network_monitor_thread.is_alive():
            self.network_monitor_thread.join(timeout=1)
        
        # 停止BBR监控
        if self.bbr_monitor:
            self.bbr_monitor.stop_monitoring()
        
        # 关闭ffmpeg进程
        if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
            try:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2)
            except:
                try:
                    self.ffmpeg_process.kill()
                except:
                    pass
        
        # 重置状态
        self.is_streaming = False
        self.stream_start_time = None
        
        logger.info("VideoServer资源清理完成")


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
    """启动服务器的函数，可以被其他程序调用 - 改进的实现"""
    global server_running, server_task

    # 创建BBR QUIC配置
    config = create_bbr_quic_configuration()
    config.is_client = False

    # 加载证书和密钥
    config.load_cert_chain("cert.pem", "key.pem")

    # 创建BBR服务器实例
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

    logger.info(f"BBR QUIC视频服务器已启动，监听端口4433，支持的ALPN协议: {ALPN_PROTOCOLS}")
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