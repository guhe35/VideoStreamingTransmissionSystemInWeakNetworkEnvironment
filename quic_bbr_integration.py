import time
import logging
import asyncio
from typing import Optional, Dict, Any
from aioquic.quic.connection import QuicConnection
from aioquic.quic.configuration import QuicConfiguration
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.events import StreamDataReceived, ConnectionTerminated
from bbr_congestion_control import BBRCongestionControl, BBRMetrics

# 设置日志
logger = logging.getLogger("quic-bbr-integration")

class BBRQuicConnection(QuicConnection):
    """
    集成BBR拥塞控制的QUIC连接类
    
    这个类扩展了原始的QuicConnection，添加了BBR拥塞控制算法
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 初始化BBR拥塞控制 - 统一BBR实例
        self.bbr = BBRCongestionControl()
        
        # 连接统计信息
        self.connection_start_time = time.time()
        self.last_rtt_update = time.time()
        self.last_bandwidth_update = time.time()
        
        # 数据包跟踪 - 改进的实现
        self.sent_packets = {}  # 发送的数据包跟踪
        self.delivered_packets = {}  # 已传输的数据包跟踪
        self.next_packet_id = 0
        
        # 定期更新BBR状态
        self.bbr_update_task = None
        
        logger.info("BBR QUIC连接初始化完成")
    
    def _send_datagram(self, data: bytes, addr: tuple) -> None:
        """
        重写发送数据报方法，集成BBR拥塞控制 - 改进的实现
        """
        # 记录发送的数据包
        packet_id = self.next_packet_id
        self.next_packet_id += 1
        
        self.sent_packets[packet_id] = {
            'data': data,
            'size': len(data),
            'timestamp': time.time(),
            'addr': addr,
            'delivered': False
        }
        
        # 通知BBR算法数据包发送
        self.bbr.on_packet_sent(len(data))
        logger.debug(f"QUIC发送数据包: {len(data)} bytes, 地址: {addr}")
        
        # 调用原始发送方法
        super()._send_datagram(data, addr)
    
    def _handle_ack_frame(self, frame_type: int, buf: bytes) -> None:
        """
        重写ACK帧处理方法，更新BBR算法 - 改进的实现
        """
        # 调用原始ACK处理方法
        super()._handle_ack_frame(frame_type, buf)
        
        # 更新BBR算法
        self._update_bbr_from_ack()
    
    def _update_bbr_from_ack(self):
        """
        从ACK帧更新BBR算法 - 改进的实现
        """
        current_time = time.time()
        
        # 获取准确的RTT - 改进的RTT获取机制
        rtt = self._get_accurate_rtt()
        
        if rtt and rtt > 0:
            self.bbr.update_rtt(rtt)
            logger.debug(f"QUIC ACK RTT更新: {rtt*1000:.2f}ms")
        
        # 更新BBR状态
        self.bbr.update_state()
        
        # 计算新的拥塞窗口和发送速率
        new_cwnd = self.bbr.calculate_cwnd()
        new_pacing_rate = self.bbr.calculate_pacing_rate()
        
        # 应用新的拥塞控制参数 - 改进的参数应用机制
        self._apply_congestion_control_params(new_cwnd, new_pacing_rate)
        
        # 模拟数据包传输完成（基于ACK）
        if len(self.sent_packets) > 0:
            # 取最近发送的数据包
            latest_packet = max(self.sent_packets.items(), key=lambda x: x[1]['timestamp'])
            packet_size = latest_packet[1]['size']
            delivery_rtt = rtt if rtt and rtt > 0 else 0.01  # 默认10ms RTT
            
            # 通知BBR算法数据包传输完成
            self.bbr.on_packet_delivered(packet_size, delivery_rtt)
            logger.debug(f"QUIC ACK处理: 数据包大小 {packet_size} bytes, RTT {delivery_rtt*1000:.2f}ms")
        
        # 定期记录BBR指标
        if current_time - self.last_rtt_update > 1.0:  # 每秒记录一次
            self.bbr.log_metrics()
            self.last_rtt_update = current_time
    
    def _get_accurate_rtt(self) -> Optional[float]:
        """
        获取准确的RTT值 - 改进的RTT获取机制
        
        Returns:
            RTT值（秒），如果无法获取则返回None
        """
        # 尝试多种方式获取RTT
        if hasattr(self, '_loss') and hasattr(self._loss, 'smoothed_rtt'):
            rtt = self._loss.smoothed_rtt
            if rtt and rtt > 0:
                return rtt
        
        if hasattr(self, 'get_rtt'):
            try:
                rtt = self.get_rtt()
                if rtt and rtt > 0:
                    return rtt
            except:
                pass
        
        # 尝试从QUIC内部获取RTT
        if hasattr(self, '_loss') and hasattr(self._loss, 'rtt'):
            rtt = self._loss.rtt
            if rtt and rtt > 0:
                return rtt
        
        # 如果都无法获取，返回默认值
        logger.debug("无法获取准确RTT，使用默认值")
        return 0.01  # 10ms默认值
    
    def _apply_congestion_control_params(self, new_cwnd: int, new_pacing_rate: float):
        """
        应用拥塞控制参数 - 改进的参数应用机制
        
        Args:
            new_cwnd: 新的拥塞窗口大小
            new_pacing_rate: 新的发送速率
        """
        try:
            # 尝试更新QUIC内部的拥塞控制参数
            if hasattr(self, '_loss'):
                # 更新拥塞窗口
                if hasattr(self._loss, 'congestion_window'):
                    self._loss.congestion_window = new_cwnd
                    logger.debug(f"更新拥塞窗口: {new_cwnd/1024:.1f}KB")
                
                # 更新发送速率
                if hasattr(self._loss, 'pacing_rate'):
                    self._loss.pacing_rate = new_pacing_rate
                    logger.debug(f"更新发送速率: {new_pacing_rate/1024/1024:.2f}MB/s")
                
                # 更新其他相关参数
                if hasattr(self._loss, 'ssthresh'):
                    self._loss.ssthresh = new_cwnd
                
                # 尝试更新QUIC的发送限制
                if hasattr(self, 'max_data'):
                    # 根据BBR的拥塞窗口调整最大数据量
                    self.max_data = min(self.max_data, new_cwnd * 2)
                
                if hasattr(self, 'max_stream_data'):
                    # 调整流数据限制
                    self.max_stream_data = min(self.max_stream_data, new_cwnd)
                    
        except Exception as e:
            logger.warning(f"应用拥塞控制参数时出错: {e}")
    
    def get_bbr_metrics(self) -> Optional[BBRMetrics]:
        """
        获取BBR算法指标 - 统一的指标获取
        
        Returns:
            BBR算法指标，如果不可用则返回None
        """
        if self.bbr is None:
            return None
        
        metrics = self.bbr.get_metrics()
        if metrics and metrics.bandwidth > 0:
            logger.debug(f"获取BBR指标: 带宽={metrics.bandwidth/1024/1024:.2f}MB/s, RTT={metrics.rtt*1000:.2f}ms")
            return metrics
        
        logger.warning("BBR指标不可用")
        return None
    
    def get_bbr_state_info(self) -> Optional[Dict[str, Any]]:
        """
        获取BBR状态信息 - 统一的状态获取
        
        Returns:
            BBR状态信息，如果不可用则返回None
        """
        if self.bbr is None:
            return None
        
        state_info = self.bbr.get_state_info()
        if state_info:
            logger.debug(f"获取BBR状态: {state_info.get('state', 'unknown')}")
            return state_info
        
        logger.warning("BBR状态信息不可用")
        return None
    
    def start_bbr_monitoring(self):
        """
        启动BBR监控任务
        """
        if self.bbr_update_task is None:
            self.bbr_update_task = asyncio.create_task(self._bbr_update_loop())
            logger.info("BBR监控任务已启动")
    
    def stop_bbr_monitoring(self):
        """
        停止BBR监控任务
        """
        if self.bbr_update_task:
            self.bbr_update_task.cancel()
            self.bbr_update_task = None
            logger.info("BBR监控任务已停止")
    
    async def _bbr_update_loop(self):
        """
        BBR更新循环 - 改进的实现
        """
        try:
            while True:
                # 更新BBR状态
                self.bbr.update_state()
                
                # 计算新的拥塞控制参数
                new_cwnd = self.bbr.calculate_cwnd()
                new_pacing_rate = self.bbr.calculate_pacing_rate()
                
                # 应用新的参数
                self._apply_congestion_control_params(new_cwnd, new_pacing_rate)
                
                # 每100ms更新一次
                await asyncio.sleep(0.1)
                
        except asyncio.CancelledError:
            logger.info("BBR更新循环已取消")
        except Exception as e:
            logger.error(f"BBR更新循环出错: {e}")

class BBRQuicProtocol(QuicConnectionProtocol):
    """
    集成BBR拥塞控制的QUIC协议类 - 改进的实现
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # 延迟初始化BBR实例，避免重复创建
        self.bbr = None
        
        logger.info("BBR QUIC协议初始化完成")
    
    def _ensure_bbr_initialized(self):
        """
        确保BBR实例已初始化 - 统一BBR实例管理
        """
        if self.bbr is None:
            self.bbr = BBRCongestionControl()
            logger.info("BBR实例已初始化")
    
    def connection_made(self, transport):
        """
        连接建立时的处理 - 改进的实现
        """
        super().connection_made(transport)
        
        # 确保BBR已初始化
        self._ensure_bbr_initialized()
        
        # 启动BBR监控
        if hasattr(self._quic, 'start_bbr_monitoring'):
            self._quic.start_bbr_monitoring()
        
        logger.info("BBR QUIC连接已建立")
    
    def connection_lost(self, exc):
        """
        连接丢失时的处理 - 改进的实现
        """
        # 停止BBR监控
        if hasattr(self._quic, 'stop_bbr_monitoring'):
            self._quic.stop_bbr_monitoring()
        
        # 重置BBR状态
        if self.bbr:
            self.bbr.reset()
        
        super().connection_lost(exc)
        logger.info("BBR QUIC连接已丢失")
    
    def update_bbr_from_data(self, data_size: int, rtt: float = None):
        """
        从数据传输更新BBR算法 - 改进的实现
        
        Args:
            data_size: 数据大小 (bytes)
            rtt: 往返时间 (seconds)
        """
        # 确保BBR已初始化
        self._ensure_bbr_initialized()
        
        if self.bbr:
            # 通知BBR算法数据包发送
            self.bbr.on_packet_sent(data_size)
            
            # 如果有RTT信息，更新RTT
            if rtt is not None and rtt > 0:
                self.bbr.update_rtt(rtt)
            
            # 模拟数据包传输完成
            delivery_rtt = rtt if rtt and rtt > 0 else 0.01  # 默认10ms RTT
            self.bbr.on_packet_delivered(data_size, delivery_rtt)
            
            # 更新BBR状态
            self.bbr.update_state()
            
            logger.debug(f"BBR协议数据更新: {data_size} bytes, RTT: {delivery_rtt*1000:.2f}ms")
        else:
            logger.warning("BBR实例不存在，无法更新")
    
    def get_bbr_metrics(self) -> Optional[BBRMetrics]:
        """
        获取BBR算法指标 - 统一的指标获取
        
        Returns:
            BBR算法指标，如果不可用则返回None
        """
        # 确保BBR已初始化
        self._ensure_bbr_initialized()
        
        # 首先尝试从_quic对象获取
        if hasattr(self._quic, 'get_bbr_metrics'):
            metrics = self._quic.get_bbr_metrics()
            if metrics:
                logger.debug(f"从QUIC连接获取BBR指标: 带宽={metrics.bandwidth/1024/1024:.2f}MB/s, RTT={metrics.rtt*1000:.2f}ms")
                return metrics
        
        # 如果_quic对象没有BBR方法，使用本地的BBR实例
        if self.bbr:
            metrics = self.bbr.get_metrics()
            if metrics and metrics.bandwidth > 0:
                logger.debug(f"从本地BBR实例获取指标: 带宽={metrics.bandwidth/1024/1024:.2f}MB/s, RTT={metrics.rtt*1000:.2f}ms")
                return metrics
        
        logger.warning("无法获取BBR指标")
        return None
    
    def get_bbr_state_info(self) -> Optional[Dict[str, Any]]:
        """
        获取BBR状态信息 - 统一的状态获取
        
        Returns:
            BBR状态信息，如果不可用则返回None
        """
        # 确保BBR已初始化
        self._ensure_bbr_initialized()
        
        # 首先尝试从_quic对象获取
        if hasattr(self._quic, 'get_bbr_state_info'):
            state_info = self._quic.get_bbr_state_info()
            if state_info:
                logger.debug(f"从QUIC连接获取BBR状态: {state_info.get('state', 'unknown')}")
                return state_info
        
        # 如果_quic对象没有BBR方法，使用本地的BBR实例
        if self.bbr:
            state_info = self.bbr.get_state_info()
            if state_info:
                logger.debug(f"从本地BBR实例获取状态: {state_info.get('state', 'unknown')}")
                return state_info
        
        logger.warning("无法获取BBR状态信息")
        return None
    
    def start_bbr_monitoring(self):
        """
        启动BBR监控任务
        """
        # 确保BBR已初始化
        self._ensure_bbr_initialized()
        
        if self.bbr_update_task is None:
            self.bbr_update_task = asyncio.create_task(self._bbr_update_loop())
            logger.info("BBR监控任务已启动")
    
    def stop_bbr_monitoring(self):
        """
        停止BBR监控任务
        """
        if self.bbr_update_task:
            self.bbr_update_task.cancel()
            self.bbr_update_task = None
            logger.info("BBR监控任务已停止")
    
    async def _bbr_update_loop(self):
        """
        BBR更新循环 - 改进的实现
        """
        try:
            while True:
                # 确保BBR已初始化
                self._ensure_bbr_initialized()
                
                if self.bbr:
                    # 更新BBR状态
                    self.bbr.update_state()
                    
                    # 计算新的拥塞控制参数
                    new_cwnd = self.bbr.calculate_cwnd()
                    new_pacing_rate = self.bbr.calculate_pacing_rate()
                    
                    # 应用新的参数
                    if hasattr(self._quic, '_apply_congestion_control_params'):
                        self._quic._apply_congestion_control_params(new_cwnd, new_pacing_rate)
                
                # 每100ms更新一次
                await asyncio.sleep(0.1)
                
        except asyncio.CancelledError:
            logger.info("BBR更新循环已取消")
        except Exception as e:
            logger.error(f"BBR更新循环出错: {e}")

def create_bbr_quic_configuration() -> QuicConfiguration:
    """
    创建集成BBR的QUIC配置 - 改进的实现
    
    Returns:
        配置了BBR拥塞控制的QUIC配置
    """
    config = QuicConfiguration()
    
    # 设置ALPN协议
    config.alpn_protocols = ["quic-demo"]
    
    # 优化QUIC配置参数以配合BBR算法
    config.max_data = 10 * 1024 * 1024  # 10MB最大数据量
    config.max_stream_data = 5 * 1024 * 1024  # 5MB每个流最大数据量
    
    # 设置初始拥塞窗口大小 (BBR推荐值)
    config.initial_max_data = 10 * 1024 * 1024
    config.initial_max_stream_data_bidi_local = 5 * 1024 * 1024
    config.initial_max_stream_data_bidi_remote = 5 * 1024 * 1024
    config.initial_max_stream_data_uni = 5 * 1024 * 1024
    
    # 设置BBR相关的QUIC参数
    config.max_idle_timeout = 30.0  # 30秒空闲超时
    config.max_udp_payload_size = 1200  # 标准QUIC数据包大小
    
    logger.info("BBR QUIC配置创建完成")
    return config

def create_bbr_server_protocol(*args, **kwargs) -> BBRQuicProtocol:
    """
    创建BBR服务器协议实例
    
    Returns:
        BBR服务器协议实例
    """
    return BBRQuicProtocol(*args, **kwargs)

def create_bbr_client_protocol(*args, **kwargs) -> BBRQuicProtocol:
    """
    创建BBR客户端协议实例
    
    Returns:
        BBR客户端协议实例
    """
    return BBRQuicProtocol(*args, **kwargs)

class BBRNetworkMonitor:
    """
    BBR网络监控器 - 改进的实现
    
    用于监控和记录BBR算法的性能指标
    """
    
    def __init__(self):
        self.metrics_history = []
        self.max_history_size = 1000
        self.monitoring = False
        self.monitor_task = None
        self.protocol = None
    
    def start_monitoring(self, protocol: BBRQuicProtocol):
        """
        开始监控BBR协议 - 改进的实现
        
        Args:
            protocol: BBR QUIC协议实例
        """
        self.protocol = protocol
        self.monitoring = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("BBR网络监控已启动")
    
    def stop_monitoring(self):
        """
        停止监控 - 改进的实现
        """
        self.monitoring = False
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self.protocol = None
        logger.info("BBR网络监控已停止")
    
    async def _monitor_loop(self):
        """
        监控循环 - 改进的实现
        """
        try:
            while self.monitoring and self.protocol:
                # 获取BBR指标
                metrics = self.protocol.get_bbr_metrics()
                state_info = self.protocol.get_bbr_state_info()
                
                if metrics and state_info:
                    # 记录指标历史
                    record = {
                        'timestamp': time.time(),
                        'metrics': metrics,
                        'state_info': state_info
                    }
                    self.metrics_history.append(record)
                    
                    # 限制历史记录大小
                    if len(self.metrics_history) > self.max_history_size:
                        self.metrics_history.pop(0)
                    
                    # 记录性能指标
                    logger.debug(f"BBR监控 - 状态: {state_info['state']}, "
                               f"带宽: {metrics.bandwidth/1024/1024:.2f}MB/s, "
                               f"RTT: {metrics.rtt*1000:.2f}ms, "
                               f"拥塞窗口: {metrics.cwnd/1024:.1f}KB, "
                               f"在途数据: {metrics.inflight/1024:.1f}KB")
                
                # 每1秒监控一次
                await asyncio.sleep(1.0)
                
        except asyncio.CancelledError:
            logger.info("BBR监控循环已取消")
        except Exception as e:
            logger.error(f"BBR监控循环出错: {e}")
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """
        获取指标摘要 - 改进的实现
        
        Returns:
            指标摘要
        """
        if not self.metrics_history:
            return {}
        
        # 计算平均指标
        total_bandwidth = sum(r['metrics'].bandwidth for r in self.metrics_history)
        total_rtt = sum(r['metrics'].rtt for r in self.metrics_history)
        total_cwnd = sum(r['metrics'].cwnd for r in self.metrics_history)
        total_inflight = sum(r['metrics'].inflight for r in self.metrics_history)
        
        count = len(self.metrics_history)
        
        return {
            'avg_bandwidth': total_bandwidth / count if count > 0 else 0,
            'avg_rtt': total_rtt / count if count > 0 else 0,
            'avg_cwnd': total_cwnd / count if count > 0 else 0,
            'avg_inflight': total_inflight / count if count > 0 else 0,
            'total_samples': count,
            'monitoring_duration': time.time() - self.metrics_history[0]['timestamp'] if self.metrics_history else 0
        } 