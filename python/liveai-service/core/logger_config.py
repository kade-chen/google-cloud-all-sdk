import logging
import os
import socket
import struct
import platform
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# 仅 Linux 可用时再导入 fcntl
if platform.system() != "Windows":
    import fcntl


def get_ip():
    """
    获取当前主机或容器 IP：
    - 优先环境变量 POD_IP
    - Linux 下尝试 eth0 网卡
    - 回退主机名解析或 127.0.0.1
    """
    pod_ip = os.getenv("POD_IP")
    if pod_ip:
        return pod_ip

    # Linux 容器环境：尝试从 eth0 网卡读取 IP
    if platform.system() != "Windows":
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ip = socket.inet_ntoa(struct.unpack('!I', fcntl.ioctl(
                s.fileno(),
                0x8915,  # SIOCGIFADDR
                struct.pack('256s', b'eth0'[:15])
            )[20:24])[0].to_bytes(4, 'big'))
            return ip
        except Exception:
            pass

    # 其他情况（Windows、本地开发）
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


class DatedDirectoryFileHandler(TimedRotatingFileHandler):
    """
    每天一个文件夹，每2小时一个日志文件（包含容器IP + 时间段）
    """
    def __init__(self, log_dir, filename, level, when="H", interval=2, backupCount=84, encoding="utf-8"):
        self.log_dir = log_dir
        self.filename = filename
        self.level = level
        self.ip = get_ip()
        self.interval = interval
        os.makedirs(log_dir, exist_ok=True)

        log_path = self._get_dated_filepath()
        super().__init__(log_path, when=when, interval=interval, backupCount=backupCount, encoding=encoding)
        self.setLevel(level)

    def _get_dated_filepath(self):
        """生成带日期与小时段的日志文件路径"""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        hour = now.hour
        start_hour = (hour // self.interval) * self.interval
        end_hour = (start_hour + self.interval) % 24
        time_range = f"{start_hour:02d}-{end_hour:02d}"

        dated_dir = os.path.join(self.log_dir, date_str)
        os.makedirs(dated_dir, exist_ok=True)

        base_name, ext = os.path.splitext(self.filename)
        filename = f"{self.ip}.{base_name}.{date_str}_{time_range}{ext}"
        return os.path.join(dated_dir, filename)

    def doRollover(self):
        """日志轮转时更新路径"""
        self.baseFilename = os.path.abspath(self._get_dated_filepath())
        super().doRollover()


def setup_logger(service_name=None):
    """初始化日志系统"""
    if service_name is None:
        service_name = os.getenv("SERVICE_NAME", "liveai-service-api")

    log_dir = f"/data/logs/{service_name}"
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    logger = logging.getLogger()
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

    info_handler = DatedDirectoryFileHandler(log_dir, "info.log", level=logging.INFO)
    info_handler.setFormatter(formatter)
    info_handler.addFilter(lambda record: record.levelno < logging.ERROR)

    error_handler = DatedDirectoryFileHandler(log_dir, "error.log", level=logging.ERROR)
    error_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    if not logger.handlers:
        logger.addHandler(info_handler)
        logger.addHandler(error_handler)
        logger.addHandler(console_handler)

    return logger
