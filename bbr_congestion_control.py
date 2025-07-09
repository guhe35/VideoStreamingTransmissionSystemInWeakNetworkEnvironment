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
    
    # 新增：带宽评估准确性指标
    bandwidth_confidence: float = 0.0    # 带宽估算置信度 (0-1)
    bandwidth_variance: float = 0.0      # 带宽方差
    measurement_count: int = 0           # 测量样本数量
    last_measurement_time: float = 0.0   # 最后测量时间
    
    # 新增：丢包和拥塞指标  
    loss_rate: float = 0.0              # 丢包率
    retransmissions: int = 0            # 重传次数
    out_of_order_packets: int = 0       # 乱序包数量
    duplicate_acks: int = 0             # 重复ACK数量
    
    # 新增：网络接口限制
    interface_max_bandwidth: float = float('inf')  # 网络接口最大带宽
    interface_type: str = "unknown"                 # 网络接口类型

class BBRCongestionControl:
    """
    BBR (Bottleneck Bandwidth and Round-trip propagation time) 拥塞控制算法实现
    
    基于BBR v1论文的完整实现，包含完整的BBR状态机和参数管理
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
        
        # BBR核心参数 - 改进的初始化
        self.btl_bw = 0.0  # 瓶颈带宽，初始为0，等待真实测量
        self.rt_prop = float('inf')  # 往返传播时间，初始为无穷大
        self.min_rtt = float('inf')  # 最小RTT，初始为无穷大
        self.rtt = float('inf')      # 当前RTT，初始为无穷大
        
        # Round概念 - 改进的实现
        self.round_count = 0
        self.round_start_time = time.time()
        self.round_trip_count = 0
        self.next_round_delivered = 0
        
        # 带宽估算 - 改进的实现
        self.delivery_rate = 0.0   # 传输速率
        self.max_bandwidth = 0.0   # 最大带宽
        self.bandwidth_samples = [] # 带宽样本
        self.max_bandwidth_samples = 20  # 增加带宽样本数
        self.bandwidth_window_start = time.time()
        self.bandwidth_window_size = 10  # 带宽测量窗口大小 (rounds)
        
        # 带宽测量窗口 - 新增
        self.bandwidth_window = []  # 带宽测量窗口
        self.bandwidth_window_len = 10.0  # 带宽窗口长度 (seconds)
        
        # 带宽衰减机制 - 优化版本
        self.bandwidth_decay_factor = 0.95  # 带宽衰减因子
        self.last_bandwidth_decay = time.time()
        self.bandwidth_decay_interval = 60.0  # 带宽衰减间隔，增加到60秒
        
        # RTT估算 - 改进的实现
        self.rtt_samples = []      # RTT样本
        self.max_rtt_samples = 20  # 增加RTT样本数
        self.rtt_window_start = time.time()
        self.rtt_window_size = 10  # RTT测量窗口大小 (rounds)
        self.rtt_window_len = 10.0  # RTT窗口长度 (seconds)
        
        # RTT测量窗口 - 新增
        self.rtt_window = []  # RTT测量窗口
        self.min_rtt_win_len = 10.0  # 最小RTT窗口长度 (seconds)
        
        # 探测参数 - 按照BBR论文标准
        self.probe_bw_cycle = 0    # 带宽探测周期
        self.probe_rtt_cycle = 0   # RTT探测周期
        self.probe_bw_cycles = 8   # 带宽探测周期数
        self.probe_rtt_cycles = 4  # RTT探测周期数
        
        # 状态转换参数 - 按照BBR论文标准
        self.startup_threshold = 1.25    # 启动阶段阈值
        self.drain_threshold = 0.8       # 排空阶段阈值
        self.probe_rtt_timeout = 0.2     # RTT探测超时 (seconds)
        
        # 增益参数 - 按照BBR论文标准
        self.pacing_gain = 1.0
        self.cwnd_gain = 1.0
        self.pacing_gains = [1.25, 0.75, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # 8周期探测增益
        self.cwnd_gains = [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]       # 8周期拥塞窗口增益
        
        # 统计信息
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
        
        # 新增：带宽评估准确性
        self.bandwidth_confidence = 0.0
        self.bandwidth_variance = 0.0
        self.measurement_count = 0
        self.bandwidth_history = []  # 带宽历史记录
        self.max_bandwidth_history = 50  # 保留更多历史记录用于准确性分析
        
        # 新增：丢包和拥塞统计
        self.retransmissions = 0
        self.out_of_order_packets = 0
        self.duplicate_acks = 0
        
        # 新增：网络接口信息
        self.interface_max_bandwidth = self._detect_interface_bandwidth()
        self.interface_type = self._detect_interface_type()
        
        # Pacing机制 - 新增
        self.pacing_timer = None
        self.pacing_queue = []
        self.last_pacing_time = time.time()
        
        # 丢包检测 - 新增
        self.loss_events = 0
        self.last_loss_time = 0
        self.loss_rate = 0.0
        
        # 缓冲区估算 - 新增
        self.buffer_size = 0
        self.buffer_occupancy = 0.0
        
        logger.info("BBR拥塞控制算法初始化完成")
    
    def _detect_interface_bandwidth(self) -> float:
        """
        检测网络接口的最大带宽
        
        Returns:
            网络接口最大带宽 (bytes/second)
        """
        try:
            import psutil
            # 获取网络接口统计信息
            net_stats = psutil.net_if_stats()
            max_speed = 0
            
            for interface, stats in net_stats.items():
                if stats.isup and stats.speed > 0:  # 接口激活且有速度信息
                    # 转换为bytes/second (speed是Mbps)
                    interface_speed = stats.speed * 1024 * 1024 / 8
                    max_speed = max(max_speed, interface_speed)
            
            # 如果无法检测到，使用默认值
            if max_speed == 0:
                max_speed = 100 * 1024 * 1024  # 默认100Mbps
            
            logger.info(f"检测到网络接口最大带宽: {max_speed / 1024 / 1024:.2f} MB/s")
            return max_speed
            
        except ImportError:
            logger.warning("psutil未安装，无法检测网络接口带宽")
            return 100 * 1024 * 1024  # 默认100Mbps
        except Exception as e:
            logger.error(f"检测网络接口带宽时出错: {e}")
            return 100 * 1024 * 1024  # 默认100Mbps
    
    def _detect_interface_type(self) -> str:
        """
        检测网络接口类型
        
        Returns:
            网络接口类型字符串
        """
        try:
            import psutil
            net_stats = psutil.net_if_stats()
            
            for interface, stats in net_stats.items():
                if stats.isup:
                    if "ethernet" in interface.lower() or "eth" in interface.lower():
                        return "ethernet"
                    elif "wifi" in interface.lower() or "wlan" in interface.lower():
                        return "wifi"
                    elif "loopback" in interface.lower() or "lo" in interface.lower():
                        return "loopback"
            
            return "unknown"
            
        except Exception as e:
            logger.error(f"检测网络接口类型时出错: {e}")
            return "unknown"
    
    def update_round_counter(self, delivered: int):
        """
        更新Round计数器 - 改进的实现
        
        Args:
            delivered: 本次传输的字节数
        """
        self.bytes_delivered += delivered
        
        # 检查是否完成一个round
        if self.bytes_delivered >= self.next_round_delivered:
            self.round_count += 1
            self.round_trip_count += 1
            self.next_round_delivered = self.bytes_delivered + self.btl_bw * self.rt_prop
            
            # 更新round开始时间
            self.round_start_time = time.time()
            
            logger.debug(f"BBR Round完成: {self.round_count}, 传输字节: {delivered}")
    
    def update_bandwidth(self, delivered_bytes: int, delivered_time: float):
        """
        更新带宽估算 - 改进的实现，增加准确性验证
        
        Args:
            delivered_bytes: 传输的字节数
            delivered_time: 传输时间 (seconds)
        """
        if delivered_time <= 0:
            return
        
        # 计算传输速率
        delivery_rate = delivered_bytes / delivered_time
        current_time = time.time()
        
        # 检查带宽测量的合理性
        if not self._validate_bandwidth_measurement(delivery_rate, delivered_time):
            logger.debug(f"带宽测量被拒绝: {delivery_rate/1024/1024:.2f} MB/s (不合理)")
            return
        
        # 更新带宽历史记录
        self.bandwidth_history.append({
            'rate': delivery_rate,
            'timestamp': current_time,
            'bytes': delivered_bytes,
            'duration': delivered_time
        })
        
        # 保持历史记录大小
        if len(self.bandwidth_history) > self.max_bandwidth_history:
            self.bandwidth_history.pop(0)
        
        # 更新带宽测量窗口
        self.bandwidth_window.append({
            'rate': delivery_rate,
            'timestamp': current_time
        })
        
        # 保持窗口大小
        while len(self.bandwidth_window) > 0 and \
              current_time - self.bandwidth_window[0]['timestamp'] > self.bandwidth_window_len:
            self.bandwidth_window.pop(0)
        
        # 计算当前传输速率（使用窗口内统计方法）
        if self.bandwidth_window:
            window_rates = [sample['rate'] for sample in self.bandwidth_window]
            # 使用P95值而非最大值，减少异常值影响
            sorted_rates = sorted(window_rates)
            p95_index = int(len(sorted_rates) * 0.95)
            self.delivery_rate = sorted_rates[min(p95_index, len(sorted_rates) - 1)]
        else:
            self.delivery_rate = delivery_rate
        
        # 更新带宽样本
        self.bandwidth_samples.append(delivery_rate)
        if len(self.bandwidth_samples) > self.max_bandwidth_samples:
            self.bandwidth_samples.pop(0)
        
        # 计算带宽置信度和方差
        self._calculate_bandwidth_confidence()
        
        # 更新最大带宽 - 考虑置信度
        if self._should_update_max_bandwidth(delivery_rate):
            old_max = self.max_bandwidth
            self.max_bandwidth = delivery_rate
            self.btl_bw = self.max_bandwidth
            logger.info(f"BBR最大带宽更新: {old_max/1024/1024:.2f} -> {self.btl_bw/1024/1024:.2f} MB/s "
                       f"(置信度: {self.bandwidth_confidence:.2f})")
        
        # 带宽衰减机制 - 改进版本
        self._apply_bandwidth_decay()
        
        # 更新统计信息
        self.measurement_count += 1
        self.last_bandwidth_update = current_time
    
    def _apply_bandwidth_decay(self):
        """
        应用带宽衰减机制 - 改进版本
        """
        current_time = time.time()
        if current_time - self.last_bandwidth_decay > self.bandwidth_decay_interval:
            # 只在长时间没有新样本时才应用衰减
            if len(self.bandwidth_window) < 5:
                # 应用带宽衰减
                self.btl_bw *= self.bandwidth_decay_factor
                self.max_bandwidth *= self.bandwidth_decay_factor
                
                # 更新带宽样本
                if self.bandwidth_samples:
                    self.bandwidth_samples = [rate * self.bandwidth_decay_factor for rate in self.bandwidth_samples]
                
                self.last_bandwidth_decay = current_time
                logger.debug(f"BBR带宽衰减应用: 新带宽 {self.btl_bw/1024/1024:.2f} MB/s")
            else:
                # 有足够的带宽样本时，不进行衰减
                self.last_bandwidth_decay = current_time
                logger.debug("BBR带宽样本充足，跳过衰减")
    
    def _validate_bandwidth_measurement(self, bandwidth: float, measurement_time: float) -> bool:
        """
        验证带宽测量的合理性
        
        Args:
            bandwidth: 测量得到的带宽 (bytes/second)
            measurement_time: 测量时间 (seconds)
            
        Returns:
            测量是否合理
        """
        # 检查带宽是否超过物理接口限制
        if bandwidth > self.interface_max_bandwidth * 1.1:  # 允许10%的误差
            return False
        
        # 检查测量时间是否过短（可能不准确）
        if measurement_time < 0.001:  # 小于1ms的测量可能不准确
            return False
        
        # 检查带宽是否过低（可能是异常值）
        if bandwidth < 1024:  # 小于1KB/s可能是异常
            return False
        
        # 检查带宽是否与历史值差异过大
        if len(self.bandwidth_history) > 5:
            recent_rates = [h['rate'] for h in self.bandwidth_history[-5:]]
            avg_recent = sum(recent_rates) / len(recent_rates)
            
            # 如果新测量值与最近平均值差异超过500%，可能是异常
            if bandwidth > avg_recent * 5 or bandwidth < avg_recent * 0.2:
                return False
        
        return True
    
    def _calculate_bandwidth_confidence(self):
        """
        计算带宽估算的置信度
        """
        if len(self.bandwidth_history) < 3:
            self.bandwidth_confidence = 0.1  # 样本太少，置信度很低
            self.bandwidth_variance = 0.0
            return
        
        # 使用最近的测量计算方差
        recent_count = min(20, len(self.bandwidth_history))
        recent_rates = [h['rate'] for h in self.bandwidth_history[-recent_count:]]
        
        # 计算平均值和方差
        mean_rate = sum(recent_rates) / len(recent_rates)
        variance = sum((rate - mean_rate) ** 2 for rate in recent_rates) / len(recent_rates)
        self.bandwidth_variance = variance
        
        # 计算变异系数（标准差/平均值）
        if mean_rate > 0:
            cv = (variance ** 0.5) / mean_rate
            
            # 置信度与变异系数成反比
            # 变异系数越小，置信度越高
            if cv < 0.1:        # 变异系数 < 10%
                confidence = 0.9
            elif cv < 0.2:      # 变异系数 < 20%
                confidence = 0.8
            elif cv < 0.3:      # 变异系数 < 30%
                confidence = 0.7
            elif cv < 0.5:      # 变异系数 < 50%
                confidence = 0.6
            else:               # 变异系数 >= 50%
                confidence = 0.4
        else:
            confidence = 0.1
        
        # 考虑测量样本数量
        sample_factor = min(1.0, len(self.bandwidth_history) / 10.0)
        self.bandwidth_confidence = confidence * sample_factor
        
        logger.debug(f"带宽置信度: {self.bandwidth_confidence:.2f}, 方差: {self.bandwidth_variance:.0f}")
    
    def _should_update_max_bandwidth(self, new_bandwidth: float) -> bool:
        """
        判断是否应该更新最大带宽
        
        Args:
            new_bandwidth: 新的带宽测量值
            
        Returns:
            是否应该更新
        """
        # 如果是首次测量，直接更新
        if self.max_bandwidth == 0:
            return True
        
        # 如果新带宽明显更高，直接更新
        if new_bandwidth > self.max_bandwidth * 1.1:
            return True
        
        # 如果置信度很高且新带宽稍高，也更新
        if self.bandwidth_confidence > 0.7 and new_bandwidth > self.max_bandwidth:
            return True
        
        # 如果当前最大带宽的测量时间过久，允许更新
        current_time = time.time()
        if current_time - self.last_bandwidth_update > 30.0:  # 30秒没有更新
            return new_bandwidth > self.max_bandwidth * 0.8  # 允许适度降低
        
        return False
    
    def update_rtt(self, rtt: float):
        """
        更新RTT估算 - 改进的实现
        
        Args:
            rtt: 往返时间 (seconds)
        """
        if rtt <= 0:
            return
            
        self.rtt = rtt
        
        # 更新RTT测量窗口
        current_time = time.time()
        self.rtt_window.append({
            'rtt': rtt,
            'timestamp': current_time
        })
        
        # 保持窗口大小
        while len(self.rtt_window) > 0 and \
              current_time - self.rtt_window[0]['timestamp'] > self.rtt_window_len:
            self.rtt_window.pop(0)
        
        # 更新RTT样本
        self.rtt_samples.append(rtt)
        if len(self.rtt_samples) > self.max_rtt_samples:
            self.rtt_samples.pop(0)
        
        # RTT窗口机制 - 改进
        if current_time - self.rtt_window_start > self.rtt_window_len:
            # 重置RTT窗口
            self.rtt_samples.clear()
            self.rtt_window_start = current_time
            logger.debug("BBR RTT窗口重置")
        
        # 更新最小RTT - 这是BBR的核心
        if rtt < self.min_rtt:
            self.min_rtt = rtt
            self.rt_prop = self.min_rtt
            self.last_min_rtt_update = current_time
            logger.info(f"BBR最小RTT更新: {self.min_rtt*1000:.2f}ms")
        
        self.last_rtt_update = current_time
    
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
        
        # 更新Round计数器
        self.update_round_counter(packet_size)
        
        # 更新带宽估算 - 更频繁的更新
        current_time = time.time()
        if self.last_bandwidth_update > 0:
            elapsed = current_time - self.last_bandwidth_update
            if elapsed >= 0.05:  # 每50ms更新一次带宽，提高响应速度
                self.update_bandwidth(self.bytes_delivered, elapsed)
                self.bytes_delivered = 0
                self.last_bandwidth_update = current_time
        
        logger.debug(f"BBR数据包传输完成: {packet_size} bytes, RTT: {rtt*1000:.2f}ms")
    
    def on_loss_event(self):
        """
        丢包事件处理 - 改进的实现
        """
        current_time = time.time()
        self.loss_events += 1
        self.last_loss_time = current_time
        
        # 计算丢包率
        if self.packet_count > 0:
            self.loss_rate = self.loss_events / self.packet_count
        
        logger.debug(f"BBR检测到丢包事件，总丢包数: {self.loss_events}, 丢包率: {self.loss_rate:.4f}")
        
        # BBR对丢包的处理相对保守
        # 不立即降低拥塞窗口，而是继续基于带宽模型进行控制
        if self.state == BBRState.STARTUP:
            # 在启动阶段，丢包可能表示已达到瓶颈
            self._transition_to_drain()
    
    def get_metrics(self) -> BBRMetrics:
        """
        获取BBR算法指标 - 包含准确性指标的完整实现
        
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
            cwnd_gain=self.cwnd_gain,
            
            # 新增：带宽评估准确性指标
            bandwidth_confidence=self.bandwidth_confidence,
            bandwidth_variance=self.bandwidth_variance,
            measurement_count=self.measurement_count,
            last_measurement_time=self.last_bandwidth_update,
            
            # 新增：丢包和拥塞指标
            loss_rate=self.loss_rate,
            retransmissions=self.retransmissions,
            out_of_order_packets=self.out_of_order_packets,
            duplicate_acks=self.duplicate_acks,
            
            # 新增：网络接口限制
            interface_max_bandwidth=self.interface_max_bandwidth,
            interface_type=self.interface_type
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
            'cwnd_gain': self.cwnd_gain,
            'loss_events': self.loss_events,
            'loss_rate': self.loss_rate
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
                   f"在途数据: {metrics.inflight/1024:.1f}KB, "
                   f"丢包率: {state_info['loss_rate']:.4f}")
    
    def reset(self):
        """
        重置BBR算法状态
        """
        self.state = BBRState.STARTUP
        self.state_start_time = time.time()
        self.btl_bw = 0.0
        self.rt_prop = float('inf')
        self.min_rtt = float('inf')
        self.rtt = float('inf')
        self.delivery_rate = 0.0
        self.max_bandwidth = 0.0
        self.bandwidth_samples.clear()
        self.rtt_samples.clear()
        self.bandwidth_window.clear()
        self.rtt_window.clear()
        self.probe_bw_cycle = 0
        self.probe_rtt_cycle = 0
        self.pacing_gain = 1.0
        self.cwnd_gain = 1.0
        self.inflight = 0
        self.sent_packets.clear()
        self.delivered_packets.clear()
        self.next_packet_id = 0
        self.round_count = 0
        self.round_trip_count = 0
        self.next_round_delivered = 0
        self.loss_events = 0
        self.loss_rate = 0.0
        
        logger.info("BBR算法状态已重置") 