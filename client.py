import asyncio
import logging
import ssl
import subprocess
import time
import datetime
import threading
import psutil
import socket
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import StreamDataReceived
import re
from typing import Optional

# 导入网络监控模块
from bbr_congestion_control import BBRMetrics
from quic_bbr_integration import BBRQuicProtocol, create_bbr_quic_configuration, BBRNetworkMonitor
from network_monitor import NetworkQualityMonitor, NetworkMetrics
from network_quality_config import get_network_quality_levels, get_network_quality_config
from network_quality_evaluator import evaluate_network_quality

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-client")

# 定义ALPN协议
ALPN_PROTOCOLS = ["quic-demo"]

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

# 初始缓冲区大小（字节）
INITIAL_BUFFER_SIZE = 1024 * 1024  # 初始缓冲区大小：1MB，增加初始缓冲区大小
MIN_PLAYBACK_BUFFER = 256 * 1024  # 最小播放缓冲区：256KB


class VideoClient(BBRQuicProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.video_info = None
        self.ffplay_process = None
        self.stream_port = 8889  # 视频流端口
        self._response_received = asyncio.Event()
        self.response = None
        self.video_file_path = None  # 视频文件路径
        self.streaming = False
        self.network_quality = "FAIR"  # 默认网络质量
        self.stop_monitor = False
        self.buffer = bytearray()  # 初始化缓冲区
        self.buffer_size = 0  # 缓冲区大小
        self.last_buffer_check = time.time()
        self.stream_start_time = None
        self.total_bytes_received = 0
        self.last_bytes_count = 0
        self.last_speed_check = time.time()
        self.playback_started = False  # 播放是否已开始
        self.video_stream_id = None  # 保存当前流ID，用于接收视频数据
        self.av_sync_stats = {"pts_diff": [], "last_check": time.time()}  # 音视频同步统计
        
        # 网络监控器 - 使用NetworkMonitor
        self.network_monitor = NetworkQualityMonitor(
            target_host="127.0.0.1",
            target_port=12345,
            interval=2.0,  # 每2秒测量一次
            window_size=30,  # 保留30个历史数据点
            probe_count=5   # 每次测量5个探测包
        )
        
        # 网络质量历史记录
        self.quality_history = []
        self.quality_change_history = []  # 添加缺失的变量
        self.max_quality_history = 10
        self.last_quality_change_time = time.time()
        self.quality_change_cooldown = 5.0  # 质量切换冷却时间（秒）

    def quic_event_received(self, event):
        if isinstance(event, StreamDataReceived):
            data = event.data
            # 记录接收到的流ID
            logger.debug(f"收到流ID {event.stream_id} 的数据: {len(data)} 字节")
            
            # 更新算法（基于接收到的数据）
            if len(data) > 0:
                self.update_bbr_from_data(len(data))
            
            if self.streaming:
                # 如果收到空数据，可能表示流结束
                if not data:
                    logger.info("收到空数据包，视频流可能已结束")
                    # 如果尚未开始播放但已收到数据，强制开始播放
                    if not self.playback_started and self.buffer_size > 0:
                        logger.info(f"视频流结束，强制开始播放缓冲的数据 ({self.buffer_size/1024:.1f} KB)")
                        self.playback_started = True
                        self.start_video_playback()
                    return
                
                # 检查是否是流结束标记
                try:
                    message = data.decode()
                    if message == "STREAM_END_MARKER":
                        logger.info("收到流结束标记")
                        # 如果尚未开始播放但已收到数据，强制开始播放
                        if not self.playback_started and self.buffer_size > 0:
                            logger.info(f"收到流结束标记，强制开始播放缓冲的数据 ({self.buffer_size/1024:.1f} KB)")
                            self.playback_started = True
                            self.start_video_playback()
                        return
                except UnicodeDecodeError:
                    # 不是文本数据，继续处理为视频数据
                    pass
                
                # 记录数据包信息
                logger.info(f"收到数据包: {len(data)} 字节")
                
                # 更新接收的数据量
                self.total_bytes_received += len(data)
                
                # 如果是第一个数据包，记录开始时间，但不立即开始播放
                if self.stream_start_time is None:
                    self.stream_start_time = time.time()
                    # 启动网络监控线程
                    self.stop_monitor = False
                    self.network_monitor_thread = threading.Thread(target=self.monitor_network_quality)
                    self.network_monitor_thread.daemon = True
                    self.network_monitor_thread.start()
                    logger.info("开始接收视频流数据")
                    
                    # 等待足够的数据再开始播放，确保有足够的缓冲
                    logger.info(f"正在填充初始缓冲区，目标大小: {INITIAL_BUFFER_SIZE/1024:.1f} KB")
                
                # 安全地处理缓冲区
                try:
                    # 如果已经开始播放，直接发送数据到ffplay
                    if self.playback_started and self.ffplay_process and self.ffplay_process.stdin:
                        try:
                            # 直接发送数据，不再进行TS包检查，简化处理逻辑
                            self.ffplay_process.stdin.write(data)
                            self.ffplay_process.stdin.flush()
                        except BrokenPipeError:
                            logger.error("FFplay管道已断开")
                        except Exception as e:
                            logger.error(f"写入FFplay管道时出错: {e}")
                    else:
                        # 否则，将数据添加到缓冲区
                        # 创建新的缓冲区并复制数据，避免使用extend
                        new_buffer = bytearray(len(self.buffer) + len(data))
                        new_buffer[:len(self.buffer)] = self.buffer
                        new_buffer[len(self.buffer):] = data
                        self.buffer = new_buffer
                        self.buffer_size = len(self.buffer)
                        
                        # 记录缓冲区大小
                        if self.buffer_size % (256 * 1024) == 0:  # 每256KB记录一次
                            logger.info(f"缓冲区大小: {self.buffer_size/1024:.1f} KB")
                        
                        # 如果缓冲区达到初始大小且尚未开始播放，则启动播放
                        if self.buffer_size >= INITIAL_BUFFER_SIZE and not self.playback_started:
                            # 额外检查：确保缓冲区数据足够稳定
                            if self.buffer_size >= INITIAL_BUFFER_SIZE * 1.1:  # 要求110%的缓冲区
                                logger.info(f"初始缓冲区已填满 ({self.buffer_size/1024:.1f} KB)，开始播放")
                                self.playback_started = True
                                self.start_video_playback()
                            else:
                                logger.info(f"缓冲区接近满 ({self.buffer_size/1024:.1f} KB)，等待更多数据...")
                        elif self.buffer_size < INITIAL_BUFFER_SIZE and not self.playback_started:
                            # 显示缓冲区填充进度
                            progress = (self.buffer_size / INITIAL_BUFFER_SIZE) * 100
                            if self.buffer_size % (128 * 1024) == 0:  # 每128KB记录一次进度
                                logger.info(f"缓冲区填充进度: {progress:.1f}% ({self.buffer_size/1024:.1f} KB / {INITIAL_BUFFER_SIZE/1024:.1f} KB)")
                except Exception as e:
                    logger.error(f"处理数据流时出错: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                
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
                    # 保存当前流ID，用于接收视频数据
                    self.video_stream_id = event.stream_id
                    logger.info(f"视频流ID: {self.video_stream_id}")
                    
                    # 初始化缓冲区
                    self.buffer = bytearray()
                    self.buffer_size = 0
                    self.playback_started = False
                    self.stream_start_time = None  # 确保重置为None，等待第一个数据包到达
                    logger.info(f"正在填充初始缓冲区，目标大小: {INITIAL_BUFFER_SIZE/1024:.1f} KB")
                    
                    # 启动超时检查线程，确保即使没有收到足够数据也能开始播放
                    def timeout_check():
                        start_time = time.time()
                        max_wait_time = 15  # 最大等待15秒
                        while not self.playback_started and not self.stop_monitor:
                            current_time = time.time()
                            if current_time - start_time > max_wait_time:
                                if self.buffer_size > 0:
                                    logger.info(f"等待超时 ({max_wait_time}秒)，强制开始播放缓冲的数据 ({self.buffer_size/1024:.1f} KB)")
                                    self.playback_started = True
                                    self.start_video_playback()
                                else:
                                    logger.warning(f"等待超时 ({max_wait_time}秒)，但缓冲区为空，继续等待...")
                                break
                            time.sleep(1)
                    
                    timeout_thread = threading.Thread(target=timeout_check)
                    timeout_thread.daemon = True
                    timeout_thread.start()
                elif message == "DATA_SENT":
                    logger.info("收到服务器数据发送通知")
                    # 如果已经收到了一些数据但还没有开始播放，立即开始播放
                    if not self.playback_started and self.buffer_size > 0:
                        logger.info(f"收到数据通知，开始播放缓冲的数据 ({self.buffer_size/1024:.1f} KB)")
                        self.playback_started = True
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
                width, height, original_codec = info_parts[0:3]
                fps = info_parts[3]
                audio_codec, audio_sample_rate, audio_channels = info_parts[4:7]
                file_size = float(info_parts[7])
                duration = float(info_parts[8])

                # 显示实际使用的编码方式（H.265），而不是原始文件的编码方式
                actual_codec = "H.265 (HEVC)"  # 服务器实际使用H.265编码
                
                logger.info(f"视频信息: {width}x{height}, 原始编解码器: {original_codec}, 传输编解码器: {actual_codec}, 帧率: {fps}fps")
                logger.info(f"音频信息: 编解码器: {audio_codec}, 采样率: {audio_sample_rate}Hz, 声道数: {audio_channels}")
                logger.info(f"文件大小: {file_size / 1024 / 1024:.2f} MB, 时长: {duration:.2f}秒")

                # 保存解析的信息供播放使用
                self.video_width = width
                self.video_height = height
                self.video_codec = actual_codec  # 使用实际传输的编码方式
                self.video_fps = fps
                self.audio_codec = audio_codec
                self.audio_sample_rate = audio_sample_rate
                self.audio_channels = audio_channels
                self.file_size = file_size
                self.duration = duration

        except Exception as e:
            logger.error(f"解析视频信息时出错: {e}")
    
    def monitor_network_quality(self):
        """使用NetworkMonitor监控网络质量并发送反馈给服务器"""
        logger.info("启动网络质量监控")
        
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
                        
                        # 发送网络反馈给服务器
                        self._send_network_feedback(new_quality, current_metrics.latency, current_metrics.packet_loss)
                
                # 每2秒检测一次
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"监控网络质量时出错: {e}")
                time.sleep(2)
        
        # 停止网络监控
        self.network_monitor.stop()
    
    def _send_network_feedback(self, quality: str, latency: float, packet_loss: float):
        """
        发送网络质量反馈给服务器
        
        Args:
            quality: 网络质量等级
            latency: 延迟 (ms)
            packet_loss: 丢包率 (%)
        """
        try:
            # 构建简化的反馈消息
            feedback_message = f"NETWORK_FEEDBACK:{quality}:{latency:.2f}:{packet_loss:.2f}:{self.buffer_size}"
            
            ctrl_stream_id = self._quic.get_next_available_stream_id()
            self._quic.send_stream_data(ctrl_stream_id, feedback_message.encode())
            
            logger.info(f"已发送网络反馈: {quality} (延迟: {latency:.1f}ms, 丢包率: {packet_loss:.2f}%)")
            
        except Exception as e:
            logger.error(f"发送网络反馈时出错: {e}")
    

    

    

    

    

    
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
    
    def _check_buffer_health(self):
        """
        检查缓冲区健康状态 - 新增
        """
        try:
            if self.playback_started and self.ffplay_process and self.ffplay_process.poll() is None:
                # 检查缓冲区是否充足
                if self.buffer_size < MIN_PLAYBACK_BUFFER:
                    logger.warning(f"缓冲区不足: {self.buffer_size/1024:.1f} KB < {MIN_PLAYBACK_BUFFER/1024:.1f} KB")
                    # 可以发送缓冲区不足的反馈
                    self._send_buffer_warning()
                elif self.buffer_size > INITIAL_BUFFER_SIZE * 2:
                    logger.info(f"缓冲区充足: {self.buffer_size/1024:.1f} KB")
                    
        except Exception as e:
            logger.error(f"检查缓冲区健康状态时出错: {e}")
    
    def _send_buffer_warning(self):
        """
        发送缓冲区不足警告 - 新增
        """
        try:
            warning_message = f"BUFFER_WARNING:{self.buffer_size}:{MIN_PLAYBACK_BUFFER}"
            ctrl_stream_id = self._quic.get_next_available_stream_id()
            self._quic.send_stream_data(ctrl_stream_id, warning_message.encode())
            logger.info("已发送缓冲区不足警告")
        except Exception as e:
            logger.error(f"发送缓冲区警告时出错: {e}")
    

    

    
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
        try:
            # 使用psutil获取网络IO统计信息
            net_io_counters = psutil.net_io_counters()
            bytes_sent = net_io_counters.bytes_sent
            bytes_recv = net_io_counters.bytes_recv
            
            # 等待一小段时间
            time.sleep(1)
            
            # 再次获取统计信息
            net_io_counters = psutil.net_io_counters()
            bytes_sent_new = net_io_counters.bytes_sent
            bytes_recv_new = net_io_counters.bytes_recv
            
            # 计算每秒字节数
            bytes_sent_per_sec = bytes_sent_new - bytes_sent
            bytes_recv_per_sec = bytes_recv_new - bytes_recv
            
            # 返回接收速度 (bytes/second)
            return bytes_recv_per_sec
        except Exception as e:
            logger.error(f"测量网络速度时出错: {e}")
            return 2000000  # 默认2Mbps

    def start_video_playback(self):
        """启动视频播放 - 改进的实现"""
        def play_video():
            try:
                logger.info("准备启动视频播放...")
                
                # 确保之前的ffplay进程已关闭
                if self.ffplay_process and self.ffplay_process.poll() is None:
                    try:
                        self.ffplay_process.terminate()
                        self.ffplay_process.wait(timeout=1)
                    except:
                        try:
                            self.ffplay_process.kill()
                        except:
                            pass
                
                # 使用最基本的FFplay命令，确保视频正确显示
                cmd = [
                    'ffplay', 
                    '-i', 'pipe:0',
                    '-autoexit',  # 播放完成后自动退出
                    '-x', '1280',  # 窗口宽度
                    '-y', '720',   # 窗口高度
                    '-window_title', '弱网场景下的视频流传输系统',  # 窗口标题
                    '-noborder',  # 无边框
                ]
                logger.info("启动FFplay: " + " ".join(cmd))
                
                # 使用更大的缓冲区大小创建进程
                self.ffplay_process = subprocess.Popen(
                    cmd, 
                    stdin=subprocess.PIPE, 
                    bufsize=1024*1024*4,  # 4MB缓冲区
                    stderr=subprocess.PIPE  # 捕获错误输出
                )
                
                # 启动错误输出监控线程
                def monitor_stderr():
                    while self.ffplay_process and self.ffplay_process.poll() is None:
                        try:
                            line = self.ffplay_process.stderr.readline()
                            if line:
                                line_str = line.decode('utf-8', errors='ignore').strip()
                                
                                # 记录所有输出用于调试
                                logger.info(f"FFplay输出: {line_str}")
                                
                                # 检查是否有错误
                                if line_str and "error" in line_str.lower():
                                    logger.error(f"FFplay错误: {line_str}")
                                elif line_str and "warning" in line_str.lower():
                                    logger.warning(f"FFplay警告: {line_str}")
                        except:
                            break
                
                stderr_thread = threading.Thread(target=monitor_stderr)
                stderr_thread.daemon = True
                stderr_thread.start()
                
                # 如果有缓冲的数据，立即发送
                if len(self.buffer) > 0 and self.ffplay_process.stdin:
                    logger.info(f"发送缓冲数据到FFplay: {len(self.buffer)/1024:.1f} KB")
                    try:
                        # 直接发送所有缓冲数据
                        self.ffplay_process.stdin.write(self.buffer)
                        self.ffplay_process.stdin.flush()
                        self.buffer = bytearray()  # 清空缓冲区
                        logger.info("缓冲数据发送完成")
                    except BrokenPipeError:
                        logger.error("发送缓冲数据时管道已断开")
                        self.buffer = bytearray()  # 清空缓冲区
                    except Exception as e:
                        logger.error(f"发送缓冲数据时出错: {e}")
                
                # 等待播放完成
                try:
                    self.ffplay_process.wait()
                except Exception as e:
                    logger.error(f"等待FFplay进程时出错: {e}")
                
                # 播放结束后通知服务器
                try:
                    ctrl_stream_id = self._quic.get_next_available_stream_id()
                    self._quic.send_stream_data(ctrl_stream_id, b"STREAM_COMPLETE")
                    logger.info("已通知服务器播放完成")
                    # 停止网络监控
                    self.stop_monitor = True
                except Exception as e:
                    logger.error(f"通知服务器时出错: {e}")
                    
                # 关闭ffplay进程
                if self.ffplay_process and self.ffplay_process.poll() is None:
                    try:
                        self.ffplay_process.terminate()
                        self.ffplay_process.wait(timeout=2)
                    except Exception as e:
                        logger.error(f"关闭FFplay进程时出错: {e}")
                        try:
                            self.ffplay_process.kill()
                        except:
                            pass
            except Exception as e:
                logger.error(f"播放视频时出错: {e}")
                import traceback
                logger.error(traceback.format_exc())
                # 确保停止监控线程
                self.stop_monitor = True
        
        # 在单独的线程中启动播放
        playback_thread = threading.Thread(target=play_video)
        playback_thread.daemon = True
        playback_thread.start()

    def cleanup(self):
        """清理客户端资源"""
        logger.info("正在清理VideoClient资源...")
        
        # 停止监控
        self.stop_monitor = True
        
        # 停止网络监控
        if self.network_monitor:
            self.network_monitor.stop()
        
        # 关闭ffplay进程
        if self.ffplay_process and self.ffplay_process.poll() is None:
            try:
                self.ffplay_process.terminate()
                self.ffplay_process.wait(timeout=2)
            except:
                try:
                    self.ffplay_process.kill()
                except:
                    pass
        
        # 清空缓冲区
        self.buffer = bytearray()
        self.buffer_size = 0
        
        # 重置状态
        self.streaming = False
        self.playback_started = False
        
        logger.info("VideoClient资源清理完成")

    def periodic_network_update(self):
        """定期更新网络指标 - 改进的实现"""
        try:
            # 获取BBR指标（后台使用，不显示在GUI）
            bbr_metrics = self.get_bbr_metrics()
            
            # 获取当前网络监控指标
            current_metrics = None
            if hasattr(self, 'network_monitor'):
                current_metrics = self.network_monitor.get_current_metrics()
            
            # 计算真实的网络质量等级
            real_quality = 'UNKNOWN'
            if current_metrics:
                # 使用改进的质量判断逻辑
                real_quality = self._determine_quality_from_network_metrics(current_metrics)
            else:
                # 如果没有监控数据，使用缓存的网络质量
                real_quality = getattr(self, 'network_quality', 'UNKNOWN')
            
            # 发送网络状态到GUI - 包含详细的网络指标
            network_info = {
                'bytes_received': getattr(self, 'total_bytes_received', 0),
                'quality': real_quality,  # 使用真实的网络质量
                'buffer_size': getattr(self, 'buffer_size', 0),
                'latency': current_metrics.latency if current_metrics else -1,
                'packet_loss': current_metrics.packet_loss if current_metrics else 0,
                'jitter': current_metrics.jitter if current_metrics else 0,
                'bandwidth': getattr(current_metrics, 'bandwidth', 0) if current_metrics else 0
            }
            self.parent_thread.network_status.emit(network_info)
            
            # 更新播放状态
            playback_info = {
                'streaming': getattr(self, 'streaming', False),
                'playback_started': getattr(self, 'playback_started', False)
            }
            self.parent_thread.playback_status.emit(playback_info)
            
            # 重新启动定时器
            if not self.stop_monitor:
                self.network_update_timer = threading.Timer(1.0, self.periodic_network_update)
                self.network_update_timer.daemon = True
                self.network_update_timer.start()
        except Exception as e:
            logger.error(f"定期更新网络指标时出错: {e}")
            import traceback
            logger.error(traceback.format_exc())


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
    config = create_bbr_quic_configuration()
    config.is_client = True
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
                logger.info("已连接到QUIC服务器")

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
        logger.info("客户端已关闭") 