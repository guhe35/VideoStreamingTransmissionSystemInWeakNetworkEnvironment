#!/usr/bin/env python3

import os
import sys
import subprocess
import importlib.util

def check_module(module_name):
    """检查模块是否已安装"""
    return importlib.util.find_spec(module_name) is not None

def install_requirements():
    """安装所需依赖"""
    print("正在检查并安装所需依赖...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("依赖安装完成")
    except subprocess.CalledProcessError:
        print("依赖安装失败，请手动安装 requirements.txt 中的依赖")
        sys.exit(1)

def check_dependencies():
    """检查所需依赖是否已安装"""
    required_modules = ["PyQt5", "aioquic", "ffmpeg", "psutil", "cryptography"]
    missing_modules = []
    
    for module in required_modules:
        if not check_module(module):
            missing_modules.append(module)
    
    if missing_modules:
        print(f"缺少以下依赖: {', '.join(missing_modules)}")
        install_requirements()
        
        # 再次检查
        for module in missing_modules:
            if not check_module(module):
                print(f"依赖 {module} 安装失败，请手动安装")
                sys.exit(1)

def generate_certificates():
    """生成证书和私钥"""
    if not os.path.exists("cert.pem") or not os.path.exists("key.pem"):
        print("正在生成证书和私钥...")
        try:
            from generate_cert import generate_cert
            generate_cert()
        except Exception as e:
            print(f"生成证书失败: {str(e)}")
            sys.exit(1)
    else:
        print("证书和私钥已存在")

def start_gui():
    """启动GUI应用"""
    print("正在启动视频播放器...")
    try:
        from video_player_gui import VideoPlayerGUI
        from PyQt5.QtWidgets import QApplication
        
        app = QApplication(sys.argv)
        window = VideoPlayerGUI()
        window.show()
        sys.exit(app.exec_())
    except Exception as e:
        print(f"启动应用失败: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    print("QUIC视频播放器启动程序")
    print("=" * 50)
    
    # 检查依赖
    check_dependencies()
    
    # 生成证书
    generate_certificates()
    
    # 启动GUI
    start_gui() 