import sys
import os
import asyncio
import logging
import time
import threading
import ssl
import subprocess
import random
import json
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel, 
                           QVBoxLayout, QHBoxLayout, QWidget, QFileDialog, 
                           QComboBox, QProgressBar, QTextEdit, QGroupBox,
                           QSlider, QStyle, QStyleFactory)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer, QMimeData
from PyQt5.QtGui import QFont, QPalette, QColor, QIcon, QDragEnterEvent, QDropEvent
from PyQt5.QtChart import QChart, QChartView, QLineSeries, QValueAxis
import pyqtgraph as pg

# 导入服务端和客户端模块
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'test'))
from server import start_server, stop_server, VideoServer, analyze_video, NETWORK_QUALITY
from client import VideoClient, ALPN_PROTOCOLS

# 导入网络监控模块
from bbr_congestion_control import BBRMetrics
from quic_bbr_integration import BBRNetworkMonitor
from network_monitor import NetworkQualityMonitor

from aioquic.asyncio import serve, connect
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived

# 设置日志
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("video-player-gui")

# 初始缓冲区大小（字节）
INITIAL_BUFFER_SIZE = 1024 * 1024  # 初始缓冲区大小：1MB

class ServerThread(QThread):
    server_status = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.loop = None
        self.server_task = None
        self.running = False
    
    def run(self):
        self.running = True
        self.server_status.emit("正在启动服务器...")
        
        # 创建新的事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            # 启动服务器
            self.server_task = self.loop.create_task(start_server())
            self.server_status.emit("服务器已启动，等待连接...")
            self.loop.run_forever()
        except Exception as e:
            self.server_status.emit(f"服务器错误: {str(e)}")
        finally:
            # 确保事件循环正确关闭
            try:
                if self.loop and not self.loop.is_closed():
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(self.loop)
                    for task in pending:
                        task.cancel()
                    
                    # 等待所有任务完成
                    if pending:
                        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    
                    self.loop.close()
            except Exception as e:
                logger.error(f"关闭事件循环时出错: {e}")
            
            self.server_status.emit("服务器已停止")
            self.running = False
    
    def stop(self):
        if self.loop and self.running:
            # 停止服务器
            async def stop_task():
                await stop_server()
                self.loop.stop()
            
            self.loop.create_task(stop_task())
            self.running = False


class ClientThread(QThread):
    client_status = pyqtSignal(str)
    video_info = pyqtSignal(dict)
    network_status = pyqtSignal(dict)
    playback_status = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self.video_file_path = None
        self.loop = None
        self.client = None
        self.running = False
        self.client_protocol = None
    
    def set_video_file(self, file_path):
        self.video_file_path = file_path
    
    def run(self):
        if not self.video_file_path:
            self.client_status.emit("未选择视频文件")
            return
        
        self.running = True
        self.client_status.emit("正在连接到服务器...")
        
        # 创建新的事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            # 运行客户端
            self.loop.run_until_complete(self.run_client())
        except Exception as e:
            self.client_status.emit(f"客户端错误: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            # 确保事件循环正确关闭
            try:
                if self.loop and not self.loop.is_closed():
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(self.loop)
                    for task in pending:
                        task.cancel()
                    
                    # 等待所有任务完成
                    if pending:
                        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    
                    self.loop.close()
            except Exception as e:
                logger.error(f"关闭事件循环时出错: {e}")
            
            self.client_status.emit("客户端已断开连接")
            self.running = False
    
    async def run_client(self):
        # 创建QUIC配置
        config = QuicConfiguration(is_client=True)
        config.alpn_protocols = ALPN_PROTOCOLS
        config.verify_mode = ssl.CERT_NONE
        
        # 优化QUIC配置参数
        config.max_data = 10 * 1024 * 1024
        config.max_stream_data = 5 * 1024 * 1024
        
        # 连接重试机制
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries and self.running:
            try:
                # 创建自定义客户端协议
                class GUIVideoClient(VideoClient):
                    def __init__(self, *args, **kwargs):
                        self.parent_thread = kwargs.pop('parent_thread')
                        super().__init__(*args, **kwargs)
                        logger.info("GUIVideoClient初始化完成")
                        
                        # 启动定时器，定期更新网络指标
                        self.network_update_timer = threading.Timer(1.0, self.periodic_network_update)
                        self.network_update_timer.daemon = True
                        self.network_update_timer.start()
                    
                    def cleanup(self):
                        """清理资源 - 改进的实现"""
                        logger.info("正在清理GUIVideoClient资源...")
                        # 停止网络更新定时器
                        if hasattr(self, 'network_update_timer') and self.network_update_timer:
                            self.network_update_timer.cancel()
                            logger.info("网络更新定时器已停止")
                        
                        # 调用父类清理方法
                        try:
                            super().cleanup()
                        except AttributeError:
                            # 如果父类没有cleanup方法，手动清理
                            logger.info("父类没有cleanup方法，手动清理资源")
                            self.stop_monitor = True
                            
                            # 关闭FFplay进程
                            if hasattr(self, 'ffplay_process') and self.ffplay_process:
                                try:
                                    if self.ffplay_process.poll() is None:
                                        self.ffplay_process.terminate()
                                        self.ffplay_process.wait(timeout=2)
                                except:
                                    try:
                                        self.ffplay_process.kill()
                                    except:
                                        pass
                        
                        logger.info("GUIVideoClient资源清理完成")
                    
                    def periodic_network_update(self):
                        """定期更新网络指标 - 改进的实现"""
                        try:
                            # 获取算法指标（后台使用，不显示在GUI）
                            bbr_metrics = self.get_bbr_metrics()
                            
                            # 发送网络状态到GUI
                            network_info = {
                                'bytes_received': getattr(self, 'total_bytes_received', 0),
                                'quality': getattr(self, 'network_quality', 'UNKNOWN'),
                                'buffer_size': getattr(self, 'buffer_size', 0)
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
                    
                    def quic_event_received(self, event):
                        if isinstance(event, StreamDataReceived):
                            data = event.data
                            
                            # 处理视频信息
                            if not self.streaming:
                                try:
                                    message = data.decode()
                                    if message.startswith("VIDEO_INFO:"):
                                        video_info_str = message.split(":", 1)[1]
                                        self.video_info = video_info_str
                                        self.parse_video_info(video_info_str)
                                        
                                        # 发送视频信息到GUI
                                        if hasattr(self, 'video_width'):
                                            info = {
                                                'width': self.video_width,
                                                'height': self.video_height,
                                                'codec': self.video_codec,
                                                'fps': self.video_fps,
                                                'audio_codec': self.audio_codec,
                                                'duration': self.duration
                                            }
                                            self.parent_thread.video_info.emit(info)
                                except:
                                    pass
                        
                        # 调用原始处理方法
                        super().quic_event_received(event)
                
                # 连接到服务器
                self.client_status.emit("正在连接到服务器...")
                
                async with connect(
                        "127.0.0.1",
                        4433,
                        configuration=config,
                        create_protocol=lambda *args, **kwargs: GUIVideoClient(*args, parent_thread=self, **kwargs)
                ) as client:
                    self.client_protocol = client
                    self.client_status.emit("已连接到服务器")
                    
                    # 设置视频文件路径
                    client.set_video_file(self.video_file_path)
                    
                    # 发送文件路径给服务器
                    self.client_status.emit("发送视频文件路径给服务器...")
                    stream_id = client._quic.get_next_available_stream_id()
                    client._quic.send_stream_data(stream_id, f"SET_VIDEO_FILE:{self.video_file_path}".encode())
                    
                    # 等待服务器响应
                    await asyncio.sleep(2)
                    
                    # 保持连接直到播放完成
                    while self.running:
                        await asyncio.sleep(0.5)
                        
                        # 更新网络状态
                        if hasattr(client, 'total_bytes_received') and hasattr(client, 'network_quality'):
                            # 获取QUIC连接统计信息
                            network_info = {
                                'bytes_received': client.total_bytes_received,
                                'quality': client.network_quality,
                                'buffer_size': getattr(client, 'buffer_size', 0)
                            }
                            self.network_status.emit(network_info)
                        
                        # 更新播放状态
                        playback_info = {
                            'streaming': client.streaming,
                            'playback_started': getattr(client, 'playback_started', False)
                        }
                        self.playback_status.emit(playback_info)
                        
                        # 检查FFplay进程是否还在运行
                        if hasattr(client, 'ffplay_process') and client.ffplay_process:
                            if client.ffplay_process.poll() is not None:
                                self.client_status.emit("视频播放已完成")
                                break
                
                # 如果正常完成，跳出重试循环
                break
                
            except ConnectionRefusedError:
                retry_count += 1
                self.client_status.emit(f"连接被拒绝，服务器可能未启动。重试 {retry_count}/{max_retries}")
                if retry_count < max_retries and self.running:
                    await asyncio.sleep(2)  # 等待2秒后重试
                else:
                    self.client_status.emit("无法连接到服务器，请确保服务器已启动")
                    break
                    
            except Exception as e:
                retry_count += 1
                self.client_status.emit(f"连接错误: {str(e)}")
                if retry_count < max_retries and self.running:
                    await asyncio.sleep(2)  # 等待2秒后重试
                else:
                    self.client_status.emit("连接失败，已达到最大重试次数")
                    break
    
    def stop(self):
        self.running = False
        if self.loop:
            self.loop.stop()
        
        # 清理客户端资源
        if self.client_protocol:
            try:
                if hasattr(self.client_protocol, 'cleanup'):
                    self.client_protocol.cleanup()
                
                # 关闭FFplay进程
                if hasattr(self.client_protocol, 'ffplay_process'):
                    try:
                        if self.client_protocol.ffplay_process and self.client_protocol.ffplay_process.poll() is None:
                            self.client_protocol.ffplay_process.terminate()
                            self.client_protocol.ffplay_process.wait(timeout=2)
                    except:
                        try:
                            if self.client_protocol.ffplay_process:
                                self.client_protocol.ffplay_process.kill()
                        except:
                            pass
            except Exception as e:
                logger.error(f"停止客户端时出错: {e}")
                import traceback
                logger.error(traceback.format_exc())


class NetworkMonitorThread(QThread):
    metrics_updated = pyqtSignal(object)
    
    def __init__(self):
        super().__init__()
        self.monitor = NetworkQualityMonitor()
        self.running = False
    
    def run(self):
        self.running = True
        self.monitor.start()
        
        while self.running:
            metrics = self.monitor.get_current_metrics()
            if metrics:
                self.metrics_updated.emit(metrics)
            time.sleep(0.5)
    
    def stop(self):
        self.running = False
        self.monitor.stop()

class VideoPlayerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.server_thread = None
        self.client_thread = None
        self.network_monitor_thread = None
        self.selected_video_path = None
        
        # 初始化状态
        self.bytes_received = 0
        self.start_time = time.time()
        
        # 初始化数据存储
        self.latency_data = {'x': [], 'y': []}
        self.packet_loss_data = {'x': [], 'y': []}
        self.jitter_data = {'x': [], 'y': []}
        self.bitrate_data = {'x': [], 'y': []}
        self.buffer_data = {'x': [], 'y': []}
        
        # 播放历史记录
        self.history_file = "play_history.json"
        self.play_history = self.load_history()
        
        # 设置应用主题
        self.apply_light_theme()
        
        # 初始化UI
        self.init_ui()
        
        # 启动更新定时器
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_stats)
        self.update_timer.start(1000)  # 每秒更新一次
        
        # 初始化网络监控
        self.network_monitor_thread = NetworkMonitorThread()
        self.network_monitor_thread.metrics_updated.connect(self.update_network_metrics)
        self.network_monitor_thread.start()
        
        # 自动启动服务器
        QTimer.singleShot(500, self.start_server)

    def apply_light_theme(self):
        """应用浅色主题"""
        self.setStyle(QStyleFactory.create('Fusion'))
        palette = QPalette()
        
        # 设置浅色主题的颜色
        palette.setColor(QPalette.Window, QColor(240, 240, 240))
        palette.setColor(QPalette.WindowText, QColor(0, 0, 0))
        palette.setColor(QPalette.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.AlternateBase, QColor(245, 245, 245))
        palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette.setColor(QPalette.ToolTipText, QColor(0, 0, 0))
        palette.setColor(QPalette.Text, QColor(0, 0, 0))
        palette.setColor(QPalette.Button, QColor(240, 240, 240))
        palette.setColor(QPalette.ButtonText, QColor(0, 0, 0))
        palette.setColor(QPalette.BrightText, QColor(255, 0, 0))
        palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        
        self.setPalette(palette)

    def load_history(self):
        """加载播放历史"""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"加载播放历史失败: {e}")
            return []

    def save_history(self):
        """保存播放历史"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.play_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存播放历史失败: {e}")

    def add_to_history(self, file_path):
        """添加视频到播放历史"""
        if not file_path:
            return
            
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        history_entry = {
            "file_path": file_path,
            "file_name": os.path.basename(file_path),
            "play_time": current_time
        }
        
        # 移除重复项
        self.play_history = [h for h in self.play_history if h["file_path"] != file_path]
        
        # 添加新记录到开头
        self.play_history.insert(0, history_entry)
        
        # 限制历史记录数量
        self.play_history = self.play_history[:50]  # 保留最近50条记录
        
        # 保存历史记录
        self.save_history()

    def init_ui(self):
        """初始化GUI界面"""
        self.setWindowTitle('QUIC视频播放器')
        self.setGeometry(100, 100, 1200, 800)
        
        # 设置窗口接受拖放
        self.setAcceptDrops(True)
        
        # 设置主窗口背景色
        self.setStyleSheet("""
            QMainWindow {
                background-color: #2C3E50;
            }
            QWidget {
                background-color: #2C3E50;
                color: #ECF0F1;
            }
        """)
        
        # 创建主布局
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QHBoxLayout(main_widget)
        main_layout.setSpacing(10)  # 增加组件之间的间距
        main_layout.setContentsMargins(10, 10, 10, 10)  # 设置边距
        
        # 左侧布局
        left_layout = QVBoxLayout()
        left_layout.setSpacing(10)  # 增加组件之间的间距
        
        # 服务器状态区域
        server_group = QGroupBox("服务器状态")
        server_layout = QVBoxLayout()
        
        self.server_status_label = QLabel("服务器状态: 未启动")
        self.server_status_label.setFont(QFont("Microsoft YaHei", 9))
        server_layout.addWidget(self.server_status_label)
        
        # 添加网络质量和比特率标签
        self.network_quality_label = QLabel("网络质量: -")
        self.network_quality_label.setFont(QFont("Microsoft YaHei", 9))
        server_layout.addWidget(self.network_quality_label)
        
        self.bitrate_label = QLabel("比特率: - Mbps")
        self.bitrate_label.setFont(QFont("Microsoft YaHei", 9))
        server_layout.addWidget(self.bitrate_label)
        
        server_group.setLayout(server_layout)
        left_layout.addWidget(server_group)
        
        # 区域2：视频控制区
        control_group = QGroupBox("视频控制")
        control_layout = QVBoxLayout()
        
        # 视频选择按钮
        self.select_video_btn = QPushButton("选择视频")
        self.select_video_btn.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.select_video_btn.clicked.connect(self.select_video)
        control_layout.addWidget(self.select_video_btn)
        
        # 视频状态标签
        self.video_status_label = QLabel("当前未选择视频")
        self.video_status_label.setFont(QFont("Microsoft YaHei", 10))
        control_layout.addWidget(self.video_status_label)
        
        # 播放状态标签
        self.streaming_status_label = QLabel("流传输: 未开始")
        self.streaming_status_label.setFont(QFont("Microsoft YaHei", 9))
        control_layout.addWidget(self.streaming_status_label)
        
        self.playback_status_label = QLabel("播放状态: 未开始")
        self.playback_status_label.setFont(QFont("Microsoft YaHei", 9))
        control_layout.addWidget(self.playback_status_label)
        
        # 播放控制按钮
        button_layout = QHBoxLayout()
        self.play_video_btn = QPushButton()
        self.play_video_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_video_btn.clicked.connect(self.play_video)
        
        self.stop_video_btn = QPushButton()
        self.stop_video_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_video_btn.clicked.connect(self.stop_video)
        
        button_layout.addWidget(self.play_video_btn)
        button_layout.addWidget(self.stop_video_btn)
        control_layout.addLayout(button_layout)
        
        control_group.setLayout(control_layout)
        left_layout.addWidget(control_group)
        
        # 缓冲区状态显示
        buffer_group = QGroupBox("缓冲状态")
        buffer_layout = QVBoxLayout()
        
        self.buffer_progress = QProgressBar()
        self.buffer_progress.setTextVisible(True)
        self.buffer_progress.setFormat("缓冲: %p%")
        self.buffer_progress.setMinimum(0)
        self.buffer_progress.setMaximum(100)
        buffer_layout.addWidget(self.buffer_progress)
        
        self.buffer_label = QLabel("缓冲区: 0 KB")
        self.buffer_label.setFont(QFont("Microsoft YaHei", 9))
        buffer_layout.addWidget(self.buffer_label)
        
        buffer_group.setLayout(buffer_layout)
        left_layout.addWidget(buffer_group)
        
        # 区域3：传输视频信息
        info_group = QGroupBox("视频信息")
        info_layout = QVBoxLayout()
        
        # 使用统一的字体和样式
        info_font = QFont("Microsoft YaHei", 9)
        self.resolution_label = QLabel("分辨率")
        self.codec_label = QLabel("传输编码")
        self.fps_label = QLabel("帧率")
        self.audio_codec_label = QLabel("音频编解码器")
        self.duration_label = QLabel("视频总时长")
        
        for label in [self.resolution_label, self.codec_label, self.fps_label, 
                     self.audio_codec_label, self.duration_label]:
            label.setFont(info_font)
            info_layout.addWidget(label)
        
        info_group.setLayout(info_layout)
        left_layout.addWidget(info_group)
        
        main_layout.addLayout(left_layout)
        
        # 右侧布局（网络监控图表）
        right_layout = QVBoxLayout()
        
        # 区域5：网络延迟图表
        latency_group = QGroupBox("网络延迟")
        latency_layout = QVBoxLayout()
        
        self.latency_plot = pg.PlotWidget()
        self.latency_plot.setBackground('w')
        self.latency_plot.setLabel('left', '延迟 (ms)')
        self.latency_plot.setLabel('bottom', '时间 (s)')
        self.latency_curve = self.latency_plot.plot(pen='b')
        latency_layout.addWidget(self.latency_plot)
        
        latency_group.setLayout(latency_layout)
        right_layout.addWidget(latency_group)
        
        # 区域7：丢包率图表
        packet_loss_group = QGroupBox("丢包率")
        packet_loss_layout = QVBoxLayout()
        
        self.packet_loss_plot = pg.PlotWidget()
        self.packet_loss_plot.setBackground('w')
        self.packet_loss_plot.setLabel('left', '丢包率 (%)')
        self.packet_loss_plot.setLabel('bottom', '时间 (s)')
        self.packet_loss_curve = self.packet_loss_plot.plot(pen='r')
        packet_loss_layout.addWidget(self.packet_loss_plot)
        
        packet_loss_group.setLayout(packet_loss_layout)
        right_layout.addWidget(packet_loss_group)
        
        # 区域8：网络抖动图表
        jitter_group = QGroupBox("网络抖动")
        jitter_layout = QVBoxLayout()
        
        self.jitter_plot = pg.PlotWidget()
        self.jitter_plot.setBackground('w')
        self.jitter_plot.setLabel('left', '抖动 (ms)')
        self.jitter_plot.setLabel('bottom', '时间 (s)')
        self.jitter_curve = self.jitter_plot.plot(pen='g')
        jitter_layout.addWidget(self.jitter_plot)
        
        jitter_group.setLayout(jitter_layout)
        right_layout.addWidget(jitter_group)
        
        main_layout.addLayout(right_layout)
        
        # 设置布局比例
        main_layout.setStretch(0, 1)  # 左侧布局
        main_layout.setStretch(1, 2)  # 右侧布局
        
        # 初始化按钮状态
        self.play_video_btn.setEnabled(False)
        self.stop_video_btn.setEnabled(False)
        
        # 设置样式
        self.setStyleSheet("""
            QMainWindow {
                background-color: #2C3E50;
            }
            QWidget {
                background-color: #2C3E50;
                color: #ECF0F1;
            }
            QGroupBox {
                font-family: 'Microsoft YaHei';
                font-size: 11pt;
                font-weight: bold;
                border: 2px solid #3498DB;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 10px;
                background-color: #34495E;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: #3498DB;
            }
            QPushButton {
                padding: 8px;
                border-radius: 4px;
                background-color: #3498DB;
                color: white;
                border: none;
                font-weight: bold;
                min-width: 80px;
            }
            QPushButton:hover {
                background-color: #2980B9;
            }
            QPushButton:pressed {
                background-color: #2472A4;
            }
            QPushButton:disabled {
                background-color: #7F8C8D;
                color: #BDC3C7;
            }
            QLabel {
                color: #ECF0F1;
                padding: 2px;
            }
            QProgressBar {
                border: 2px solid #3498DB;
                border-radius: 5px;
                text-align: center;
                height: 20px;
                background-color: #34495E;
                color: white;
            }
            QProgressBar::chunk {
                background-color: #3498DB;
                border-radius: 3px;
            }
            QProgressBar:disabled {
                border-color: #7F8C8D;
            }
            QProgressBar::chunk:disabled {
                background-color: #7F8C8D;
            }
            
            /* 图表样式 */
            QChartView {
                background-color: #34495E;
                border-radius: 8px;
            }
            
            /* 滚动条样式 */
            QScrollBar:vertical {
                border: none;
                background-color: #34495E;
                width: 10px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #3498DB;
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #2980B9;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background-color: #34495E;
            }
            
            /* 提示框样式 */
            QToolTip {
                background-color: #34495E;
                color: #ECF0F1;
                border: 1px solid #3498DB;
                border-radius: 4px;
                padding: 4px;
            }
        """)
        
        # 设置pyqtgraph样式
        pg.setConfigOption('background', '#34495E')
        pg.setConfigOption('foreground', '#ECF0F1')
        
        # 更新图表样式
        for plot in [self.latency_plot, self.packet_loss_plot, self.jitter_plot]:
            plot.setBackground('#34495E')
            plot.getAxis('left').setPen('#ECF0F1')
            plot.getAxis('bottom').setPen('#ECF0F1')
            plot.getAxis('left').setTextPen('#ECF0F1')
            plot.getAxis('bottom').setTextPen('#ECF0F1')

    def log(self, message):
        """添加日志消息到日志区域"""
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        self.log_text.append(f"[{timestamp}] {message}")
        # 滚动到底部
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())
    
    def start_server(self):
        """启动服务器"""
        if not self.server_thread or not self.server_thread.running:
            self.server_thread = ServerThread()
            self.server_thread.server_status.connect(self.update_server_status)
            self.server_thread.start()
            self.log("正在启动服务器...")
    
    def stop_server(self):
        """停止服务器"""
        if self.server_thread and self.server_thread.running:
            self.server_thread.stop()
            self.log("正在停止服务器...")
    
    def update_server_status(self, status):
        """更新服务器状态"""
        self.server_status_label.setText(f"服务器状态: {status}")
        self.log(f"服务器状态: {status}")
        self.update_ui_state()
    
    def select_video(self):
        """选择视频文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择视频文件", 
            "", 
            "视频文件 (*.mp4 *.avi *.mkv *.mov *.wmv *.flv);;所有文件 (*.*)"
        )
        
        if file_path:
            self.selected_video_path = file_path
            self.video_status_label.setText(f"当前选择: {os.path.basename(file_path)}")
            self.play_video_btn.setEnabled(True)
            
            # 重置图表数据
            self.latency_data = {'x': [], 'y': []}
            self.packet_loss_data = {'x': [], 'y': []}
            self.jitter_data = {'x': [], 'y': []}
            
            # 重置计时器
            self.start_time = time.time()
    
    def update_playback_status(self, info):
        """更新播放状态"""
        streaming = "已开始" if info['streaming'] else "未开始"
        self.streaming_status_label.setText(f"流传输: {streaming}")
        
        playback = "已开始" if info['playback_started'] else "缓冲中"
        if info['streaming']:
            self.playback_status_label.setText(f"播放状态: {playback}")
        else:
            self.playback_status_label.setText("播放状态: 未开始")
        
        # 更新缓冲区状态显示
        if 'buffer_size' in info:
            buffer_size = info['buffer_size']
            if buffer_size > 0:
                progress = min(100, (buffer_size / (INITIAL_BUFFER_SIZE)) * 100)
                self.buffer_progress.setValue(int(progress))
                self.buffer_label.setText(f"缓冲区: {buffer_size/1024:.1f} KB")
            else:
                self.buffer_progress.setValue(0)
                self.buffer_label.setText("缓冲区: 等待中...")

    def play_video(self):
        """播放视频"""
        if not self.selected_video_path:
            self.log("错误: 未选择视频文件")
            return
        
        if not self.server_thread or not self.server_thread.running:
            self.log("错误: 服务器未启动")
            return
        
        # 添加到播放历史
        self.add_to_history(self.selected_video_path)
        
        # 启动客户端线程
        self.client_thread = ClientThread()
        self.client_thread.set_video_file(self.selected_video_path)
        self.client_thread.client_status.connect(self.update_client_status)
        self.client_thread.video_info.connect(self.update_video_info)
        self.client_thread.network_status.connect(self.update_network_status)
        self.client_thread.playback_status.connect(self.update_playback_status)
        self.client_thread.start()
        
        self.play_video_btn.setEnabled(False)
        self.stop_video_btn.setEnabled(True)
        self.log("正在开始播放视频...")
        
        # 重置计数器
        self.bytes_received = 0
        self.start_time = time.time()
    
    def stop_video(self):
        """停止视频播放"""
        if self.client_thread and self.client_thread.running:
            try:
                logger.info("正在停止视频播放...")
                
                # 先停止客户端线程
                self.client_thread.stop()
                
                # 等待线程结束
                if self.client_thread.wait(3000):  # 等待3秒
                    logger.info("客户端线程已停止")
                else:
                    logger.warning("客户端线程未能在3秒内停止，强制终止")
                    self.client_thread.terminate()
                    self.client_thread.wait(1000)
                
                # 确保FFplay进程被关闭
                if hasattr(self.client_thread, 'client_protocol') and self.client_thread.client_protocol:
                    client = self.client_thread.client_protocol
                    if hasattr(client, 'ffplay_process') and client.ffplay_process:
                        try:
                            if client.ffplay_process.poll() is None:
                                logger.info("正在关闭FFplay进程...")
                                client.ffplay_process.terminate()
                                client.ffplay_process.wait(timeout=2)
                        except:
                            try:
                                if client.ffplay_process:
                                    client.ffplay_process.kill()
                            except:
                                pass
                
                self.play_video_btn.setEnabled(bool(self.server_thread and self.server_thread.running and self.selected_video_path))
                self.stop_video_btn.setEnabled(False)
                
                # 重置缓冲区进度条和网络状态显示
                self.buffer_progress.setValue(0)
                self.buffer_label.setText("缓冲区: - KB")
                self.bitrate_label.setText("比特率: - Mbps")
                self.network_quality_label.setText("网络质量: -")
                self.playback_status_label.setText("播放状态: 未开始")
                self.streaming_status_label.setText("流传输: 未开始")
                
                logger.info("视频播放已停止")
                
            except Exception as e:
                logger.error(f"停止视频播放时出错: {e}")
                import traceback
                logger.error(traceback.format_exc())
    
    def update_client_status(self, status):
        """更新客户端状态"""
        self.log(f"客户端: {status}")
        if status == "视频播放已完成" or status == "客户端已断开连接":
            self.play_video_btn.setEnabled(bool(self.server_thread and self.server_thread.running and self.selected_video_path))
            self.stop_video_btn.setEnabled(False)
            # 重置缓冲区进度条
            self.buffer_progress.setValue(0)
            self.buffer_label.setText("缓冲区: - KB")
            self.bitrate_label.setText("比特率: - Mbps")
            self.network_quality_label.setText("网络质量: -")
    
    def update_video_info(self, info):
        """更新视频信息"""
        self.resolution_label.setText(f"分辨率: {info['width']}x{info['height']}")
        self.codec_label.setText(f"传输编码: {info['codec']}")
        self.fps_label.setText(f"帧率: {info['fps']}fps")
        self.audio_codec_label.setText(f"音频编解码器: {info['audio_codec']}")
        self.duration_label.setText(f"视频总时长: {float(info['duration']):.2f}秒")
    
    def update_network_status(self, info):
        """更新网络状态"""
        try:
            # 更新网络质量
            if 'quality' in info:
                self.network_quality_label.setText(f"网络质量: {info['quality']}")
            
            # 更新比特率
            if 'bytes_received' in info:
                self.bytes_received = info['bytes_received']
                elapsed = time.time() - self.start_time
                if elapsed > 0:
                    bitrate = (self.bytes_received * 8) / (elapsed * 1000000)  # Mbps
                    self.bitrate_label.setText(f"比特率: {bitrate:.2f} Mbps")
            
            # 更新缓冲区
            if 'buffer_size' in info:
                buffer_size = info['buffer_size']
                buffer_kb = buffer_size / 1024  # 转换为KB
                self.buffer_label.setText(f"缓冲区: {buffer_kb:.1f} KB")
                
                # 更新缓冲区进度条
                if buffer_size >= 0:
                    # 计算缓冲区百分比，最大值为初始缓冲区大小
                    progress = min(100, (buffer_size / INITIAL_BUFFER_SIZE) * 100)
                    logger.debug(f"缓冲区进度: {progress:.1f}% (buffer_size={buffer_size}, INITIAL_BUFFER_SIZE={INITIAL_BUFFER_SIZE})")
                    self.buffer_progress.setValue(int(progress))
                else:
                    self.buffer_progress.setValue(0)
                    self.buffer_label.setText("缓冲区: 等待中...")
        except Exception as e:
            logger.error(f"更新网络状态时出错: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def update_stats(self):
        """定期更新统计信息"""
        if self.client_thread and self.client_thread.running and self.client_thread.client_protocol:
            client = self.client_thread.client_protocol
            current_time = time.time() - self.start_time
            
            if hasattr(client, 'total_bytes_received'):
                self.bytes_received = client.total_bytes_received
                bitrate = (self.bytes_received * 8) / (1000000 * max(1, current_time))  # Mbps
                
                self.bitrate_data['x'].append(current_time)
                self.bitrate_data['y'].append(bitrate)
                
                if len(self.bitrate_data['x']) > 60:
                    self.bitrate_data['x'] = self.bitrate_data['x'][-60:]
                    self.bitrate_data['y'] = self.bitrate_data['y'][-60:]
                
                self.bitrate_curve.setData(self.bitrate_data['x'], self.bitrate_data['y'])
            
            if hasattr(client, 'buffer_size'):
                self.buffer_size = client.buffer_size
                buffer_kb = self.buffer_size / 1024  # KB
                
                self.buffer_data['x'].append(current_time)
                self.buffer_data['y'].append(buffer_kb)
                
                if len(self.buffer_data['x']) > 60:
                    self.buffer_data['x'] = self.buffer_data['x'][-60:]
                    self.buffer_data['y'] = self.buffer_data['y'][-60:]
                
                self.buffer_curve.setData(self.buffer_data['x'], self.buffer_data['y'])
    
    def update_network_metrics(self, metrics):
        """更新网络质量指标显示"""
        if not hasattr(self, 'latency_data'):
            # 如果数据存储未初始化，则初始化它们
            self.latency_data = {'x': [], 'y': []}
            self.packet_loss_data = {'x': [], 'y': []}
            self.jitter_data = {'x': [], 'y': []}
        
        current_time = time.time() - self.start_time
        
        # 更新网络延迟图表
        if metrics.latency >= 0:
            self.latency_data['x'].append(current_time)
            self.latency_data['y'].append(metrics.latency)
            
            # 保持最近60秒的数据
            if len(self.latency_data['x']) > 60:
                self.latency_data['x'] = self.latency_data['x'][-60:]
                self.latency_data['y'] = self.latency_data['y'][-60:]
            
            self.latency_curve.setData(self.latency_data['x'], self.latency_data['y'])
        
        # 更新丢包率图表
        self.packet_loss_data['x'].append(current_time)
        self.packet_loss_data['y'].append(metrics.packet_loss)
        
        # 保持最近60秒的数据
        if len(self.packet_loss_data['x']) > 60:
            self.packet_loss_data['x'] = self.packet_loss_data['x'][-60:]
            self.packet_loss_data['y'] = self.packet_loss_data['y'][-60:]
        
        self.packet_loss_curve.setData(self.packet_loss_data['x'], self.packet_loss_data['y'])
        
        # 更新网络抖动图表
        self.jitter_data['x'].append(current_time)
        self.jitter_data['y'].append(metrics.jitter)
        
        # 保持最近60秒的数据
        if len(self.jitter_data['x']) > 60:
            self.jitter_data['x'] = self.jitter_data['x'][-60:]
            self.jitter_data['y'] = self.jitter_data['y'][-60:]
        
        self.jitter_curve.setData(self.jitter_data['x'], self.jitter_data['y'])
    
    def update_ui_state(self):
        """更新UI状态"""
        # 更新播放按钮状态
        server_running = bool(self.server_thread and self.server_thread.running)
        video_selected = bool(self.selected_video_path is not None)
        client_running = bool(self.client_thread and self.client_thread.running)
        
        self.play_video_btn.setEnabled(server_running and video_selected and not client_running)
        self.stop_video_btn.setEnabled(client_running)
        self.select_video_btn.setEnabled(not client_running)
    
    def closeEvent(self, event):
        """窗口关闭事件 - 改进的实现"""
        logger.info("正在关闭应用程序...")
        
        try:
            # 停止客户端线程
            if self.client_thread and self.client_thread.running:
                logger.info("正在停止客户端线程...")
                self.client_thread.stop()
                
                # 等待客户端线程结束
                if self.client_thread.wait(3000):
                    logger.info("客户端线程已停止")
                else:
                    logger.warning("客户端线程未能在3秒内停止，强制终止")
                    self.client_thread.terminate()
                    self.client_thread.wait(1000)
                
                # 确保FFplay进程被关闭
                if hasattr(self.client_thread, 'client_protocol') and self.client_thread.client_protocol:
                    client = self.client_thread.client_protocol
                    if hasattr(client, 'ffplay_process') and client.ffplay_process:
                        try:
                            if client.ffplay_process.poll() is None:
                                logger.info("正在关闭FFplay进程...")
                                client.ffplay_process.terminate()
                                client.ffplay_process.wait(timeout=2)
                        except:
                            try:
                                if client.ffplay_process:
                                    client.ffplay_process.kill()
                            except:
                                pass
            
            # 停止服务器线程
            if self.server_thread and self.server_thread.running:
                logger.info("正在停止服务器线程...")
                self.server_thread.stop()
                
                # 等待服务器线程结束
                if self.server_thread.wait(3000):
                    logger.info("服务器线程已停止")
                else:
                    logger.warning("服务器线程未能在3秒内停止，强制终止")
                    self.server_thread.terminate()
                    self.server_thread.wait(1000)
            
            # 停止网络监控线程
            if self.network_monitor_thread:
                self.network_monitor_thread.stop()
                self.network_monitor_thread.wait()
            
            logger.info("应用程序关闭完成")
            
        except Exception as e:
            logger.error(f"关闭应用程序时出错: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        event.accept()

    def dragEnterEvent(self, event: QDragEnterEvent):
        """处理拖入事件"""
        if event.mimeData().hasUrls():
            url = event.mimeData().urls()[0]
            if url.isLocalFile():
                file_path = url.toLocalFile()
                if file_path.lower().endswith(('.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv')):
                    event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        """处理放下事件"""
        url = event.mimeData().urls()[0]
        file_path = url.toLocalFile()
        self.selected_video_path = file_path
        self.video_status_label.setText(f"当前选择: {os.path.basename(file_path)}")
        self.play_video_btn.setEnabled(True)
        event.acceptProposedAction()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoPlayerGUI()
    window.show()
    sys.exit(app.exec_())
