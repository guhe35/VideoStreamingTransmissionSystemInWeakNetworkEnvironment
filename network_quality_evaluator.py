# -*- coding: utf-8 -*-
"""
统一的网络质量评估函数
"""

from network_quality_config import get_network_quality_weights, get_network_quality_thresholds
from network_monitor import NetworkMetrics
import logging

logger = logging.getLogger(__name__)

def evaluate_network_quality(metrics: NetworkMetrics) -> str:
    """
    统一的网络质量评估函数
    
    Args:
        metrics: NetworkMetrics网络指标
        
    Returns:
        网络质量等级字符串
    """
    if not metrics:
        return "FAIR"  # 默认等级
    
    # 获取评估权重
    weights = get_network_quality_weights()
    latency_weight = weights["latency_weight"]
    packet_loss_weight = weights["packet_loss_weight"]
    jitter_weight = weights["jitter_weight"]
    bandwidth_weight = weights["bandwidth_weight"]
    
    # 获取评分阈值
    thresholds = get_network_quality_thresholds()
    
    # 计算综合评分
    quality_score = 0
    
    # 延迟评分 (35%)
    latency_score = _evaluate_latency(metrics.latency)
    quality_score += latency_score * latency_weight * 100
    
    # 丢包率评分 (25%)
    packet_loss_score = _evaluate_packet_loss(metrics.packet_loss)
    quality_score += packet_loss_score * packet_loss_weight * 100
    
    # 抖动评分 (20%)
    jitter_score = _evaluate_jitter(metrics.jitter)
    quality_score += jitter_score * jitter_weight * 100
    
    # 带宽评分 (20%)
    bandwidth_score = _evaluate_bandwidth(metrics.bandwidth)
    quality_score += bandwidth_score * bandwidth_weight * 100
    
    # 根据综合评分确定质量等级
    if quality_score >= thresholds["ULTRA"]:
        return "ULTRA"
    elif quality_score >= thresholds["EXCELLENT"]:
        return "EXCELLENT"
    elif quality_score >= thresholds["GOOD"]:
        return "GOOD"
    elif quality_score >= thresholds["FAIR"]:
        return "FAIR"
    elif quality_score >= thresholds["POOR"]:
        return "POOR"
    elif quality_score >= thresholds["BAD"]:
        return "BAD"
    else:
        return "CRITICAL"

def _evaluate_latency(latency: float) -> float:
    """
    评估延迟质量 (0-1)
    
    Args:
        latency: 延迟 (毫秒)
        
    Returns:
        延迟评分 (0-1)
    """
    if latency <= 0:
        return 0.0
    
    if latency <= 20:              # 延迟 <= 20ms (超高速网络)
        return 1.0
    elif latency <= 50:            # 延迟 <= 50ms (优秀网络)
        return 0.95
    elif latency <= 100:           # 延迟 <= 100ms (良好网络)
        return 0.90
    elif latency <= 200:           # 延迟 <= 200ms (一般网络)
        return 0.80
    elif latency <= 500:           # 延迟 <= 500ms (较差网络)
        return 0.60
    elif latency <= 1000:          # 延迟 <= 1000ms (很差网络)
        return 0.40
    else:                          # 延迟 > 1000ms (极差网络)
        return 0.20

def _evaluate_packet_loss(packet_loss: float) -> float:
    """
    评估丢包率质量 (0-1)
    
    Args:
        packet_loss: 丢包率 (百分比)
        
    Returns:
        丢包率评分 (0-1)
    """
    if packet_loss <= 0.05:        # 丢包率 <= 0.05% (超高速网络)
        return 1.0
    elif packet_loss <= 0.1:       # 丢包率 <= 0.1% (优秀网络)
        return 0.95
    elif packet_loss <= 1.0:       # 丢包率 <= 1% (良好网络)
        return 0.90
    elif packet_loss <= 3.0:       # 丢包率 <= 3% (一般网络)
        return 0.80
    elif packet_loss <= 5.0:       # 丢包率 <= 5% (较差网络)
        return 0.60
    elif packet_loss <= 10.0:      # 丢包率 <= 10% (很差网络)
        return 0.40
    else:                          # 丢包率 > 10% (极差网络)
        return 0.20

def _evaluate_jitter(jitter: float) -> float:
    """
    评估抖动质量 (0-1)
    
    Args:
        jitter: 抖动 (毫秒)
        
    Returns:
        抖动评分 (0-1)
    """
    if jitter <= 5:                # 抖动 <= 5ms (超高速网络)
        return 1.0
    elif jitter <= 10:             # 抖动 <= 10ms (优秀网络)
        return 0.95
    elif jitter <= 20:             # 抖动 <= 20ms (良好网络)
        return 0.90
    elif jitter <= 50:             # 抖动 <= 50ms (一般网络)
        return 0.80
    elif jitter <= 100:            # 抖动 <= 100ms (较差网络)
        return 0.60
    elif jitter <= 200:            # 抖动 <= 200ms (很差网络)
        return 0.40
    else:                          # 抖动 > 200ms (极差网络)
        return 0.20

def _evaluate_bandwidth(bandwidth: float) -> float:
    """
    评估带宽质量 (0-1)
    
    Args:
        bandwidth: 带宽 (bytes/second)
        
    Returns:
        带宽评分 (0-1)
    """
    # 转换为Mbps
    bandwidth_mbps = bandwidth / 1000000
    
    if bandwidth_mbps >= 15:       # 带宽 >= 15Mbps (超高速网络)
        return 1.0
    elif bandwidth_mbps >= 8:      # 带宽 >= 8Mbps (优秀网络)
        return 0.95
    elif bandwidth_mbps >= 4:      # 带宽 >= 4Mbps (良好网络)
        return 0.90
    elif bandwidth_mbps >= 2:      # 带宽 >= 2Mbps (一般网络)
        return 0.80
    elif bandwidth_mbps >= 1:      # 带宽 >= 1Mbps (较差网络)
        return 0.60
    elif bandwidth_mbps >= 0.5:    # 带宽 >= 500Kbps (很差网络)
        return 0.40
    else:                          # 带宽 < 500Kbps (极差网络)
        return 0.20 