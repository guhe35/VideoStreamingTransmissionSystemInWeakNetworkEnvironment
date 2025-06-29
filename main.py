import asyncio
import logging
import threading
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("quic-video-main")

# 导入服务端和客户端模块
try:
    from server import start_server
    from client import run_client
except ImportError as e:
    logger.error(f"导入模块失败: {e}")
    logger.error("请确保 server.py 和 client.py 文件在同一目录下")
    sys.exit(1)


class VideoStreamManager:
    def __init__(self):
        self.server_task = None
        self.client_task = None
        self.server_running = False
        self.client_running = False

    async def start_server(self):
        """启动服务端"""
        try:
            logger.info("正在启动QUIC视频服务器...")
            
            # 启动服务器
            await start_server()
            return True
            
        except Exception as e:
            logger.error(f"启动服务器时出错: {e}")
            return False

    async def start_client(self):
        """启动客户端"""
        try:
            logger.info("正在启动QUIC视频客户端...")
            
            # 启动客户端
            await run_client()
            return True
            
        except Exception as e:
            logger.error(f"启动客户端时出错: {e}")
            return False

    async def run_sequential(self):
        """顺序运行：先启动服务器，再启动客户端"""
        logger.info("=== 顺序运行模式 ===")
        
        try:
            # 步骤1: 启动服务器
            logger.info("步骤1: 启动服务器")
            self.server_task = asyncio.create_task(self.start_server())
            
            # 等待服务器启动完成
            logger.info("等待服务器启动...")
            await asyncio.sleep(2)  # 给服务器足够时间启动
            
            # 步骤2: 启动客户端
            logger.info("步骤2: 启动客户端")
            self.client_task = asyncio.create_task(self.start_client())
            
            # 等待任务完成
            await asyncio.gather(self.server_task, self.client_task)
            
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
            if self.server_task:
                self.server_task.cancel()
            if self.client_task:
                self.client_task.cancel()
        except Exception as e:
            logger.error(f"顺序运行出错: {e}")
            if self.server_task:
                self.server_task.cancel()
            if self.client_task:
                self.client_task.cancel()


async def main():
    """主函数"""
    print("顺序运行模式 先启动服务器，再启动客户端")
    
    try:
        
        manager = VideoStreamManager()
        # 顺序运行模式
        await manager.run_sequential()
            
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序运行出错: {e}")
        import traceback
        logger.error(traceback.format_exc())


def check_dependencies():
    """检查依赖"""
    required_modules = ['ffmpeg', 'aioquic', 'cryptography']
    missing_modules = []
    
    for module in required_modules:
        try:
            __import__(module)
        except ImportError:
            missing_modules.append(module)
    
    if missing_modules:
        logger.error(f"缺少以下依赖模块: {', '.join(missing_modules)}")
        logger.error("请运行: pip install -r requirements.txt")
        return False
    
    return True


def check_certificates():
    """检查证书文件"""
    cert_file = 'cert.pem'
    key_file = 'key.pem'
    
    if not os.path.exists(cert_file) or not os.path.exists(key_file):
        logger.error("缺少证书文件")
        logger.error("请确保 cert.pem 和 key.pem 文件在当前目录下")
        logger.error("可以使用 generate_cert.py 生成证书文件")
        return False
    
    logger.info("证书文件检查通过")
    return True


if __name__ == "__main__":
    print("正在检查环境...")
    
    # 检查依赖
    if not check_dependencies():
        sys.exit(1)
    
    # 检查证书文件
    if not check_certificates():
        sys.exit(1)
    
    print("环境检查完成！")
    print()
    
    # 运行主程序
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已退出")
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        sys.exit(1) 