import sys
import os
import asyncio
import logging
import time
import threading
import ssl
import subprocess
import random
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel, 
                           QVBoxLayout, QHBoxLayout, QWidget, QFileDialog, 
                           QComboBox, QProgressBar, QTextEdit, QGroupBox)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QFont

# 导入服务端和客户端模块
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'test'))
from server import start_server, stop_server, VideoServer, analyze_video, NETWORK_QUALITY
from client import VideoClient, ALPN_PROTOCOLS

# 导入BBR拥塞控制
from bbr_congestion_control import BBRMetrics
from quic_bbr_integration import BBRNetworkMonitor

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
                            # 确保BBR已初始化
                            self._ensure_bbr_initialized()
                            
                            # 获取BBR指标
                            bbr_metrics = self.get_bbr_metrics()
                            bbr_state = self.get_bbr_state_info()
                            
                            # 发送网络状态到GUI
                            network_info = {
                                'bytes_received': getattr(self, 'total_bytes_received', 0),
                                'quality': getattr(self, 'network_quality', 'UNKNOWN'),
                                'buffer_size': getattr(self, 'buffer_size', 0),
                                'bbr_metrics': bbr_metrics,
                                'bbr_state': bbr_state
                            }
                            self.parent_thread.network_status.emit(network_info)
                            
                            # 更新播放状态
                            playback_info = {
                                'streaming': getattr(self, 'streaming', False),
                                'playback_started': getattr(self, 'playback_started', False)
                            }
                            self.parent_thread.playback_status.emit(playback_info)
                            
                            # 添加调试日志
                            if bbr_metrics:
                                logger.debug(f"GUI BBR指标: 状态={bbr_state.get('state', 'unknown') if bbr_state else 'unknown'}, "
                                           f"带宽={bbr_metrics.bandwidth/1024/1024:.2f}MB/s, "
                                           f"RTT={bbr_metrics.rtt*1000:.2f}ms, "
                                           f"在途数据={bbr_metrics.inflight/1024:.1f}KB")
                            else:
                                logger.debug("GUI BBR指标不可用")
                            
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
                            # 获取BBR指标
                            bbr_metrics = client.get_bbr_metrics()
                            bbr_state = client.get_bbr_state_info()
                            
                            # 获取QUIC连接统计信息
                            network_info = {
                                'bytes_received': client.total_bytes_received,
                                'quality': client.network_quality,
                                'buffer_size': getattr(client, 'buffer_size', 0),
                                'bbr_metrics': bbr_metrics,
                                'bbr_state': bbr_state
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


class VideoPlayerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.server_thread = None
        self.client_thread = None
        self.selected_video_path = None
        self.init_ui()
        
        # 启动更新定时器
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_stats)
        self.update_timer.start(1000)  # 每秒更新一次
        
        # 初始化状态
        self.bytes_received = 0
        self.start_time = time.time()
        
        # 自动启动服务器
        QTimer.singleShot(500, self.start_server)  # 延迟500毫秒启动服务器，确保UI已完全加载
    
    def init_ui(self):
        self.setWindowTitle('QUIC视频播放器 - BBR优化版')
        self.setGeometry(100, 100, 900, 700)
        
        # 创建主布局
        main_layout = QVBoxLayout()
        
        # 服务器状态显示（隐藏控制按钮，只显示状态）
        server_group = QGroupBox("服务器状态")
        server_layout = QHBoxLayout()
        
        self.server_status_label = QLabel("服务器状态: 未启动")
        server_layout.addWidget(self.server_status_label)
        
        # 保留按钮但隐藏它们，以便保持原有功能
        self.start_server_btn = QPushButton("启动服务器")
        self.start_server_btn.clicked.connect(self.start_server)
        self.start_server_btn.hide()
        
        self.stop_server_btn = QPushButton("停止服务器")
        self.stop_server_btn.clicked.connect(self.stop_server)
        self.stop_server_btn.setEnabled(False)
        self.stop_server_btn.hide()
        
        server_group.setLayout(server_layout)
        main_layout.addWidget(server_group)
        
        # 视频选择区域
        video_group = QGroupBox("视频选择")
        video_layout = QHBoxLayout()
        
        self.select_video_btn = QPushButton("选择视频文件")
        self.select_video_btn.clicked.connect(self.select_video)
        video_layout.addWidget(self.select_video_btn)
        
        self.video_path_label = QLabel("未选择视频文件")
        video_layout.addWidget(self.video_path_label)
        
        self.play_video_btn = QPushButton("播放视频")
        self.play_video_btn.clicked.connect(self.play_video)
        self.play_video_btn.setEnabled(False)
        video_layout.addWidget(self.play_video_btn)
        
        self.stop_video_btn = QPushButton("停止播放")
        self.stop_video_btn.clicked.connect(self.stop_video)
        self.stop_video_btn.setEnabled(False)
        video_layout.addWidget(self.stop_video_btn)
        
        video_group.setLayout(video_layout)
        main_layout.addWidget(video_group)
        
        # 视频信息区域
        info_group = QGroupBox("视频信息")
        info_layout = QVBoxLayout()
        
        self.video_info_label = QLabel("未加载视频")
        info_layout.addWidget(self.video_info_label)
        
        info_group.setLayout(info_layout)
        main_layout.addWidget(info_group)
        
        # 网络状态区域
        network_group = QGroupBox("网络状态")
        network_layout = QVBoxLayout()
        
        self.network_quality_label = QLabel("网络质量: -")
        network_layout.addWidget(self.network_quality_label)
        
        self.bitrate_label = QLabel("比特率: - Mbps")
        network_layout.addWidget(self.bitrate_label)
        
        self.buffer_label = QLabel("缓冲区: - KB")
        network_layout.addWidget(self.buffer_label)
        
        self.buffer_progress = QProgressBar()
        self.buffer_progress.setRange(0, 100)
        self.buffer_progress.setValue(0)
        network_layout.addWidget(self.buffer_progress)
        
        # BBR算法状态显示 - 改进的显示
        self.bbr_state_label = QLabel("BBR状态: -")
        network_layout.addWidget(self.bbr_state_label)
        
        self.bbr_bandwidth_label = QLabel("BBR带宽: - MB/s")
        network_layout.addWidget(self.bbr_bandwidth_label)
        
        self.bbr_rtt_label = QLabel("BBR RTT: - ms")
        network_layout.addWidget(self.bbr_rtt_label)
        
        self.bbr_cwnd_label = QLabel("BBR拥塞窗口: - KB")
        network_layout.addWidget(self.bbr_cwnd_label)
        
        # 新增BBR指标显示
        self.bbr_inflight_label = QLabel("BBR在途数据: - KB")
        network_layout.addWidget(self.bbr_inflight_label)
        
        self.bbr_pacing_gain_label = QLabel("BBR发送增益: -")
        network_layout.addWidget(self.bbr_pacing_gain_label)
        
        self.bbr_cwnd_gain_label = QLabel("BBR拥塞窗口增益: -")
        network_layout.addWidget(self.bbr_cwnd_gain_label)
        
        network_group.setLayout(network_layout)
        main_layout.addWidget(network_group)
        
        # 播放状态区域
        playback_group = QGroupBox("播放状态")
        playback_layout = QVBoxLayout()
        
        self.playback_status_label = QLabel("播放状态: 未开始")
        playback_layout.addWidget(self.playback_status_label)
        
        self.streaming_status_label = QLabel("流传输: 未开始")
        playback_layout.addWidget(self.streaming_status_label)
        
        playback_group.setLayout(playback_layout)
        main_layout.addWidget(playback_group)
        
        # 日志区域
        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout()
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group)
        
        # 设置主布局
        central_widget = QWidget()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # 初始状态
        self.update_ui_state()
    
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
            self.start_server_btn.setEnabled(False)
            self.stop_server_btn.setEnabled(True)
            self.log("正在启动服务器...")
    
    def stop_server(self):
        """停止服务器"""
        if self.server_thread and self.server_thread.running:
            self.server_thread.stop()
            self.log("正在停止服务器...")
            self.start_server_btn.setEnabled(True)
            self.stop_server_btn.setEnabled(False)
    
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
            self.video_path_label.setText(os.path.basename(file_path))
            self.log(f"已选择视频文件: {os.path.basename(file_path)}")
            self.update_ui_state()
    
    def play_video(self):
        """播放视频"""
        if not self.selected_video_path:
            self.log("错误: 未选择视频文件")
            return
        
        if not self.server_thread or not self.server_thread.running:
            self.log("错误: 服务器未启动")
            return
        
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
                
                # 重置BBR指标显示
                self.bbr_state_label.setText("BBR状态: -")
                self.bbr_bandwidth_label.setText("BBR带宽: - MB/s")
                self.bbr_rtt_label.setText("BBR RTT: - ms")
                self.bbr_cwnd_label.setText("BBR拥塞窗口: - KB")
                self.bbr_inflight_label.setText("BBR在途数据: - KB")
                self.bbr_pacing_gain_label.setText("BBR发送增益: -")
                self.bbr_cwnd_gain_label.setText("BBR拥塞窗口增益: -")
                
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
        info_text = f"分辨率: {info['width']}x{info['height']}<br>"
        info_text += f"视频编解码器: {info['codec']}<br>"
        info_text += f"帧率: {info['fps']}fps<br>"
        info_text += f"音频编解码器: {info['audio_codec']}<br>"
        info_text += f"时长: {float(info['duration']):.2f}秒"
        
        self.video_info_label.setText(info_text)
        self.log(f"已加载视频信息: {info['width']}x{info['height']}, {info['codec']}, {info['fps']}fps")
    
    def update_network_status(self, info):
        """更新网络状态 - 改进的实现"""
        self.bytes_received = info['bytes_received']
        self.network_quality_label.setText(f"网络质量: {info['quality']}")
        
        # 计算比特率
        elapsed = time.time() - self.start_time
        if elapsed > 0:
            bitrate = (self.bytes_received * 8) / (elapsed * 1000000)  # Mbps
            self.bitrate_label.setText(f"比特率: {bitrate:.2f} Mbps")
        
        # 更新缓冲区
        buffer_size = info['buffer_size'] / 1024  # KB
        self.buffer_label.setText(f"缓冲区: {buffer_size:.1f} KB")
        
        # 更新缓冲区进度条
        if buffer_size > 0:
            buffer_percent = min(100, (buffer_size / (INITIAL_BUFFER_SIZE / 1024)) * 100)
            self.buffer_progress.setValue(int(buffer_percent))
        
        # 更新BBR算法状态 - 改进的BBR指标显示
        if 'bbr_metrics' in info and info['bbr_metrics']:
            bbr_metrics = info['bbr_metrics']
            bbr_state = info.get('bbr_state', {})
            
            # 更新BBR状态
            state_name = bbr_state.get('state', 'unknown')
            self.bbr_state_label.setText(f"BBR状态: {state_name}")
            
            # 更新BBR带宽
            bandwidth_mbps = bbr_metrics.bandwidth / 1024 / 1024
            self.bbr_bandwidth_label.setText(f"BBR带宽: {bandwidth_mbps:.2f} MB/s")
            
            # 更新BBR RTT
            rtt_ms = bbr_metrics.rtt * 1000
            self.bbr_rtt_label.setText(f"BBR RTT: {rtt_ms:.2f} ms")
            
            # 更新BBR拥塞窗口
            cwnd_kb = bbr_metrics.cwnd / 1024
            self.bbr_cwnd_label.setText(f"BBR拥塞窗口: {cwnd_kb:.1f} KB")
            
            # 更新BBR在途数据
            inflight_kb = bbr_metrics.inflight / 1024
            self.bbr_inflight_label.setText(f"BBR在途数据: {inflight_kb:.1f} KB")
            
            # 更新BBR增益参数
            pacing_gain = bbr_metrics.pacing_gain
            self.bbr_pacing_gain_label.setText(f"BBR发送增益: {pacing_gain:.2f}")
            
            cwnd_gain = bbr_metrics.cwnd_gain
            self.bbr_cwnd_gain_label.setText(f"BBR拥塞窗口增益: {cwnd_gain:.2f}")
            
            # 添加调试日志
            logger.info(f"GUI BBR指标更新: 状态={state_name}, "
                       f"带宽={bandwidth_mbps:.2f}MB/s, "
                       f"RTT={rtt_ms:.2f}ms, "
                       f"拥塞窗口={cwnd_kb:.1f}KB, "
                       f"在途数据={inflight_kb:.1f}KB, "
                       f"发送增益={pacing_gain:.2f}, "
                       f"拥塞窗口增益={cwnd_gain:.2f}")
        else:
            # 如果没有BBR指标，显示默认值
            self.bbr_state_label.setText("BBR状态: 不可用")
            self.bbr_bandwidth_label.setText("BBR带宽: - MB/s")
            self.bbr_rtt_label.setText("BBR RTT: - ms")
            self.bbr_cwnd_label.setText("BBR拥塞窗口: - KB")
            self.bbr_inflight_label.setText("BBR在途数据: - KB")
            self.bbr_pacing_gain_label.setText("BBR发送增益: -")
            self.bbr_cwnd_gain_label.setText("BBR拥塞窗口增益: -")
            
            # 添加调试日志
            logger.warning("GUI BBR指标不可用")
    
    def update_playback_status(self, info):
        """更新播放状态"""
        streaming = "已开始" if info['streaming'] else "未开始"
        self.streaming_status_label.setText(f"流传输: {streaming}")
        
        playback = "已开始" if info['playback_started'] else "缓冲中"
        if info['streaming']:
            self.playback_status_label.setText(f"播放状态: {playback}")
        else:
            self.playback_status_label.setText("播放状态: 未开始")
    
    def update_stats(self):
        """定期更新统计信息 - 改进的实现"""
        # 如果有活跃的客户端连接，更新网络状态
        if self.client_thread and self.client_thread.running and self.client_thread.client_protocol:
            client = self.client_thread.client_protocol
            
            # 确保即使没有新的数据包到达，也能更新网络状态显示
            if hasattr(client, 'total_bytes_received') and hasattr(client, 'network_quality'):
                # 获取BBR指标
                bbr_metrics = client.get_bbr_metrics()
                bbr_state = client.get_bbr_state_info()
                
                # 添加调试信息
                if bbr_metrics:
                    logger.debug(f"GUI获取到BBR指标: 带宽={bbr_metrics.bandwidth/1024/1024:.2f}MB/s, RTT={bbr_metrics.rtt*1000:.2f}ms")
                else:
                    logger.debug("GUI BBR指标获取失败")
                
                if bbr_state:
                    logger.debug(f"GUI获取到BBR状态: {bbr_state.get('state', 'unknown')}")
                else:
                    logger.debug("GUI BBR状态获取失败")
                
                network_info = {
                    'bytes_received': client.total_bytes_received,
                    'quality': client.network_quality,
                    'buffer_size': getattr(client, 'buffer_size', 0),
                    'bbr_metrics': bbr_metrics,
                    'bbr_state': bbr_state
                }
                self.update_network_status(network_info)
    
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
            
            logger.info("应用程序关闭完成")
            
        except Exception as e:
            logger.error(f"关闭应用程序时出错: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoPlayerGUI()
    window.show()
    sys.exit(app.exec_())
