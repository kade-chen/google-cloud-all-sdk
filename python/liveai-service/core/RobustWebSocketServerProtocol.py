import logging
import time
from collections import defaultdict

# 配置日志
logger = logging.getLogger('websocket_server')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


class PortScanProtector:
    """端口扫描防护系统"""

    def __init__(self):
        self.connection_attempts = defaultdict(list)
        self.banned_ips = {}
        self.scan_threshold = 10  # 每分钟10次连接尝试视为扫描
        self.ban_duration = 1800  # 封禁30分钟(1800秒)

    def should_accept(self, remote_address):
        """检查是否应该接受连接"""
        ip = remote_address[0] if isinstance(remote_address, tuple) else remote_address
        now = time.time()

        # 检查封禁列表
        if ip in self.banned_ips:
            if now < self.banned_ips[ip]:
                return False
            # 封禁过期
            del self.banned_ips[ip]

        # 记录连接尝试
        self.connection_attempts[ip].append(now)

        # 清理旧记录(保留最近5分钟)
        self.connection_attempts[ip] = [
            t for t in self.connection_attempts[ip]
            if now - t < 300
        ]

        # 检查扫描行为
        if len(self.connection_attempts[ip]) > self.scan_threshold:
            self.banned_ips[ip] = now + self.ban_duration
            logger.warning(f"IP {ip} banned for {self.ban_duration} seconds")
            return False

        return True


# 全局扫描防护器
scan_protector = PortScanProtector()


async def robustness_middleware(websocket):
    """处理连接前检查和协议兼容性的中间件"""
    try:
        # 1. 应用端口扫描防护
        if not scan_protector.should_accept(websocket.remote_address):
            logger.info(f"拒绝潜在扫描: {websocket.remote_address}")
            await websocket.close(code=1008, reason="Forbidden")
            return

        # 2. 检查传输是否可用
        if websocket.transport is None:
            logger.debug("连接传输不可用，无法处理请求")
            return

        # 3. 获取请求头（新方法）
        headers = {}
        try:
            # 对于支持 get_headers() 的版本
            if hasattr(websocket, 'get_headers'):
                headers = await websocket.get_headers()
            # 对于旧版本兼容
            elif hasattr(websocket, 'request'):
                headers = websocket.request.headers
        except AttributeError:
            logger.debug("无法获取请求头，使用空头部")

        # 4. 检查必要头信息
        required_headers = ["Host", "Upgrade", "Connection", "Sec-WebSocket-Key"]
        missing_headers = [h for h in required_headers if h not in headers]

        if missing_headers:
            logger.info(f"缺少必要请求头 {missing_headers}: {websocket.remote_address}")
            await websocket.close(
                code=1002,
                reason="Invalid WebSocket request headers"
            )
            return

        # 5. 验证WebSocket升级信息
        connection_header = headers.get("Connection", "").lower()
        upgrade_header = headers.get("Upgrade", "").lower()

        if "upgrade" not in connection_header or "websocket" not in upgrade_header:
            logger.info(f"无效的 WebSocket 请求: {websocket.remote_address}")
            await websocket.close(
                code=1002,
                reason="Invalid WebSocket upgrade request"
            )
            return
    except Exception as e:
        logger.error(f"连接检查错误: {e}")
        try:
            # 尝试优雅地关闭连接
            await websocket.close(
                code=1011,
                reason="Internal server error"
            )
        except:
            pass
        return