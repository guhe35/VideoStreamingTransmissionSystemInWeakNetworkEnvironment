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
from typing import Optional

# 导入网络监控模块
from bbr_congestion_control import BBRMetrics
from quic_bbr_integration import BBRQuicProtocol, create_bbr_quic_configuration, BBRNetworkMonitor
from network_monitor import NetworkQualityMonitor, NetworkMetrics
from network_quality_config import get_network_quality_levels, get_network_quality_config
from network_quality_evaluator import evaluate_network_quality

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-server")

# 定义ALPN协议
ALPN_PROTOCOLS = ["quic-demo"]

# 全局变量用于控制服务器
server_running = False
server_task = None

# 使用统一的网络质量等级定义
NETWORK_QUALITY = {
    "ULTRA": get_network_quality_config("ULTRA"),
    "EXCELLENT": get_network_quality_config("EXCELLENT"),
    "GOOD": get_network_quality_config("GOOD"),
    "FAIR": get_network_quality_config("FAIR"),
    "POOR": get_network_quality_config("POOR"),
    "BAD": get_network_quality_config("BAD"),
    "CRITICAL": get_network_quality_config("CRITICAL")
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
        self.network_quality = "FAIR"  # 默认网络质量
        self.stop_monitor = False
        self.buffer = bytearray()  # 初始化缓冲区
        self.is_streaming = False  # 流传输状态
        self.stream_start_time = None
        
        # 网络监控器 - 使用NetworkMonitor
        self.network_monitor = NetworkQualityMonitor(
            target_host="127.0.0.1",
            target_port=12345,
            interval=2.0,  # 每2秒测量一次
            window_size=30,  # 保留30个历史数据点
            probe_count=5   # 每次测量5个探测包
        )
        
        # 自适应编码控制
        self.adaptive_encoding_enabled = True
        self.last_quality_change_time = time.time()
        self.quality_change_cooldown = 5.0  # 质量切换冷却时间（秒）
        
        # 网络质量历史记录
        self.quality_history = []
        self.max_quality_history = 10

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
                    self._process_client_feedback(message)
                except Exception as e:
                    logger.error(f"处理网络反馈时出错: {e}")
            
            elif message.startswith("BUFFER_WARNING:"):
                # 处理缓冲区警告 - 新增
                try:
                    warning_parts = message.split(":")
                    if len(warning_parts) >= 3:
                        current_buffer = int(warning_parts[1])
                        min_buffer = int(warning_parts[2])
                        
                        logger.warning(f"客户端缓冲区警告: 当前={current_buffer/1024:.1f}KB, "
                                     f"最小需求={min_buffer/1024:.1f}KB")
                        
                        # 如果缓冲区严重不足，主动降低质量
                        if current_buffer < min_buffer * 0.5:
                            current_quality = self.network_quality
                            quality_levels = list(NETWORK_QUALITY.keys())
                            current_level = quality_levels.index(current_quality) if current_quality in quality_levels else 3
                            if current_level < len(quality_levels) - 1:
                                new_quality = quality_levels[current_level + 1]
                                logger.info(f"缓冲区严重不足，主动降低质量到: {new_quality}")
                                self.network_quality = new_quality
                                if self.adaptive_encoding_enabled and self.is_streaming:
                                    self._adjust_encoding_parameters(new_quality)
                except Exception as e:
                    logger.error(f"处理缓冲区警告时出错: {e}")

    def _adjust_encoding_parameters(self, quality_level: str):
        """
        根据网络质量调整编码参数
        
        Args:
            quality_level: 网络质量等级
        """
        try:
            if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
                # 根据新的质量等级确定编码参数
                if quality_level == "EXCELLENT":
                    video_bitrate = "2000k"
                    video_scale = "1280:720"
                    audio_bitrate = "128k"
                elif quality_level == "GOOD":
                    video_bitrate = "1500k"
                    video_scale = "1280:720"
                    audio_bitrate = "96k"
                elif quality_level == "FAIR":
                    video_bitrate = "1000k"
                    video_scale = "854:480"
                    audio_bitrate = "64k"
                elif quality_level == "POOR":
                    video_bitrate = "500k"
                    video_scale = "640:360"
                    audio_bitrate = "48k"
                else:  # BAD
                    video_bitrate = "250k"
                    video_scale = "426:240"
                    audio_bitrate = "32k"
                
                quality_params = {
                    'video_bitrate': video_bitrate,
                    'video_scale': video_scale,
                    'audio_bitrate': audio_bitrate
                }
                
                logger.info(f"调整编码参数: {quality_level} - "
                          f"视频码率: {video_bitrate}, "
                          f"分辨率: {video_scale}, "
                          f"音频码率: {audio_bitrate}")
                
                # 实现动态调整编码参数的逻辑
                self._restart_ffmpeg_with_new_params(quality_params)
                
        except Exception as e:
            logger.error(f"调整编码参数时出错: {e}")
    
    def _restart_ffmpeg_with_new_params(self, quality_params: dict):
        """
        使用新参数重启FFmpeg进程
        
        Args:
            quality_params: 新的编码参数
        """
        try:
            # 保存当前播放位置
            current_position = 0
            if hasattr(self, 'stream_start_time') and self.stream_start_time:
                elapsed = time.time() - self.stream_start_time
                current_position = max(0, elapsed - 2)
            
            # 关闭当前FFmpeg进程
            if self.ffmpeg_process:
                self.ffmpeg_process.terminate()
                self.ffmpeg_process.wait(timeout=2)
            
            # 使用新参数重新启动FFmpeg
            cmd = [
                'ffmpeg', 
                '-loglevel', 'info',  # 增加日志级别，帮助调试
                # 从视频开头开始，确保不丢失开头
                '-ss', '0',  # 固定从0开始
                '-i', self.video_file_path,
                '-f', 'mpegts',
                '-vcodec', 'libx265',  # 更换为H.265编码器
                '-preset', 'ultrafast', 
                '-tune', 'zerolatency',
                '-b:v', quality_params['video_bitrate'], 
                '-vf', f'scale={quality_params["video_scale"]}',
                '-pix_fmt', 'yuv420p',  # 确保使用兼容的像素格式
                '-g', '30',  # 设置GOP大小，每30帧一个关键帧
                '-x265-params', 'keyint=30:min-keyint=30:bframes=0',  # H.265参数，禁用B帧以降低延迟
                '-acodec', 'aac', 
                '-b:a', quality_params['audio_bitrate'],
                '-ar', '48000',  # 固定音频采样率
                '-ac', '2',      # 固定音频通道数
                '-async', '1',   # 音频同步
                '-vsync', '1',   # 使用简单的视频同步模式
                '-max_muxing_queue_size', '4096',  # 增加复用队列大小
                '-fflags', '+genpts',  # 生成PTS时间戳
                '-flags', '+global_header',   # 添加全局头信息
                '-flush_packets', '1',
                '-strict', 'experimental',    # 允许实验性编码器
                '-movflags', '+faststart',    # 优化流媒体播放
                'pipe:1'
            ]
            
            logger.info("使用新参数重启FFmpeg: " + " ".join(cmd))
            self.ffmpeg_process = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            
            # 更新流开始时间
            self.stream_start_time = time.time()
            
        except Exception as e:
            logger.error(f"重启FFmpeg时出错: {e}")

    def monitor_network_quality(self):
        """使用NetworkMonitor监控网络质量并调整编码参数"""
        # 启动网络监控
        self.network_monitor.start()
        
        while not self.stop_monitor:
            try:
                # 获取当前网络指标
                current_metrics = self.network_monitor.get_current_metrics()
                
                if current_metrics:
                    # 根据NetworkMonitor指标确定网络质量
                    new_quality = self._determine_quality_from_network_metrics(current_metrics)
                    
                    current_time = time.time()
                    
                    # 检查质量变化冷却时间
                    if (new_quality != self.network_quality and 
                        current_time - self.last_quality_change_time > self.quality_change_cooldown):
                        
                        logger.info(f"网络质量变化: {self.network_quality} -> {new_quality} "
                                  f"(延迟: {current_metrics.latency:.1f}ms, "
                                  f"丢包率: {current_metrics.packet_loss:.2f}%, "
                                  f"抖动: {current_metrics.jitter:.1f}ms)")
                        
                        # 记录质量变化历史
                        self._record_quality_change(self.network_quality, new_quality, current_time)
                        
                        self.network_quality = new_quality
                        self.last_quality_change_time = current_time
                        
                        # 如果启用了自适应编码，调整编码参数
                        if self.adaptive_encoding_enabled and self.is_streaming:
                            self._adjust_encoding_parameters(new_quality)
                
                # 每2秒检测一次
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"监控网络质量时出错: {e}")
                time.sleep(2)
        
        # 停止网络监控
        self.network_monitor.stop()
    
    def _determine_quality_from_network_metrics(self, metrics: NetworkMetrics) -> str:
        """
        根据NetworkMonitor指标确定网络质量 - 使用统一的评估函数
        
        Args:
            metrics: NetworkMonitor网络指标
            
        Returns:
            网络质量等级
        """
        # 使用统一的网络质量评估函数
        quality = evaluate_network_quality(metrics)
        
        logger.info(f"网络质量评估结果: {quality} "
                   f"(延迟: {metrics.latency:.1f}ms, "
                   f"丢包率: {metrics.packet_loss:.2f}%, "
                   f"抖动: {metrics.jitter:.1f}ms, "
                   f"带宽: {metrics.bandwidth/1000000:.2f}Mbps)")
        
        return quality
    
    def _record_quality_change(self, old_quality: str, new_quality: str, timestamp: float):
        """
        记录质量变化历史
        
        Args:
            old_quality: 旧质量等级
            new_quality: 新质量等级
            timestamp: 变化时间戳
        """
        change_record = {
            'old_quality': old_quality,
            'new_quality': new_quality,
            'timestamp': timestamp,
            'direction': self._get_quality_change_direction(old_quality, new_quality)
        }
        
        self.quality_history.append(change_record)
        
        # 限制历史记录大小
        if len(self.quality_history) > self.max_quality_history:
            self.quality_history.pop(0)
        
        logger.info(f"记录质量变化: {old_quality} -> {new_quality} ({change_record['direction']})")
    
    def _get_quality_change_direction(self, old_quality: str, new_quality: str) -> str:
        """
        获取质量变化方向
        
        Args:
            old_quality: 旧质量等级
            new_quality: 新质量等级
            
        Returns:
            变化方向字符串
        """
        quality_levels = ["EXCELLENT", "GOOD", "FAIR", "POOR", "BAD"]
        old_index = quality_levels.index(old_quality)
        new_index = quality_levels.index(new_quality)
        
        if new_index < old_index:
            return "升级"
        elif new_index > old_index:
            return "降级"
        else:
            return "不变"

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
            
            # 计算当前播放位置（秒）- 修复开头丢失问题
            current_position = 0  # 始终从0开始，确保不丢失开头
            try:
                # 移除播放位置估算，避免丢失开头
                # 如果需要从特定位置开始，可以在这里设置
                # current_position = 0  # 确保从视频开头开始
                logger.info(f"从视频开头开始播放 (位置: {current_position:.2f}秒)")
            except Exception as e:
                logger.error(f"计算播放位置时出错: {e}")
            
            # 启动新的FFmpeg进程，保持相同的优化参数
            cmd = [
                'ffmpeg', 
                '-loglevel', 'info',  # 增加日志级别，帮助调试
                # 从视频开头开始，确保不丢失开头
                '-ss', '0',  # 固定从0开始
                '-i', input_file,
                '-f', 'mpegts',
                '-vcodec', 'libx265',  # 更换为H.265编码器
                '-preset', 'ultrafast', 
                '-tune', 'zerolatency',
                '-b:v', video_bitrate, 
                '-vf', f'scale={video_scale}',
                '-pix_fmt', 'yuv420p',  # 确保使用兼容的像素格式
                '-g', '30',  # 设置GOP大小，每30帧一个关键帧
                '-x265-params', 'keyint=30:min-keyint=30:bframes=0',  # H.265参数，禁用B帧以降低延迟
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
                '-movflags', '+faststart',    # 优化流媒体播放
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
                    
                    # 更新算法（基于发送的数据）
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
        """清理服务器端资源"""
        logger.info("正在清理VideoServer资源...")
        
        # 停止监控
        self.stop_monitor = True
        
        # 停止网络监控
        if self.network_monitor:
            self.network_monitor.stop()
        
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


    
    def _process_client_feedback(self, message: str):
        """
        处理客户端反馈消息
        
        Args:
            message: 客户端反馈消息
        """
        try:
            feedback_parts = message.split(":")
            if len(feedback_parts) >= 2:
                quality_level = feedback_parts[1].strip()
                
                # 解析详细的反馈信息
                if len(feedback_parts) >= 5:
                    try:
                        latency = float(feedback_parts[2])
                        packet_loss = float(feedback_parts[3])
                        buffer_size = int(feedback_parts[4])
                        
                        logger.info(f"收到客户端网络反馈: 质量={quality_level}, "
                                  f"延迟={latency:.2f}ms, "
                                  f"丢包率={packet_loss:.2f}%, "
                                  f"缓冲区={buffer_size/1024:.1f}KB")
                        
                        # 应用客户端反馈
                        if quality_level in ["EXCELLENT", "GOOD", "FAIR", "POOR", "BAD"]:
                            self.network_quality = quality_level
                            logger.info(f"根据客户端反馈调整网络质量为: {quality_level}")
                            
                            # 如果启用了自适应编码，立即调整编码参数
                            if self.adaptive_encoding_enabled and self.is_streaming:
                                self._adjust_encoding_parameters(quality_level)
                        
                    except (ValueError, IndexError):
                        logger.warning("无法解析详细反馈信息，使用基本质量等级")
                        if quality_level in ["EXCELLENT", "GOOD", "FAIR", "POOR", "BAD"]:
                            self.network_quality = quality_level
                            logger.info(f"根据客户端反馈调整网络质量为: {quality_level}")
        except Exception as e:
            logger.error(f"处理客户端反馈时出错: {e}")


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

        logger.info(f"原始视频: {width}x{height}, 原始编解码器: {codec_name}, 帧率: {fps}fps, 时长: {duration:.2f}秒")
        logger.info(f"传输编码: H.265 (HEVC) - 使用libx265编码器")
        if audio_stream:
            logger.info(f"原始音频: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")

        return video_info

    except Exception as e:
        logger.error(f"分析视频时出错: {e}")
        return None


async def start_server():
    """启动服务器的函数，可以被其他程序调用 - 改进的实现"""
    global server_running, server_task

    # 创建QUIC配置
    config = create_bbr_quic_configuration()
    config.is_client = False

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