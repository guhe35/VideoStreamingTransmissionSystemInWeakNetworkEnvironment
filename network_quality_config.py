# -*- coding: utf-8 -*-
"""
统一的网络质量等级配置
"""

# 网络质量等级定义 - 统一使用7级系统
NETWORK_QUALITY_LEVELS = {
    "ULTRA": {
        "video_bitrate": "4000k", 
        "video_scale": "2560:1440", 
        "audio_bitrate": "192k", 
        "video_codec": "libx265",
        "preset": "medium",
        "crf": "16",
        "gop_size": "90",
        "max_latency": 20,         # 延迟 <= 20ms
        "max_packet_loss": 0.05,   # 丢包率 <= 0.05%
        "max_jitter": 5,           # 抖动 <= 5ms
        "min_bandwidth": 15000000, # 带宽 >= 15Mbps
        "description": "超高速网络 - 2K超清"
    },
    "EXCELLENT": {
        "video_bitrate": "2500k", 
        "video_scale": "1920:1080", 
        "audio_bitrate": "128k", 
        "video_codec": "libx265",
        "preset": "fast",
        "crf": "18",
        "gop_size": "60",
        "max_latency": 50,         # 延迟 <= 50ms
        "max_packet_loss": 0.1,    # 丢包率 <= 0.1%
        "max_jitter": 10,          # 抖动 <= 10ms
        "min_bandwidth": 8000000,  # 带宽 >= 8Mbps
        "description": "优秀网络 - 1080p高清"
    },
    "GOOD": {
        "video_bitrate": "1500k", 
        "video_scale": "1280:720", 
        "audio_bitrate": "96k", 
        "video_codec": "libx265",
        "preset": "fast",
        "crf": "22",
        "gop_size": "45",
        "max_latency": 100,        # 延迟 <= 100ms
        "max_packet_loss": 1.0,    # 丢包率 <= 1%
        "max_jitter": 20,          # 抖动 <= 20ms
        "min_bandwidth": 4000000,  # 带宽 >= 4Mbps
        "description": "良好网络 - 720p高清"
    },
    "FAIR": {
        "video_bitrate": "800k", 
        "video_scale": "960:540", 
        "audio_bitrate": "64k", 
        "video_codec": "libx265",
        "preset": "fast",
        "crf": "26",
        "gop_size": "30",
        "max_latency": 200,        # 延迟 <= 200ms
        "max_packet_loss": 3.0,    # 丢包率 <= 3%
        "max_jitter": 50,          # 抖动 <= 50ms
        "min_bandwidth": 2000000,  # 带宽 >= 2Mbps
        "description": "一般网络 - 540p标清"
    },
    "POOR": {
        "video_bitrate": "400k", 
        "video_scale": "640:360", 
        "audio_bitrate": "48k", 
        "video_codec": "libx264",
        "preset": "ultrafast",
        "crf": "30",
        "gop_size": "15",
        "max_latency": 500,        # 延迟 <= 500ms
        "max_packet_loss": 5.0,    # 丢包率 <= 5%
        "max_jitter": 100,         # 抖动 <= 100ms
        "min_bandwidth": 1000000,  # 带宽 >= 1Mbps
        "description": "较差网络 - 360p流畅"
    },
    "BAD": {
        "video_bitrate": "200k", 
        "video_scale": "480:270", 
        "audio_bitrate": "32k", 
        "video_codec": "libx264",
        "preset": "ultrafast",
        "crf": "35",
        "gop_size": "10",
        "max_latency": 1000,       # 延迟 <= 1000ms
        "max_packet_loss": 10.0,   # 丢包率 <= 10%
        "max_jitter": 200,         # 抖动 <= 200ms
        "min_bandwidth": 500000,   # 带宽 >= 500Kbps
        "description": "很差网络 - 270p低清"
    },
    "CRITICAL": {
        "video_bitrate": "100k", 
        "video_scale": "320:180", 
        "audio_bitrate": "24k", 
        "video_codec": "libx264",
        "preset": "ultrafast",
        "crf": "40",
        "gop_size": "5",
        "max_latency": float('inf'), # 延迟 > 1000ms
        "max_packet_loss": float('inf'), # 丢包率 > 10%
        "max_jitter": float('inf'), # 抖动 > 200ms
        "min_bandwidth": 0,        # 带宽 < 500Kbps
        "description": "极差网络 - 180p极低清"
    }
}

# 统一的网络质量评估权重配置
NETWORK_QUALITY_WEIGHTS = {
    "latency_weight": 0.35,      # 延迟权重 35%
    "packet_loss_weight": 0.25,  # 丢包率权重 25%
    "jitter_weight": 0.20,       # 抖动权重 20%
    "bandwidth_weight": 0.20     # 带宽权重 20%
}

# 网络质量评分阈值
NETWORK_QUALITY_THRESHOLDS = {
    "ULTRA": 95,        # 95-100分
    "EXCELLENT": 80,    # 80-94分
    "GOOD": 65,         # 65-79分
    "FAIR": 50,         # 50-64分
    "POOR": 35,         # 35-49分
    "BAD": 20,          # 20-34分
    "CRITICAL": 0       # 0-19分
}

def get_network_quality_levels():
    """获取网络质量等级列表"""
    return list(NETWORK_QUALITY_LEVELS.keys())

def get_network_quality_config(level: str):
    """获取指定等级的网络质量配置"""
    return NETWORK_QUALITY_LEVELS.get(level, NETWORK_QUALITY_LEVELS["FAIR"])

def get_network_quality_weights():
    """获取网络质量评估权重"""
    return NETWORK_QUALITY_WEIGHTS.copy()

def get_network_quality_thresholds():
    """获取网络质量评分阈值"""
    return NETWORK_QUALITY_THRESHOLDS.copy() 