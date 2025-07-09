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

# 网络质量等级定义 - 统一的5级弱网环境评估系统（H.265优化）
NETWORK_QUALITY = {
    "WEAK_GOOD": {
        "video_bitrate": "800k", 
        "video_scale": "640:360", 
        "audio_bitrate": "64k", 
        "min_speed": 1000000,      # 1 Mbps
        "min_rtt": 0,
        "max_rtt": 100,            # RTT <= 100ms
        "max_loss_rate": 0.01,     # 丢包率 <= 1%
        "description": "弱网良好"
    },
    "WEAK_MEDIUM": {
        "video_bitrate": "400k", 
        "video_scale": "480:270", 
        "audio_bitrate": "48k", 
        "min_speed": 500000,       # 500 Kbps
        "min_rtt": 100,
        "max_rtt": 200,            # 100ms < RTT <= 200ms
        "max_loss_rate": 0.02,     # 丢包率 <= 2%
        "description": "弱网中等"
    },
    "WEAK_POOR": {
        "video_bitrate": "200k", 
        "video_scale": "320:180", 
        "audio_bitrate": "32k", 
        "min_speed": 250000,       # 250 Kbps
        "min_rtt": 200,
        "max_rtt": 400,            # 200ms < RTT <= 400ms
        "max_loss_rate": 0.05,     # 丢包率 <= 5%
        "description": "弱网较差"
    },
    "WEAK_VERY_POOR": {
        "video_bitrate": "100k", 
        "video_scale": "240:135", 
        "audio_bitrate": "24k", 
        "min_speed": 100000,       # 100 Kbps
        "min_rtt": 400,
        "max_rtt": 800,            # 400ms < RTT <= 800ms
        "max_loss_rate": 0.1,      # 丢包率 <= 10%
        "description": "弱网极差"
    },
    "WEAK_CRITICAL": {
        "video_bitrate": "50k", 
        "video_scale": "160:90", 
        "audio_bitrate": "16k", 
        "min_speed": 0,            # < 100 Kbps
        "min_rtt": 800,
        "max_rtt": float('inf'),   # RTT > 800ms
        "max_loss_rate": 1.0,      # 丢包率 > 10%
        "description": "弱网临界"
    }
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
        self.network_quality = "WEAK_MEDIUM"  # 默认网络质量
        self.network_monitor_thread = None
        self.stop_monitor = False
        self.buffer = bytearray()  # 初始化缓冲区
        self.is_streaming = False  # 流传输状态
        self.stream_start_time = None
        
        # 网络监控器 - 改进的实现
        self.bbr_monitor = BBRNetworkMonitor()
        self.bbr_monitor.start_monitoring(self)
        
        # 指标跟踪 - 新增
        self.bbr_metrics_history = []
        self.last_bbr_update = time.time()
        self.adaptive_encoding_enabled = True  # 启用自适应编码
        
        # 预测性调整 - 新增
        self.quality_prediction_enabled = True
        self.quality_trend_history = []
        self.max_trend_history = 20
        self.prediction_threshold = 0.7  # 预测置信度阈值
        
        # 稳定性机制 - 新增
        self.quality_stability_enabled = True
        self.quality_stability_threshold = 3  # 需要连续3次相同结果才切换
        self.quality_candidate = None  # 候选质量等级
        self.quality_candidate_count = 0  # 候选质量等级连续出现次数
        self.quality_change_cooldown = 8.0  # 质量切换冷却时间（秒）
        self.last_quality_change_time = time.time()
        self.quality_hysteresis = True  # 启用迟滞机制
        self.quality_change_history = []  # 质量变化历史
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
            
            elif message.startswith("NETWORK_FEEDBACK:") or message.startswith("ENHANCED_FEEDBACK:"):
                # 接收客户端的网络反馈 - 支持增强反馈
                try:
                    if message.startswith("ENHANCED_FEEDBACK:"):
                        # 处理增强反馈
                        self._process_enhanced_feedback(message)
                    else:
                        # 处理传统反馈（向后兼容）
                        self._process_traditional_feedback(message)
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
        根据网络质量调整编码参数 - 优化实现
        
        Args:
            quality_level: 网络质量等级
        """
        try:
            if self.ffmpeg_process and self.ffmpeg_process.poll() is None:
                # 获取新的编码参数
                quality_params = NETWORK_QUALITY.get(quality_level, NETWORK_QUALITY["WEAK_MEDIUM"])
                
                logger.info(f"调整编码参数: {quality_level} - "
                          f"视频码率: {quality_params['video_bitrate']}, "
                          f"分辨率: {quality_params['video_scale']}, "
                          f"音频码率: {quality_params['audio_bitrate']}")
                
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
        """监控网络质量并调整编码参数 - 优化实现"""
        last_quality_change_time = time.time()
        quality_stability_period = 8  # 减少稳定期到8秒，提高响应速度
        prediction_check_interval = 15  # 每15秒检查一次预测
        last_prediction_check = time.time()
        
        while not self.stop_monitor:
            try:
                # 获取算法指标 - 改进的实现
                bbr_metrics = self.get_bbr_metrics()
                if bbr_metrics:
                    # 使用算法带宽估算
                    current_speed = bbr_metrics.bandwidth
                    current_rtt = bbr_metrics.rtt * 1000  # 转换为毫秒
                    
                    # 保存当前指标用于预测
                    self.current_bandwidth = current_speed
                    self.current_rtt = current_rtt
                    
                    # 记录指标历史（后台使用）
                    self.bbr_metrics_history.append({
                        'timestamp': time.time(),
                        'bandwidth': current_speed,
                        'rtt': current_rtt,
                        'cwnd': bbr_metrics.cwnd,
                        'inflight': bbr_metrics.inflight
                    })
                    
                    # 限制历史记录大小
                    if len(self.bbr_metrics_history) > 100:
                        self.bbr_metrics_history.pop(0)
                    
                    # 根据带宽确定质量级别
                    raw_quality = self._determine_quality_from_bbr(bbr_metrics)
                    
                    current_time = time.time()
                    
                    # 检查预测性调整
                    if (self.quality_prediction_enabled and 
                        current_time - last_prediction_check > prediction_check_interval):
                        predicted_quality = self._predict_quality_trend()
                        if predicted_quality and predicted_quality != raw_quality:
                            # 如果预测的质量与当前检测的质量不同，考虑提前调整
                            logger.info(f"预测性调整: 当前检测 {raw_quality}, 预测 {predicted_quality}")
                            # 可以选择使用预测的质量或当前检测的质量
                            # 这里使用预测的质量，但增加额外的稳定性检查
                            if abs(current_speed - self.quality_trend_history[-1]['bandwidth']) < current_speed * 0.1:
                                raw_quality = predicted_quality
                        last_prediction_check = current_time
                    
                    # 应用稳定性机制
                    new_quality = self._apply_quality_stability(raw_quality, current_time)
                    
                    # 只有当质量实际发生变化时才进行调整
                    if new_quality != self.network_quality:
                        logger.info(f"网络质量变化: {self.network_quality} -> {new_quality} "
                                  f"(带宽: {current_speed/1000000:.2f} Mbps, RTT: {current_rtt:.2f}ms)")
                        
                        # 更新质量趋势历史
                        self._update_quality_trend_history(new_quality)
                        
                        # 记录质量变化历史
                        self._record_quality_change(self.network_quality, new_quality, current_time)
                        
                        self.network_quality = new_quality
                        self.last_quality_change_time = current_time
                        
                        # 如果启用了自适应编码，调整编码参数
                        if self.adaptive_encoding_enabled:
                            self._adjust_encoding_parameters(new_quality)
                else:
                    # 使用传统网络测速方法作为备选
                    logger.info("使用传统网络测速方法")
                    speed = self.measure_network_speed()
                    
                    # 根据网络速度调整质量
                    new_quality = self._determine_quality_from_speed(speed)
                    
                    current_time = time.time()
                    # 只有当质量变化且上次调整已经超过稳定期时才进行调整
                    if new_quality != self.network_quality and current_time - last_quality_change_time > quality_stability_period:
                        logger.info(f"传统网络质量变化: {self.network_quality} -> {new_quality} (速度: {speed/1000000:.2f} Mbps)")
                        
                        # 更新质量趋势历史
                        self._update_quality_trend_history(new_quality)
                        
                        self.network_quality = new_quality
                        last_quality_change_time = current_time
                        
                        # 如果启用了自适应编码，调整编码参数
                        if self.adaptive_encoding_enabled:
                            self._adjust_encoding_parameters(new_quality)
                
                # 每4秒检测一次（提高检测频率）
                time.sleep(4)
            except Exception as e:
                logger.error(f"监控网络质量时出错: {e}")
                time.sleep(4)
    
    def _determine_quality_from_bbr(self, bbr_metrics: BBRMetrics) -> str:
        """
        根据BBR算法指标确定网络质量 - 综合评估实现
        
        Args:
            bbr_metrics: BBR算法指标
            
        Returns:
            网络质量等级
        """
        bandwidth = bbr_metrics.bandwidth  # bytes/second
        rtt = bbr_metrics.rtt * 1000  # 转换为毫秒
        
        # 获取丢包率和其他拥塞信号
        loss_rate = getattr(bbr_metrics, 'loss_rate', 0.0)
        
        # 综合评估：带宽 + RTT + 丢包率
        quality_score = 0
        
        # 带宽评分 (40%)
        if bandwidth >= 1000000:      # >= 1 Mbps
            quality_score += 40
        elif bandwidth >= 500000:     # >= 500 Kbps
            quality_score += 30
        elif bandwidth >= 250000:     # >= 250 Kbps
            quality_score += 20
        elif bandwidth >= 100000:     # >= 100 Kbps
            quality_score += 10
        else:                         # < 100 Kbps
            quality_score += 0
        
        # RTT评分 (35%)
        if rtt <= 100:                # RTT <= 100ms
            quality_score += 35
        elif rtt <= 200:              # RTT <= 200ms
            quality_score += 28
        elif rtt <= 400:              # RTT <= 400ms
            quality_score += 21
        elif rtt <= 800:              # RTT <= 800ms
            quality_score += 14
        else:                         # RTT > 800ms
            quality_score += 0
        
        # 丢包率评分 (25%)
        if loss_rate <= 0.01:         # 丢包率 <= 1%
            quality_score += 25
        elif loss_rate <= 0.02:       # 丢包率 <= 2%
            quality_score += 20
        elif loss_rate <= 0.05:       # 丢包率 <= 5%
            quality_score += 15
        elif loss_rate <= 0.1:        # 丢包率 <= 10%
            quality_score += 10
        else:                         # 丢包率 > 10%
            quality_score += 0
        
        # 根据综合评分确定质量等级
        if quality_score >= 85:       # 85-100分
            return "WEAK_GOOD"
        elif quality_score >= 65:     # 65-84分
            return "WEAK_MEDIUM"
        elif quality_score >= 45:     # 45-64分
            return "WEAK_POOR"
        elif quality_score >= 25:     # 25-44分
            return "WEAK_VERY_POOR"
        else:                         # 0-24分
            return "WEAK_CRITICAL"
    
    def _determine_quality_from_speed(self, speed: float) -> str:
        """
        根据传统网络测速确定网络质量 - 统一评估实现
        
        Args:
            speed: 网络速度 (bytes/second)
            
        Returns:
            网络质量等级
        """
        # 仅基于带宽的简化评估（传统测速缺乏RTT和丢包率信息）
        if speed >= 1000000:    # >= 1 Mbps
            return "WEAK_GOOD"
        elif speed >= 500000:   # >= 500 Kbps
            return "WEAK_MEDIUM"
        elif speed >= 250000:   # >= 250 Kbps
            return "WEAK_POOR"
        elif speed >= 100000:   # >= 100 Kbps
            return "WEAK_VERY_POOR"
        else:                   # < 100 Kbps
            return "WEAK_CRITICAL"
    
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

    def _predict_quality_trend(self) -> Optional[str]:
        """
        预测网络质量变化趋势 - 增强的预测算法
        
        Returns:
            预测的质量等级，如果无法预测则返回None
        """
        if len(self.quality_trend_history) < 8:  # 需要更多历史数据
            return None
        
        try:
            # 使用多种预测方法的组合
            trend_prediction = self._predict_by_trend_analysis()
            pattern_prediction = self._predict_by_pattern_recognition()
            bandwidth_prediction = self._predict_by_bandwidth_trend()
            rtt_prediction = self._predict_by_rtt_trend()
            
            # 预测结果投票机制
            predictions = [p for p in [trend_prediction, pattern_prediction, bandwidth_prediction, rtt_prediction] if p is not None]
            
            if not predictions:
                return None
            
            # 统计每个预测结果的投票数
            prediction_votes = {}
            for prediction in predictions:
                prediction_votes[prediction] = prediction_votes.get(prediction, 0) + 1
            
            # 选择投票数最多的预测结果
            max_votes = max(prediction_votes.values())
            if max_votes >= 2:  # 至少有2种方法同意
                for prediction, votes in prediction_votes.items():
                    if votes == max_votes:
                        logger.info(f"预测结果 (投票: {votes}/{len(predictions)}): {prediction}")
                        return prediction
            
            return None
            
        except Exception as e:
            logger.error(f"预测质量趋势时出错: {e}")
            return None
    
    def _predict_by_trend_analysis(self) -> Optional[str]:
        """
        基于趋势分析的预测 - 新增
        
        Returns:
            预测的质量等级
        """
        recent_trends = self.quality_trend_history[-10:]
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        
        # 计算质量变化趋势
        improvements = 0
        degradations = 0
        
        for i in range(1, len(recent_trends)):
            prev_quality = recent_trends[i-1]['quality']
            curr_quality = recent_trends[i]['quality']
            
            prev_level = quality_levels.index(prev_quality) if prev_quality in quality_levels else 2
            curr_level = quality_levels.index(curr_quality) if curr_quality in quality_levels else 2
            
            if curr_level < prev_level:
                improvements += 1
            elif curr_level > prev_level:
                degradations += 1
        
        total_changes = improvements + degradations
        if total_changes == 0:
            return None
        
        if improvements / total_changes > 0.7:
            # 预测质量提升
            current_level = quality_levels.index(self.network_quality)
            if current_level > 0:
                return quality_levels[current_level - 1]
        elif degradations / total_changes > 0.7:
            # 预测质量下降
            current_level = quality_levels.index(self.network_quality)
            if current_level < len(quality_levels) - 1:
                return quality_levels[current_level + 1]
        
        return None
    
    def _predict_by_pattern_recognition(self) -> Optional[str]:
        """
        基于模式识别的预测 - 新增
        
        Returns:
            预测的质量等级
        """
        if len(self.quality_trend_history) < 12:
            return None
        
        # 检查周期性模式
        recent_history = self.quality_trend_history[-12:]
        
        # 检查是否有规律的质量变化模式
        quality_sequence = [item['quality'] for item in recent_history]
        
        # 检查短周期模式（3-4个数据点的重复）
        for period in [3, 4, 6]:
            if len(quality_sequence) >= period * 2:
                pattern = quality_sequence[-period:]
                previous_pattern = quality_sequence[-period*2:-period]
                
                # 如果模式重复，预测下一个值
                if pattern == previous_pattern:
                    next_index = len(quality_sequence) % period
                    if next_index < len(pattern):
                        predicted_quality = pattern[next_index]
                        logger.debug(f"检测到周期性模式 (周期: {period}): {predicted_quality}")
                        return predicted_quality
        
        return None
    
    def _predict_by_bandwidth_trend(self) -> Optional[str]:
        """
        基于带宽趋势的预测 - 新增
        
        Returns:
            预测的质量等级
        """
        if len(self.quality_trend_history) < 6:
            return None
        
        recent_trends = self.quality_trend_history[-6:]
        bandwidths = [item['bandwidth'] for item in recent_trends]
        
        # 计算带宽变化率
        bandwidth_changes = []
        for i in range(1, len(bandwidths)):
            if bandwidths[i-1] > 0:
                change_rate = (bandwidths[i] - bandwidths[i-1]) / bandwidths[i-1]
                bandwidth_changes.append(change_rate)
        
        if not bandwidth_changes:
            return None
        
        # 计算平均变化率
        avg_change_rate = sum(bandwidth_changes) / len(bandwidth_changes)
        
        # 预测下一时刻的带宽
        current_bandwidth = bandwidths[-1]
        predicted_bandwidth = current_bandwidth * (1 + avg_change_rate)
        
        # 根据预测带宽确定质量等级
        return self._bandwidth_to_quality(predicted_bandwidth)
    
    def _predict_by_rtt_trend(self) -> Optional[str]:
        """
        基于RTT趋势的预测 - 新增
        
        Returns:
            预测的质量等级
        """
        if len(self.quality_trend_history) < 6:
            return None
        
        recent_trends = self.quality_trend_history[-6:]
        rtts = [item['rtt'] for item in recent_trends]
        
        # 计算RTT变化率
        rtt_changes = []
        for i in range(1, len(rtts)):
            if rtts[i-1] > 0:
                change_rate = (rtts[i] - rtts[i-1]) / rtts[i-1]
                rtt_changes.append(change_rate)
        
        if not rtt_changes:
            return None
        
        # 计算平均变化率
        avg_change_rate = sum(rtt_changes) / len(rtt_changes)
        
        # 预测下一时刻的RTT
        current_rtt = rtts[-1]
        predicted_rtt = current_rtt * (1 + avg_change_rate)
        
        # 根据预测RTT调整质量预测
        if predicted_rtt > current_rtt * 1.2:  # RTT显著增加
            return self._degrade_quality(self.network_quality)
        elif predicted_rtt < current_rtt * 0.8:  # RTT显著减少
            return self._improve_quality(self.network_quality)
        
        return None
    
    def _bandwidth_to_quality(self, bandwidth: float) -> str:
        """
        根据带宽确定质量等级 - 新增
        
        Args:
            bandwidth: 带宽 (bytes/second)
            
        Returns:
            质量等级
        """
        if bandwidth >= 1000000:
            return "WEAK_GOOD"
        elif bandwidth >= 500000:
            return "WEAK_MEDIUM"
        elif bandwidth >= 250000:
            return "WEAK_POOR"
        elif bandwidth >= 100000:
            return "WEAK_VERY_POOR"
        else:
            return "WEAK_CRITICAL"
    
    def _improve_quality(self, current_quality: str) -> Optional[str]:
        """
        提升质量等级 - 新增
        
        Args:
            current_quality: 当前质量等级
            
        Returns:
            提升后的质量等级
        """
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        current_index = quality_levels.index(current_quality)
        if current_index > 0:
            return quality_levels[current_index - 1]
        return None
    
    def _degrade_quality(self, current_quality: str) -> Optional[str]:
        """
        降低质量等级 - 新增
        
        Args:
            current_quality: 当前质量等级
            
        Returns:
            降低后的质量等级
        """
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        current_index = quality_levels.index(current_quality)
        if current_index < len(quality_levels) - 1:
            return quality_levels[current_index + 1]
        return None
    
    def _update_quality_trend_history(self, new_quality: str):
        """
        更新质量变化历史 - 新增
        
        Args:
            new_quality: 新的质量等级
        """
        try:
            trend_record = {
                'timestamp': time.time(),
                'quality': new_quality,
                'bandwidth': getattr(self, 'current_bandwidth', 0),
                'rtt': getattr(self, 'current_rtt', 0)
            }
            
            self.quality_trend_history.append(trend_record)
            
            # 限制历史记录大小
            if len(self.quality_trend_history) > self.max_trend_history:
                self.quality_trend_history.pop(0)
                
        except Exception as e:
            logger.error(f"更新质量趋势历史时出错: {e}")
    
    def _apply_quality_stability(self, raw_quality: str, current_time: float) -> str:
        """
        应用质量稳定性机制 - 新增
        
        Args:
            raw_quality: 原始质量检测结果
            current_time: 当前时间
            
        Returns:
            经过稳定性机制处理的质量等级
        """
        if not self.quality_stability_enabled:
            return raw_quality
        
        # 检查冷却时间
        if current_time - self.last_quality_change_time < self.quality_change_cooldown:
            logger.debug(f"质量切换冷却中，剩余时间: {self.quality_change_cooldown - (current_time - self.last_quality_change_time):.1f}秒")
            return self.network_quality  # 保持当前质量
        
        # 如果原始质量与当前质量相同，重置候选计数
        if raw_quality == self.network_quality:
            self.quality_candidate = None
            self.quality_candidate_count = 0
            return self.network_quality
        
        # 应用迟滞机制
        if self.quality_hysteresis:
            adjusted_quality = self._apply_hysteresis(raw_quality)
        else:
            adjusted_quality = raw_quality
        
        # 如果调整后的质量与当前质量相同，不需要切换
        if adjusted_quality == self.network_quality:
            return self.network_quality
        
        # 更新候选质量
        if adjusted_quality == self.quality_candidate:
            self.quality_candidate_count += 1
        else:
            self.quality_candidate = adjusted_quality
            self.quality_candidate_count = 1
        
        # 检查是否达到稳定性阈值
        if self.quality_candidate_count >= self.quality_stability_threshold:
            logger.info(f"质量稳定性阈值达到: {self.quality_candidate} (连续{self.quality_candidate_count}次)")
            self.quality_candidate = None
            self.quality_candidate_count = 0
            return adjusted_quality
        else:
            logger.debug(f"质量候选: {self.quality_candidate} (连续{self.quality_candidate_count}/{self.quality_stability_threshold}次)")
            return self.network_quality  # 保持当前质量直到达到阈值
    
    def _apply_hysteresis(self, raw_quality: str) -> str:
        """
        应用迟滞机制避免在边界附近频繁切换 - 新增
        
        Args:
            raw_quality: 原始质量等级
            
        Returns:
            经过迟滞处理的质量等级
        """
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        current_index = quality_levels.index(self.network_quality)
        raw_index = quality_levels.index(raw_quality)
        
        # 如果质量变化幅度很小（相邻等级），需要更强的信号才切换
        if abs(raw_index - current_index) == 1:
            # 检查最近的质量变化历史
            if len(self.quality_change_history) > 0:
                recent_changes = [change for change in self.quality_change_history 
                                if time.time() - change['timestamp'] < 30.0]  # 最近30秒
                
                # 如果最近变化过于频繁，增加稳定性要求
                if len(recent_changes) >= 3:
                    logger.debug("检测到频繁质量变化，增加稳定性要求")
                    return self.network_quality  # 保持当前质量
        
        return raw_quality
    
    def _record_quality_change(self, old_quality: str, new_quality: str, timestamp: float):
        """
        记录质量变化历史 - 新增
        
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
        
        self.quality_change_history.append(change_record)
        
        # 限制历史记录大小
        if len(self.quality_change_history) > self.max_quality_history:
            self.quality_change_history.pop(0)
        
        logger.info(f"记录质量变化: {old_quality} -> {new_quality} ({change_record['direction']})")
    
    def _get_quality_change_direction(self, old_quality: str, new_quality: str) -> str:
        """
        获取质量变化方向 - 新增
        
        Args:
            old_quality: 旧质量等级
            new_quality: 新质量等级
            
        Returns:
            变化方向字符串
        """
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        old_index = quality_levels.index(old_quality)
        new_index = quality_levels.index(new_quality)
        
        if new_index < old_index:
            return "升级"
        elif new_index > old_index:
            return "降级"
        else:
            return "不变"
    
    def _process_enhanced_feedback(self, message: str):
        """
        处理增强反馈消息 - 新增
        
        Args:
            message: 增强反馈消息
        """
        try:
            parts = message.split(":")
            if len(parts) < 9:
                logger.warning("增强反馈消息格式不完整")
                return
            
            # 解析基本信息
            quality_level = parts[1]
            bandwidth = float(parts[2])
            rtt = float(parts[3])
            buffer_size = int(parts[4])
            feedback_weight = float(parts[5])
            validation_score = float(parts[6])
            quality_stability = float(parts[7])
            measurement_confidence = float(parts[8])
            
            # 解析BBR指标（如果可用）
            bbr_metrics = {}
            if len(parts) >= 13:
                bbr_metrics = {
                    'bandwidth': float(parts[9]),
                    'rtt': float(parts[10]),
                    'loss_rate': float(parts[11]),
                    'confidence': float(parts[12])
                }
            
            logger.info(f"收到增强网络反馈: 质量={quality_level}, "
                       f"带宽={bandwidth/1000000:.2f}Mbps, "
                       f"RTT={rtt:.2f}ms, "
                       f"权重={feedback_weight:.2f}, "
                       f"验证分数={validation_score:.2f}")
            
            # 验证反馈质量
            if validation_score < 0.5:
                logger.warning(f"反馈验证分数过低 ({validation_score:.2f})，降低权重")
                feedback_weight *= 0.5
            
            if measurement_confidence < 0.3:
                logger.warning(f"测量置信度过低 ({measurement_confidence:.2f})，降低权重")
                feedback_weight *= 0.7
            
            # 应用权重机制处理反馈
            if feedback_weight > 0.7:
                # 高权重反馈，直接应用
                self._apply_client_feedback(quality_level, bandwidth, rtt, buffer_size, "高权重")
            elif feedback_weight > 0.4:
                # 中等权重反馈，需要与本地评估结合
                local_quality = self.network_quality
                if self._should_trust_client_feedback(quality_level, local_quality, feedback_weight):
                    self._apply_client_feedback(quality_level, bandwidth, rtt, buffer_size, "中等权重")
                else:
                    logger.info(f"客户端反馈 {quality_level} 与本地评估 {local_quality} 差异过大，使用本地评估")
            else:
                # 低权重反馈，仅作参考
                logger.info(f"客户端反馈权重过低 ({feedback_weight:.2f})，仅作参考")
                self._log_feedback_reference(quality_level, bandwidth, rtt)
            
            # 如果有BBR指标，进行交叉验证
            if bbr_metrics:
                self._cross_validate_with_bbr(bbr_metrics, bandwidth, rtt)
            
        except (ValueError, IndexError) as e:
            logger.error(f"解析增强反馈消息时出错: {e}")
    
    def _process_traditional_feedback(self, message: str):
        """
        处理传统反馈消息 - 新增（向后兼容）
        
        Args:
            message: 传统反馈消息
        """
        try:
            feedback_parts = message.split(":")
            if len(feedback_parts) >= 2:
                quality_level = feedback_parts[1].strip()
                
                # 解析详细的反馈信息
                if len(feedback_parts) >= 5:
                    try:
                        bandwidth = float(feedback_parts[2])
                        rtt = float(feedback_parts[3])
                        buffer_size = int(feedback_parts[4])
                        
                        logger.info(f"收到传统网络反馈: 质量={quality_level}, "
                                  f"带宽={bandwidth/1000000:.2f}Mbps, "
                                  f"RTT={rtt:.2f}ms, "
                                  f"缓冲区={buffer_size/1024:.1f}KB")
                        
                        # 传统反馈使用默认权重
                        self._apply_client_feedback(quality_level, bandwidth, rtt, buffer_size, "传统反馈")
                        
                    except (ValueError, IndexError):
                        logger.warning("无法解析详细反馈信息，使用基本质量等级")
                        if quality_level in NETWORK_QUALITY:
                            self.network_quality = quality_level
                            logger.info(f"根据客户端反馈调整网络质量为: {quality_level}")
        except Exception as e:
            logger.error(f"处理传统反馈时出错: {e}")
    
    def _apply_client_feedback(self, quality_level: str, bandwidth: float, rtt: float, buffer_size: int, feedback_type: str):
        """
        应用客户端反馈 - 新增
        
        Args:
            quality_level: 质量等级
            bandwidth: 带宽
            rtt: RTT
            buffer_size: 缓冲区大小
            feedback_type: 反馈类型
        """
        # 根据缓冲区状态调整策略
        if buffer_size < 256 * 1024:  # 缓冲区不足
            logger.warning("客户端缓冲区不足，考虑降低质量")
            # 可以主动降低质量等级
            if quality_level in NETWORK_QUALITY:
                quality_levels = list(NETWORK_QUALITY.keys())
                current_level = quality_levels.index(quality_level)
                if current_level < len(quality_levels) - 1:
                    quality_level = quality_levels[current_level + 1]
                    logger.info(f"主动降低质量到: {quality_level}")
        
        if quality_level in NETWORK_QUALITY:
            self.network_quality = quality_level
            logger.info(f"根据客户端反馈调整网络质量为: {quality_level} ({feedback_type})")
            
            # 如果启用了自适应编码，立即调整编码参数
            if self.adaptive_encoding_enabled and self.is_streaming:
                self._adjust_encoding_parameters(quality_level)
    
    def _should_trust_client_feedback(self, client_quality: str, local_quality: str, feedback_weight: float) -> bool:
        """
        判断是否应该信任客户端反馈 - 新增
        
        Args:
            client_quality: 客户端评估的质量
            local_quality: 本地评估的质量
            feedback_weight: 反馈权重
            
        Returns:
            是否应该信任客户端反馈
        """
        quality_levels = ["WEAK_GOOD", "WEAK_MEDIUM", "WEAK_POOR", "WEAK_VERY_POOR", "WEAK_CRITICAL"]
        
        try:
            client_index = quality_levels.index(client_quality)
            local_index = quality_levels.index(local_quality)
            
            # 如果差异不超过1个等级，且权重较高，则信任
            if abs(client_index - local_index) <= 1 and feedback_weight > 0.5:
                return True
            
            # 如果客户端报告质量更差，更容易信任（客户端更能感知用户体验）
            if client_index > local_index:
                return True
            
            return False
            
        except ValueError:
            logger.error(f"无效的质量等级: {client_quality} 或 {local_quality}")
            return False
    
    def _log_feedback_reference(self, quality_level: str, bandwidth: float, rtt: float):
        """
        记录低权重反馈作为参考 - 新增
        
        Args:
            quality_level: 质量等级
            bandwidth: 带宽
            rtt: RTT
        """
        logger.info(f"客户端反馈参考: 质量={quality_level}, "
                   f"带宽={bandwidth/1000000:.2f}Mbps, "
                   f"RTT={rtt:.2f}ms (权重过低，仅作参考)")
    
    def _cross_validate_with_bbr(self, client_bbr: dict, client_bandwidth: float, client_rtt: float):
        """
        与BBR指标进行交叉验证 - 新增
        
        Args:
            client_bbr: 客户端BBR指标
            client_bandwidth: 客户端带宽测量
            client_rtt: 客户端RTT测量
        """
        try:
            # 获取本地BBR指标
            local_bbr = self.get_bbr_metrics()
            if not local_bbr:
                return
            
            # 比较带宽测量
            bandwidth_diff = abs(client_bbr['bandwidth'] - local_bbr.bandwidth)
            if local_bbr.bandwidth > 0:
                bandwidth_relative_diff = bandwidth_diff / local_bbr.bandwidth
                
                if bandwidth_relative_diff > 0.3:  # 差异超过30%
                    logger.warning(f"客户端和服务端BBR带宽差异较大: "
                                 f"客户端={client_bbr['bandwidth']/1000000:.2f}Mbps, "
                                 f"服务端={local_bbr.bandwidth/1000000:.2f}Mbps")
            
            # 比较RTT测量
            rtt_diff = abs(client_bbr['rtt'] - local_bbr.rtt * 1000)
            if rtt_diff > 50:  # 差异超过50ms
                logger.warning(f"客户端和服务端RTT差异较大: "
                             f"客户端={client_bbr['rtt']:.2f}ms, "
                             f"服务端={local_bbr.rtt*1000:.2f}ms")
            
            # 比较丢包率
            local_loss_rate = getattr(local_bbr, 'loss_rate', 0.0)
            loss_rate_diff = abs(client_bbr['loss_rate'] - local_loss_rate)
            if loss_rate_diff > 0.01:  # 差异超过1%
                logger.warning(f"客户端和服务端丢包率差异较大: "
                             f"客户端={client_bbr['loss_rate']*100:.2f}%, "
                             f"服务端={local_loss_rate*100:.2f}%")
            
            logger.debug("BBR指标交叉验证完成")
            
        except Exception as e:
            logger.error(f"BBR交叉验证时出错: {e}")


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