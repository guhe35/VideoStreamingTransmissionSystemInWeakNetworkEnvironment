import time
import math
import logging
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from enum import Enum

# 设置日志
logger = logging.getLogger("bbr-congestion-control")

class BBRState(Enum):
    """BBR状态枚举"""
    STARTUP = "startup"      # 启动阶段：快速探测带宽
    DRAIN = "drain"         # 排空阶段：降低发送速率以排空缓冲区
    PROBE_BW = "probe_bw"   # 带宽探测阶段：周期性探测带宽
    PROBE_RTT = "probe_rtt" # RTT探测阶段：降低拥塞窗口以测量最小RTT

@dataclass
class BBRMetrics:
    """BBR算法指标"""
    bandwidth: float = 0.0          # 带宽估算 (bytes/second)
    min_rtt: float = float('inf')   # 最小RTT (seconds)
    rtt: float = 0.0               # 当前RTT (seconds)
    delivery_rate: float = 0.0     # 传输速率 (bytes/second)
    cwnd: int = 0                  # 拥塞窗口大小 (bytes)
    pacing_rate: float = 0.0       # 发送速率 (bytes/second)
    btl_bw: float = 0.0           # 瓶颈带宽 (bytes/second)
    rt_prop: float = float('inf')  # 往返传播时间 (seconds)
    inflight: int = 0              # 在途数据量 (bytes)
    pacing_gain: float = 1.0       # 当前发送增益
    cwnd_gain: float = 1.0         # 当前拥塞窗口增益

class BBRCongestionControl:
    """
    BBR (Bottleneck Bandwidth and Round-trip propagation time) 拥塞控制算法实现
    
    基于BBR v1论文的改进实现，包含完整的BBR状态机和参数管理
    """
    
    def __init__(self, initial_cwnd: int = 10 * 1024, initial_pacing_rate: float = 1.25e6):
        """
        初始化BBR拥塞控制
        
        Args:
            initial_cwnd: 初始拥塞窗口大小 (bytes)
            initial_pacing_rate: 初始发送速率 (bytes/second)
        """
        # BBR状态
        self.state = BBRState.STARTUP
        self.state_start_time = time.time()
        self.previous_state = None
        
        # 拥塞控制参数
        self.cwnd = initial_cwnd
        self.pacing_rate = initial_pacing_rate
        
        # BBR核心参数
        self.btl_bw = 0.0          # 瓶颈带宽
        self.rt_prop = float('inf') # 往返传播时间
        self.min_rtt = float('inf') # 最小RTT
        self.rtt = 0.0             # 当前RTT
        
        # 带宽估算 - 改进的实现
        self.delivery_rate = 0.0   # 传输速率
        self.max_bandwidth = 0.0   # 最大带宽
        self.bandwidth_samples = [] # 带宽样本
        self.max_bandwidth_samples = 10  # 最大带宽样本数
        self.bandwidth_window_start = time.time()
        self.bandwidth_window_size = 10  # 带宽测量窗口大小 (rounds)
        
        # RTT估算 - 改进的实现
        self.rtt_samples = []      # RTT样本
        self.max_rtt_samples = 10  # 最大RTT样本数
        self.rtt_window_start = time.time()
        self.rtt_window_size = 10  # RTT测量窗口大小 (rounds)
        
        # 探测参数 - 按照BBR论文标准
        self.probe_bw_cycle = 0    # 带宽探测周期
        self.probe_rtt_cycle = 0   # RTT探测周期
        self.probe_bw_cycles = 8   # 带宽探测周期数
        self.probe_rtt_cycles = 4  # RTT探测周期数
        
        # 状态转换参数 - 按照BBR论文标准
        self.startup_threshold = 1.25    # 启动阶段阈值
        self.drain_threshold = 0.8       # 排空阶段阈值
        self.probe_rtt_timeout = 0.2     # RTT探测超时 (seconds)
        self.min_rtt_win_len = 10.0      # 最小RTT窗口长度 (seconds)
        
        # 增益参数 - 按照BBR论文标准
        self.pacing_gain = 1.0
        self.cwnd_gain = 1.0
        self.pacing_gains = [1.25, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # 8周期探测增益
        self.cwnd_gains = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]       # 8周期拥塞窗口增益
        
        # 统计信息
        self.round_count = 0
        self.packet_count = 0
        self.bytes_sent = 0
        self.bytes_delivered = 0
        self.inflight = 0  # 在途数据量
        
        # 时间戳
        self.last_update_time = time.time()
        self.last_rtt_update = time.time()
        self.last_bandwidth_update = time.time()
        self.last_min_rtt_update = time.time()
        
        # 数据包跟踪 - 改进的实现
        self.sent_packets = {}  # 发送的数据包跟踪
        self.delivered_packets = {}  # 已传输的数据包跟踪
        self.next_packet_id = 0
        
        logger.info("BBR拥塞控制算法初始化完成")
    
    def update_bandwidth(self, delivered_bytes: int, delivered_time: float):
        """
        更新带宽估算 - 改进的实现
        
        Args:
            delivered_bytes: 传输的字节数
            delivered_time: 传输时间 (seconds)
        """
        if delivered_time <= 0:
            return
        
        # 计算传输速率
        delivery_rate = delivered_bytes / delivered_time
        
        # 更新带宽样本
        self.bandwidth_samples.append(delivery_rate)
        if len(self.bandwidth_samples) > self.max_bandwidth_samples:
            self.bandwidth_samples.pop(0)
        
        # 计算当前传输速率（使用滑动窗口平均）
        if self.bandwidth_samples:
            self.delivery_rate = sum(self.bandwidth_samples) / len(self.bandwidth_samples)
        
        # 更新最大带宽 - 这是BBR的核心
        if delivery_rate > self.max_bandwidth:
            self.max_bandwidth = delivery_rate
            self.btl_bw = self.max_bandwidth
            logger.info(f"BBR最大带宽更新: {self.btl_bw/1024/1024:.2f} MB/s")
        
        self.last_bandwidth_update = time.time()
    
    def update_rtt(self, rtt: float):
        """
        更新RTT估算 - 改进的实现
        
        Args:
            rtt: 往返时间 (seconds)
        """
        if rtt <= 0:
            return
            
        self.rtt = rtt
        
        # 更新RTT样本
        self.rtt_samples.append(rtt)
        if len(self.rtt_samples) > self.max_rtt_samples:
            self.rtt_samples.pop(0)
        
        # 更新最小RTT - 这是BBR的核心
        if rtt < self.min_rtt:
            self.min_rtt = rtt
            self.rt_prop = self.min_rtt
            self.last_min_rtt_update = time.time()
            logger.info(f"BBR最小RTT更新: {self.min_rtt*1000:.2f}ms")
        
        self.last_rtt_update = time.time()
    
    def calculate_cwnd(self) -> int:
        """
        计算拥塞窗口大小 - 改进的实现
        
        Returns:
            拥塞窗口大小 (bytes)
        """
        if self.btl_bw <= 0 or self.rt_prop >= float('inf'):
            return self.cwnd
        
        # BBR拥塞窗口计算公式: cwnd = btl_bw * rt_prop * cwnd_gain
        target_cwnd = int(self.btl_bw * self.rt_prop * self.cwnd_gain)
        
        # 应用状态相关的调整
        if self.state == BBRState.STARTUP:
            # 启动阶段：快速增加拥塞窗口
            target_cwnd = int(target_cwnd * 2)
        elif self.state == BBRState.DRAIN:
            # 排空阶段：降低拥塞窗口
            target_cwnd = int(target_cwnd * 0.8)
        elif self.state == BBRState.PROBE_RTT:
            # RTT探测阶段：使用最小拥塞窗口
            target_cwnd = max(4 * 1024, int(target_cwnd * 0.5))
        
        # 限制拥塞窗口范围
        min_cwnd = 4 * 1024  # 最小4KB
        max_cwnd = 10 * 1024 * 1024  # 最大10MB
        
        self.cwnd = max(min_cwnd, min(max_cwnd, target_cwnd))
        return self.cwnd
    
    def calculate_pacing_rate(self) -> float:
        """
        计算发送速率 - 改进的实现
        
        Returns:
            发送速率 (bytes/second)
        """
        if self.btl_bw <= 0:
            return self.pacing_rate
        
        # 基础发送速率
        base_rate = self.btl_bw
        
        # 根据状态调整发送速率
        if self.state == BBRState.STARTUP:
            # 启动阶段：快速增加发送速率
            pacing_gain = 2.0
        elif self.state == BBRState.DRAIN:
            # 排空阶段：降低发送速率
            pacing_gain = 0.8
        elif self.state == BBRState.PROBE_BW:
            # 带宽探测阶段：周期性调整发送速率
            cycle_position = self.probe_bw_cycle % self.probe_bw_cycles
            pacing_gain = self.pacing_gains[cycle_position]
        elif self.state == BBRState.PROBE_RTT:
            # RTT探测阶段：降低发送速率
            pacing_gain = 0.5
        else:
            pacing_gain = 1.0
        
        self.pacing_gain = pacing_gain
        self.pacing_rate = base_rate * pacing_gain
        
        # 限制发送速率范围
        min_rate = 1024  # 最小1KB/s
        max_rate = 100 * 1024 * 1024  # 最大100MB/s
        
        self.pacing_rate = max(min_rate, min(max_rate, self.pacing_rate))
        return self.pacing_rate
    
    def update_state(self):
        """
        更新BBR状态 - 改进的实现
        """
        current_time = time.time()
        state_duration = current_time - self.state_start_time
        old_state = self.state
        
        if self.state == BBRState.STARTUP:
            # 启动阶段：当带宽增长低于阈值时，进入排空阶段
            if self.btl_bw > 0 and self.delivery_rate > 0:
                growth_rate = self.delivery_rate / self.btl_bw
                if growth_rate < self.startup_threshold or state_duration > 10.0:
                    self._transition_to_drain()
        
        elif self.state == BBRState.DRAIN:
            # 排空阶段：当缓冲区排空后，进入带宽探测阶段
            if self.rtt <= self.min_rtt * 1.25 or state_duration > 2.0:
                self._transition_to_probe_bw()
        
        elif self.state == BBRState.PROBE_BW:
            # 带宽探测阶段：周期性探测带宽
            self.probe_bw_cycle += 1
            if self.probe_bw_cycle >= self.probe_bw_cycles:
                self.probe_bw_cycle = 0
            
            # 检查是否需要RTT探测
            if self.should_probe_rtt():
                self._transition_to_probe_rtt()
        
        elif self.state == BBRState.PROBE_RTT:
            # RTT探测阶段：短暂降低拥塞窗口以测量最小RTT
            self.probe_rtt_cycle += 1
            if self.probe_rtt_cycle >= self.probe_rtt_cycles or state_duration > self.probe_rtt_timeout:
                self._transition_to_probe_bw()
        
        # 如果状态发生变化，记录日志
        if old_state != self.state:
            logger.info(f"BBR状态变化: {old_state.value} -> {self.state.value}")
    
    def _transition_to_drain(self):
        """转换到排空状态"""
        self.state = BBRState.DRAIN
        self.state_start_time = time.time()
        self.pacing_gain = 0.8
        self.cwnd_gain = 1.0
        logger.info("BBR状态转换: STARTUP -> DRAIN")
    
    def _transition_to_probe_bw(self):
        """转换到带宽探测状态"""
        self.state = BBRState.PROBE_BW
        self.state_start_time = time.time()
        self.probe_bw_cycle = 0
        self.pacing_gain = 1.0
        self.cwnd_gain = 2.0
        logger.info("BBR状态转换: DRAIN/PROBE_RTT -> PROBE_BW")
    
    def _transition_to_probe_rtt(self):
        """转换到RTT探测状态"""
        self.state = BBRState.PROBE_RTT
        self.state_start_time = time.time()
        self.probe_rtt_cycle = 0
        self.pacing_gain = 0.5
        self.cwnd_gain = 0.5
        logger.info("BBR状态转换: PROBE_BW -> PROBE_RTT")
    
    def should_probe_rtt(self) -> bool:
        """
        判断是否应该进行RTT探测 - 改进的实现
        
        Returns:
            是否应该进行RTT探测
        """
        # 如果RTT显著增加，进行RTT探测
        if self.rtt > self.min_rtt * 2.0:
            return True
        
        # 定期进行RTT探测
        time_since_last_probe = time.time() - self.last_min_rtt_update
        if time_since_last_probe > self.min_rtt_win_len:
            return True
        
        return False
    
    def on_packet_sent(self, packet_size: int):
        """
        数据包发送事件 - 改进的实现
        
        Args:
            packet_size: 数据包大小 (bytes)
        """
        # 记录发送的数据包
        packet_id = self.next_packet_id
        self.next_packet_id += 1
        
        self.sent_packets[packet_id] = {
            'size': packet_size,
            'timestamp': time.time(),
            'delivered': False
        }
        
        self.packet_count += 1
        self.bytes_sent += packet_size
        self.inflight += packet_size
        
        logger.debug(f"BBR数据包发送: {packet_size} bytes, 总计: {self.bytes_sent} bytes")
    
    def on_packet_delivered(self, packet_size: int, rtt: float):
        """
        数据包传输完成事件 - 改进的实现
        
        Args:
            packet_size: 数据包大小 (bytes)
            rtt: 往返时间 (seconds)
        """
        self.bytes_delivered += packet_size
        self.inflight = max(0, self.inflight - packet_size)
        self.update_rtt(rtt)
        
        # 更新带宽估算
        current_time = time.time()
        if self.last_bandwidth_update > 0:
            elapsed = current_time - self.last_bandwidth_update
            if elapsed >= 0.1:  # 每100ms更新一次带宽
                self.update_bandwidth(self.bytes_delivered, elapsed)
                self.bytes_delivered = 0
                self.last_bandwidth_update = current_time
        
        logger.debug(f"BBR数据包传输完成: {packet_size} bytes, RTT: {rtt*1000:.2f}ms")
    
    def on_loss_event(self):
        """
        丢包事件处理 - 改进的实现
        """
        # BBR对丢包的处理相对保守
        # 不立即降低拥塞窗口，而是继续基于带宽模型进行控制
        logger.debug("BBR检测到丢包事件")
        
        # 可以在这里添加一些保守的调整
        if self.state == BBRState.STARTUP:
            # 在启动阶段，丢包可能表示已达到瓶颈
            self._transition_to_drain()
    
    def get_metrics(self) -> BBRMetrics:
        """
        获取BBR算法指标 - 改进的实现
        
        Returns:
            BBR算法指标
        """
        return BBRMetrics(
            bandwidth=self.btl_bw,
            min_rtt=self.min_rtt,
            rtt=self.rtt,
            delivery_rate=self.delivery_rate,
            cwnd=self.cwnd,
            pacing_rate=self.pacing_rate,
            btl_bw=self.btl_bw,
            rt_prop=self.rt_prop,
            inflight=self.inflight,
            pacing_gain=self.pacing_gain,
            cwnd_gain=self.cwnd_gain
        )
    
    def get_state_info(self) -> Dict:
        """
        获取状态信息 - 改进的实现
        
        Returns:
            状态信息字典
        """
        return {
            'state': self.state.value,
            'state_duration': time.time() - self.state_start_time,
            'probe_bw_cycle': self.probe_bw_cycle,
            'probe_rtt_cycle': self.probe_rtt_cycle,
            'packet_count': self.packet_count,
            'round_count': self.round_count,
            'inflight': self.inflight,
            'pacing_gain': self.pacing_gain,
            'cwnd_gain': self.cwnd_gain
        }
    
    def log_metrics(self):
        """
        记录BBR算法指标 - 改进的实现
        """
        metrics = self.get_metrics()
        state_info = self.get_state_info()
        
        logger.info(f"BBR指标 - 状态: {state_info['state']}, "
                   f"带宽: {metrics.bandwidth/1024/1024:.2f}MB/s, "
                   f"RTT: {metrics.rtt*1000:.2f}ms, "
                   f"拥塞窗口: {metrics.cwnd/1024:.1f}KB, "
                   f"发送速率: {metrics.pacing_rate/1024/1024:.2f}MB/s, "
                   f"在途数据: {metrics.inflight/1024:.1f}KB")
    
    def reset(self):
        """
        重置BBR算法状态
        """
        self.state = BBRState.STARTUP
        self.state_start_time = time.time()
        self.btl_bw = 0.0
        self.rt_prop = float('inf')
        self.min_rtt = float('inf')
        self.rtt = 0.0
        self.delivery_rate = 0.0
        self.max_bandwidth = 0.0
        self.bandwidth_samples.clear()
        self.rtt_samples.clear()
        self.probe_bw_cycle = 0
        self.probe_rtt_cycle = 0
        self.pacing_gain = 1.0
        self.cwnd_gain = 1.0
        self.inflight = 0
        self.sent_packets.clear()
        self.delivered_packets.clear()
        self.next_packet_id = 0
        
        logger.info("BBR算法状态已重置") 