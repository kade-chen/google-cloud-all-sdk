import asyncio
import base64
import json
import logging
import os
from asyncio import Queue
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import websockets
from websockets import ConnectionClosedError, InvalidHandshake, WebSocketException, Headers, ConnectionClosedOK
from websockets.http11 import Request, Response
from websockets.server import ServerConnection

from core.base_info import BaseInfo
from core.gemini_client import create_gsession
from core.logger_config import setup_logger
from core.session_future import complete_session_future, create_session_future, get_session_future, \
    cancel_session_future
from core.warmup_pool import gemini_pool
from core.websocket_handler import handle_client

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.logging import LoggingInstrumentor

# ======== 全局连接统计 ========
active_connections = set()
total_connections = 0

# 清理默认 logging
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logger = setup_logger()

# 降低不相关库的日志噪声
for name in [
    'google', 'google.auth', 'google.auth.transport', 'google.auth.transport.requests',
    'urllib3.connectionpool', 'google.generativeai', 'httpx', 'httpcore',
]:
    logging.getLogger(name).setLevel(logging.ERROR)

# 初始化 OpenTelemetry
resource = Resource(attributes={"service.name": "liveai-service-api"})

# Tracer
traces_url = os.getenv("TRACES_URL")
trace_provider = TracerProvider(resource=resource)
trace.set_tracer_provider(trace_provider)
trace_exporter = OTLPSpanExporter(endpoint=traces_url)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
tracer = trace.get_tracer(__name__)

# Metrics
metrics_url = os.getenv("METRICS_URL")
metric_exporter = OTLPMetricExporter(endpoint=metrics_url)
metric_reader = PeriodicExportingMetricReader(metric_exporter)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Metrics counters
active_connections_metrics = meter.create_up_down_counter("websocket.active_connections", description="Active WS connections")
messages_sent = meter.create_counter("websocket.messages.sent", description="Total messages sent")
messages_received = meter.create_counter("websocket.messages.received", description="Total messages received")

# Logging
logging.basicConfig(level=logging.INFO)
LoggingInstrumentor().instrument(set_logging_format=True)


# ==== 忽略握手阶段的 EOFError / ConnectionResetError ====
class IgnoreHandshakeNoise(logging.Filter):
    """忽略握手阶段常见的无害异常（EOFError / ConnectionResetError）"""
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.__dict__.get("exc_info")
        if not exc:
            return True
        etype, _, _ = exc
        msg = (record.getMessage() or "").lower()
        is_opening = "opening handshake failed" in msg or "handshake" in msg
        if is_opening and (issubclass(etype, EOFError) or issubclass(etype, ConnectionResetError)):
            return False
        return True


# 基于 websockets.server 这个 logger 挂过滤器
ws_logger = logging.getLogger("websockets.server")
ws_logger.setLevel(logging.INFO)
ws_logger.addFilter(IgnoreHandshakeNoise())

logger = logging.getLogger(__name__)

session_queue = Queue()



async def wait_for_session(connection_id):
    """等待特定连接的session就绪"""
    # 监听队列，直到找到对应的session
    while True:
        conn_id, session, success, base_info = await session_queue.get()
        if conn_id == connection_id:
            return (conn_id, session, success, base_info)
        else:
            # 不是我们要的session，放回队列（或者需要其他处理逻辑）
            await session_queue.put((conn_id, session, success, base_info))


async def pre_operation_background(session_id, base_info):
    """后台执行预操作，不阻塞握手过程"""
    try:
        logger.info(f"[Warmup] Starting background pre-operation for session {session_id}")
        # 执行预操作（可能是耗时的操作）
        session = await create_gsession(base_info)

        # 完成后设置结果
        complete_session_future(session_id, (session, True, base_info))
        logger.info(f"[Warmup] Background pre-operation completed ✅ {session_id}")


    except Exception as e:
        logger.error(f"[Warmup] Pre-operation failed for {session_id}: {e}", exc_info=True)
        complete_session_future(session_id, (None, False, base_info))


def create_base_info(params):
    base_info = BaseInfo()
    try:
        param = params.get("param")
        base_info.voice = param.get("voice")
        base_info.userId = param.get("userId")
        base_info.latitude = param.get("latitude")
        base_info.longitude = param.get("longitude")
        base_info.location = param.get("location")
        base_info.date = param.get("date")
    except:
        logger.error("invalid data type")
    if not base_info.location:
        base_info.location = "Beijing"
    if not base_info.userId:
        base_info.userId = "123456"
    if not base_info.voice:
        base_info.voice = "Aoede"
    if not base_info.date:
        base_info.date = datetime.now()
    logger.info(f"create gemini session data:{base_info.to_json()}")
    return base_info


def get_uri_param(uri):
    # 解析 query 参数
    parsed = urlparse(uri)
    query_params = parse_qs(parsed.query)

    # 将值从 list 转成单值
    params = {k: v[0] for k, v in query_params.items()}

    # 如果包含 param 字段，则进一步 Base64 + JSON 解码
    if "param" in params:
        try:
            decoded_bytes = base64.b64decode(params["param"])
            decoded_str = decoded_bytes.decode("utf-8")
            params["param"] = json.loads(decoded_str)
        except Exception as e:
            print(f"param decode error: {e}")
    return params


async def process_request(conn: ServerConnection, request: Request) -> Response | None:
    """握手前处理健康检查 / 非 WS 请求"""
    upgrade = request.headers.get("Upgrade", "").lower()
    connection_hdr = request.headers.get("Connection", "").lower()

    # 健康检查专用
    if request.path == "/healthy":
        return Response(
            200, "OK",
            Headers([("Content-Type", "text/plain")]),
            b"healthy\n",
        )

    # 非 WS 请求也直接返回 200
    if upgrade != "websocket" or "upgrade" not in connection_hdr:
        return Response(
            200, "OK",
            Headers([("Content-Type", "text/plain")]),
            b"OK\n",
        )
    params = get_uri_param(request.path)
    session_id = str(id(conn))
    base_info = create_base_info(params)
    # 为本连接创建独立 Future
    create_session_future(session_id)

    loop = asyncio.get_running_loop()
    # ✅ 真异步：后台线程执行模型预热
    loop.run_in_executor(None, lambda: asyncio.run(pre_operation_background(session_id, base_info)))
    # ✅ 不等待，立即返回，让握手继续
    return None


async def safe_ws_handler(conn: ServerConnection) -> None:
    """包装 WS 处理逻辑，捕获运行时错误（静默常见断连 + 统计连接）"""
    global total_connections

    peer = conn.remote_address
    active_connections.add(peer)
    total_connections += 1
    logger.info(f"Client connected: {peer}, Active: {len(active_connections)}, Total: {total_connections}")

    # 建立连接
    with tracer.start_as_current_span("websocket.connect") as span:
        client_ip = conn.remote_address[0]
        span.set_attribute("client.ip", client_ip)
        logging.info(f"Client connected: {client_ip}")
        active_connections_metrics.add(1)

    session_id = str(id(conn))
    try:
        await conn.send(json.dumps({"ready": True, "session_id": session_id}))
        logger.info(f"[Handler] Waiting for pre-operation result of session {session_id}")

        fut = get_session_future(session_id)
        if fut is None:
            raise RuntimeError("Session future not found")

        # 最多等待 8 秒（可根据模型初始化时间调整）
        session, success, base_info = await asyncio.wait_for(fut, timeout=5)

        if not success or session is None:
            await conn.send_close(1008, "Gemini initialization failed")
            return

        logger.info(f"[Handler] Session ready for {session_id}")


        async with session as g_session:
            await handle_client(conn, g_session, base_info, session_id)
            logger.info(f"Client disconnected normally: {peer}")
    except ConnectionClosedOK:
    # 正常关闭，不做任何日志输出
        logger.debug(f"WebSocket closed normally: {getattr(conn, 'remote_address', None)}")
    except (EOFError, ConnectionClosedError, InvalidHandshake, WebSocketException):
        logger.info(f"Client disconnected abruptly: {peer}")
    except Exception as e:
        with tracer.start_as_current_span("websocket.error") as span:
            span.record_exception(e)
            span.set_attribute("error", True)
            logging.error(f"Error: {e}")
        logger.exception(f"Unexpected error for {peer}")
    finally:
        active_connections.discard(peer)
        active_connections_metrics.add(-1)
        cancel_session_future(session_id)
        logger.info(f"Active connections after disconnect: {len(active_connections)}")


async def wrapped_handler(conn: ServerConnection) -> None:
    """包住整个连接，防止握手阶段异常冒出（静默处理）"""
    try:
        await safe_ws_handler(conn)
    except (EOFError, ConnectionClosedError, InvalidHandshake, WebSocketException):
        pass
    except Exception:
        logger.debug("Ignored handshake/connection error", exc_info=True)

async def initialize_warmup_background():
    """
      启动时在后台进行 warmup
      """
    try:
        logger.info("[Startup] Scheduling Gemini warmup task...")
        gemini_pool.start_background_tasks()
        asyncio.create_task(gemini_pool.warmup_sessions())  # warmup in background
        logger.info("[Startup] Warmup tasks started ✅")
    except Exception as e:
        logger.error(f"[Startup] Failed to schedule warmup: {e}")

async def main() -> None:
    # await initialize_warmup_background()
    port = 8080
    async with websockets.serve(
        wrapped_handler,
        "0.0.0.0",
        port,
        logger=ws_logger,        # 使用带过滤器的 logger
        open_timeout=2,          # 健康检查半截连接 2 秒就放弃
        close_timeout=2,
        max_size=10 * 1024 * 1024,
        max_queue=2048,
        ping_interval=30,
        ping_timeout=15,
        compression="deflate",
        process_request=process_request,
    ):
        logger.info(f"Running websocket server on 0.0.0.0:{port}...")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        gemini_pool.shutdown(wait=True)
