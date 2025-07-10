import time
import threading
import socket
import struct
from typing import List, Dict, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class NetworkMetrics:
    """网络质量指标"""
    timestamp: float
    latency: float  # 毫秒
    packet_loss: float  # 百分比 (0-100)
    jitter: float  # 毫秒

class NetworkQualityMonitor:
    def __init__(self, target_host: str = "127.0.0.1", target_port: int = 12345, 
                 interval: float = 1.0, window_size: int = 60, probe_count: int = 10):
        """
        初始化网络质量监控器
        
        Args:
            target_host: 目标主机地址（默认为本地回环地址）
            target_port: 目标端口
            interval: 测量间隔（秒）
            window_size: 历史数据窗口大小
            probe_count: 每次测量发送的探测包数量
        """
        self.target_host = target_host
        self.target_port = target_port
        self.interval = interval
        self.window_size = window_size
        self.probe_count = probe_count
        
        self.metrics_history: List[NetworkMetrics] = []
        self.running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        
        # 用于计算丢包率的计数器
        self.total_probes = 0
        self.failed_probes = 0
        
        # 用于计算抖动的上一次延迟值
        self.last_latency = None
        
        # 启动UDP服务器（如果监控本地网络）
        self.server_socket = None
        if target_host in ("127.0.0.1", "localhost"):
            self._start_udp_server()
    
    def _start_udp_server(self):
        """启动UDP回显服务器"""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.server_socket.bind(("0.0.0.0", self.target_port))
            
            # 在新线程中运行服务器
            threading.Thread(target=self._run_server, daemon=True).start()
            logger.info(f"UDP回显服务器已启动在端口 {self.target_port}")
        except Exception as e:
            logger.error(f"启动UDP服务器失败: {e}")
    
    def _run_server(self):
        """运行UDP回显服务器"""
        while True:
            try:
                data, client_addr = self.server_socket.recvfrom(1024)
                if not self.running:  # 如果服务已停止，退出循环
                    break
                # 立即回显数据
                self.server_socket.sendto(data, client_addr)
            except Exception as e:
                if not self.running:  # 如果是正常停止，不记录错误
                    break
                logger.error(f"处理UDP数据时出错: {e}")
    
    def _measure_udp_metrics(self) -> tuple[float, bool]:
        """使用UDP连接测量网络指标"""
        try:
            # 创建UDP socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)  # 设置超时时间
            
            # 记录开始时间
            start_time = time.time()
            
            # 发送测试数据
            test_data = struct.pack('!d', start_time)  # 8字节的时间戳
            sock.sendto(test_data, (self.target_host, self.target_port))
            
            # 等待响应
            response, _ = sock.recvfrom(len(test_data))
            if len(response) != len(test_data):
                raise Exception("接收数据不完整")
            
            # 计算往返时间
            rtt = (time.time() - start_time) * 1000  # 转换为毫秒
            
            sock.close()
            return rtt, True
            
        except Exception as e:
            if self.running:  # 只在非正常停止时记录错误
                logger.error(f"UDP测量出错: {e}")
            return float('inf'), False
    
    def start(self):
        """启动网络质量监控"""
        if not self.running:
            self.running = True
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("网络质量监控已启动")
    
    def stop(self):
        """停止网络质量监控"""
        if self.running:
            self.running = False
            
            # 关闭服务器socket
            if self.server_socket:
                try:
                    self.server_socket.close()
                except:
                    pass
            
            if self.monitor_thread:
                self.monitor_thread.join(timeout=2)
            logger.info("网络质量监控已停止")
    
    def _monitor_loop(self):
        """监控循环"""
        while self.running:
            try:
                # 进行多次探测以获得更准确的结果
                latencies = []
                success_count = 0
                
                for _ in range(self.probe_count):
                    latency, success = self._measure_udp_metrics()
                    self.total_probes += 1
                    
                    if success and latency != float('inf'):
                        latencies.append(latency)
                        success_count += 1
                    else:
                        self.failed_probes += 1
                
                # 计算平均延迟
                if latencies:
                    avg_latency = sum(latencies) / len(latencies)
                else:
                    avg_latency = float('inf')
                
                # 计算丢包率
                packet_loss = ((self.probe_count - success_count) / self.probe_count) * 100
                
                # 计算抖动
                jitter = 0
                if self.last_latency is not None and avg_latency != float('inf'):
                    jitter = abs(avg_latency - self.last_latency)
                self.last_latency = avg_latency if avg_latency != float('inf') else self.last_latency
                
                # 创建新的度量记录
                metrics = NetworkMetrics(
                    timestamp=time.time(),
                    latency=avg_latency if avg_latency != float('inf') else -1,
                    packet_loss=packet_loss,
                    jitter=jitter
                )
                
                # 更新历史数据
                with self._lock:
                    self.metrics_history.append(metrics)
                    if len(self.metrics_history) > self.window_size:
                        self.metrics_history = self.metrics_history[-self.window_size:]
                
            except Exception as e:
                logger.error(f"网络质量监控出错: {e}")
            
            # 等待下一次测量
            time.sleep(self.interval)
    
    def get_current_metrics(self) -> Optional[NetworkMetrics]:
        """获取当前网络质量指标"""
        with self._lock:
            if self.metrics_history:
                return self.metrics_history[-1]
            return None
    
    def get_metrics_history(self) -> List[NetworkMetrics]:
        """获取历史网络质量指标"""
        with self._lock:
            return self.metrics_history.copy()
    
    def get_average_metrics(self, window: int = None) -> Dict[str, float]:
        """
        获取平均网络质量指标
        
        Args:
            window: 计算平均值的窗口大小（默认使用所有历史数据）
        """
        with self._lock:
            if not self.metrics_history:
                return {
                    'avg_latency': -1,
                    'avg_packet_loss': 0,
                    'avg_jitter': 0
                }
            
            history = self.metrics_history
            if window is not None:
                history = history[-window:]
            
            valid_latencies = [m.latency for m in history if m.latency >= 0]
            avg_latency = sum(valid_latencies) / len(valid_latencies) if valid_latencies else -1
            
            return {
                'avg_latency': avg_latency,
                'avg_packet_loss': sum(m.packet_loss for m in history) / len(history),
                'avg_jitter': sum(m.jitter for m in history) / len(history)
            } 