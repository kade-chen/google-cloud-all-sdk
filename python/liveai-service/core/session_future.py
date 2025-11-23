import asyncio
import logging

logger = logging.getLogger(__name__)

# 每个连接 session_id → asyncio.Future
session_futures: dict[str, asyncio.Future] = {}

def create_session_future(session_id: str):
    """为每个新连接创建独立 Future"""
    fut = asyncio.get_event_loop().create_future()
    session_futures[session_id] = fut

def get_session_future(session_id: str) -> asyncio.Future | None:
    return session_futures.get(session_id)

def complete_session_future(session_id: str, result: tuple):
    """后台任务完成时设置结果"""
    fut = session_futures.get(session_id)
    if fut and not fut.done():
        fut.set_result(result)
        logger.info(f"[Warmup] Session future resolved: {session_id}")
    session_futures.pop(session_id, None)

def cancel_session_future(session_id: str):
    """清理时取消 Future"""
    fut = session_futures.pop(session_id, None)
    if fut and not fut.done():
        fut.cancel()
