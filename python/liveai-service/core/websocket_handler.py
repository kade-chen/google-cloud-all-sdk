# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
WebSocket message handling for Gemini Multimodal Live Proxy Server
"""

import asyncio
import base64
import json
import logging
import os
import re
import traceback
import uuid
from typing import Any, Optional

from google.genai import types
from websockets import ConnectionClosedOK, ConnectionClosedError

from core.base_info import BaseInfo
from core.gemini_client import create_gsession
from core.rocketMQ import RocketMQProducer
from core.rocketMQ import create_message_body
from core.session import create_session, remove_session, SessionState
from core.session_manager import session_manager
from core.tool_handler import execute_tool

logger = logging.getLogger(__name__)

# 🆕 新增：定义一个用于协调任务重启的异常
class ReconnectionCompleted(Exception):
    """Raised when a Gemini session has successfully reconnected and tasks need restarting."""
    pass

async def send_error_message(websocket: Any, error_data: dict) -> None:
    """Send formatted error message to client."""
    try:
        await websocket.send(json.dumps({
            "type": "error",
            "data": error_data
        }))
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")

async def cleanup_session(session: Optional[SessionState], session_id: str) -> None:
    """Clean up session resources."""
    try:
        if session:
            # Cancel any running tasks
            if session.current_tool_execution:
                session.current_tool_execution.cancel()
                try:
                    await session.current_tool_execution
                except asyncio.CancelledError:
                    pass

            # Close Gemini session
            # if session.genai_session:
            #     try:
            #         # 检查是否有 close 方法（旧 session）或需要使用 __aexit__（新 session）
            #         if hasattr(session.genai_session, '__aexit__'):
            #             await session.genai_session.__aexit__(None, None, None)
            #         logger.info("Closing genai session")
            #     except Exception as e:
            #         logger.error(f"Error closing Gemini session: {e}")
            # 修正后的关闭逻辑
            if session.genai_session:

                try:
                    # 优先检查是否是异步上下文管理器（_AsyncGeneratorContextManager 属于此类）
                    if hasattr(session.genai_session, '__aexit__'):
                        await session.genai_session.__aexit__(None, None, None)
                    # 再检查是否有 close 方法（兼容旧类型会话）
                    elif hasattr(session.genai_session, 'close'):
                        await session.genai_session.close()
                        logger.info("Closed genai session via close()")
                    else:
                        logger.warning("Gemini session has no known close method")
                except Exception as e:
                    logger.error(f"Error closing Gemini session: {e}")

            if session.RocketMQ:
                try:
                    session.RocketMQ.shutdown()
                except Exception as e:
                    logger.error(f"Error shutdown rocketMQ producer: {e}")
            # Remove session from active sessions
            remove_session(session_id)
            logger.info(f"Session {session_id} cleaned up and ended")
    except Exception as cleanup_error:
        logger.error(f"Error during session cleanup: {cleanup_error}")

async def handle_messages(websocket: Any, session: SessionState) -> None:
    """Handles bidirectional message flow between client and Gemini."""
    logger.info(f"Received message from client {session}")
    client_task = None
    gemini_task = None
    is_reconnecting = False

    try:
        async with asyncio.TaskGroup() as tg:
            # Task 1: Handle incoming messages from client
            client_task = tg.create_task(handle_client_messages(websocket, session))
            # Task 2: Handle responses from Gemini
            gemini_task = tg.create_task(handle_gemini_responses(websocket, session))
    except* Exception as eg:
        handled = False
        for exc in eg.exceptions:
            if isinstance(exc, ReconnectionCompleted):
                logger.info("Reconnection signal detected, propagating for task restart.")
                is_reconnecting = True  # 设置标志
                raise exc  # 重新抛出 ReconnectionCompleted
            if "Quota exceeded" in str(exc):
                logger.info("Quota exceeded error occurred")
                try:
                    # Send error message for UI handling
                    await send_error_message(websocket, {
                        "message": "Quota exceeded.",
                        "action": "Please wait a moment and try again in a few minutes.",
                        "error_type": "quota_exceeded"
                    })
                    # Send text message to show in chat
                    await websocket.send(json.dumps({
                        "type": "text",
                        "data": "⚠️ Quota exceeded. Please wait a moment and try again in a few minutes."
                    }))
                    handled = True
                    break
                except Exception as send_err:
                    logger.error(f"Failed to send quota error message: {send_err}")
            elif "connection closed" in str(exc).lower():
                logger.info("WebSocket connection closed")
                handled = True
                break

        if not handled:
            # For other errors, log and re-raise
            logger.error(f"Error in message handling: {eg}")
            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            raise
    finally:
        # 🆕 关键修改：如果正在重连，则跳过手动取消
        if is_reconnecting:
            logger.info("Skipping task cancellation in finally block for reconnection.")
            return
        # Cancel tasks if they're still running
        if client_task and not client_task.done():
            client_task.cancel()
            try:
                await client_task
            except asyncio.CancelledError:
                pass

        if gemini_task and not gemini_task.done():
            gemini_task.cancel()
            try:
                await gemini_task
            except asyncio.CancelledError:
                pass

# 客户端与 Gemini 服务之间的 “消息中转站”：一边接收客户端的输入（文本、音视频）并转发给 Gemini，一边处理客户端的控制指令（停止、重连）
async def handle_client_messages(websocket: Any, session: SessionState) -> None:
    """Handle incoming messages from the client."""
    running = True
    while running:
        try:
            async for message in websocket:
                data = json.loads(message)
                logger.debug(f"Received message: {data}")
                if "type" in data:
                    msg_type = data["type"]
                    if msg_type == "audio":
                        logger.debug("Client -> Gemini: Sending audio data...")
                    elif msg_type == "image":
                        logger.debug("Client -> Gemini: Sending image data...")
                    else:
                        # Replace audio data with placeholder in debug output
                        debug_data = data.copy()
                        if "data" in debug_data and debug_data["type"] == "audio":
                            debug_data["data"] = "<audio data>"
                        logger.debug(f"Client -> Gemini: {json.dumps(debug_data, indent=2)}")

                # Handle different types of input
                if "type" in data:
                    if not data.get("data"):
                        await websocket.send(json.dumps({"type": "text", "data": "data is null"}))
                        return
                    if data["type"] == "audio":
                        logger.debug("Sending audio to Gemini...")
                        await session.genai_session.send(input={
                            "data": data.get("data"),
                            "mime_type": "audio/pcm"
                        }, end_of_turn=True)
                        logger.debug("Audio sent to Gemini")
                    elif data["type"] == "image":
                        logger.info("Sending image to Gemini...")
                        await session.genai_session.send(input={
                            "data": data.get("data"),
                            "mime_type": "image/jpeg"
                        })
                        logger.info("Image sent to Gemini")
                    elif data["type"] == "text":
                        logger.info("Sending text to Gemini...")
                        await session.genai_session.send(input=data.get("data"), end_of_turn=True)
                        logger.info("Text sent to Gemini")
                    elif data["type"] == "end":
                        logger.info("Received end signal")
                    elif data["type"] == "state":
                        logger.info("Sending state to stop or reconnect genai_session...")
                        text = data.get("data")
                        if text and "stop" == text and session:
                            try:
                                if hasattr(session.genai_session, '__aexit__'):
                                    await session.genai_session.__aexit__(None, None, None)
                                logger.info("genai_session Client disconnected normally")
                                running=False
                                break
                            except Exception as e:
                                logger.error(f"Error closing Gemini session: {e}")
                                running = False
                                break
                        elif text and "reconnect" == text and session:
                            # 客户端请求重连。让 handle_gemini_responses 任务来处理协调重启，
                            # 或者，如果客户端要求，我们强制关闭当前 session，让它退出。
                            logger.info("handle_client_messages Client requested manual reconnect. Forcing session close.")
                            if hasattr(session.genai_session, '__aexit__'):
                                await session.genai_session.__aexit__(None, None, None)  # 强制关闭，触发 ConnectionClosedError，任务将退出
                            break  # 退出循环，等待 TaskGroup 传播异常
                    else:
                        logger.warning(f"Unsupported message type: {data.get('type')}")
        except ConnectionClosedOK as e:
            logger.info(f"handle_client_messages Gemini WebSocket closed normally (1000) :{e}")
            break
        except ConnectionClosedError as e:
            # 🆕 关键修改：Client任务只记录错误并退出，不进行重连，等待 handle_gemini_responses 协调重启
            logger.info(f"handle_client_messages WebSocket closed, deferring reconnection to handler: {e}")
            # 注意：这里我们不做任何事情，ConnectionClosedError 会导致 async for message in websocket 循环结束，
            # 进而导致 handle_client_messages 任务自然退出。
            # 如果退出没有自动发生，可以显式 break:
            break
        except Exception as e:
            if "connection closed" not in str(e).lower():  # Don't log normal connection closes
                logger.error(f"WebSocket connection error: {e}")
                logger.error(f"Full traceback:\n{traceback.format_exc()}")
            raise  # Re-raise to let the parent handle cleanup

async def handle_gemini_responses(websocket: Any, session: SessionState) -> None:
    """Handle responses from Gemini."""
    tool_queue = asyncio.Queue()  # Queue for tool responses

    # Start a background task to process tool calls
    tool_processor = asyncio.create_task(process_tool_queue(tool_queue, websocket, session))

    try:
        while True:
            output_transcriptions = []
            input_transcriptions = []
            async for response in session.genai_session.receive():
                try:
                    # Replace audio data with placeholder in debug output
                    debug_response = str(response)
                    if 'data=' in debug_response and 'mime_type=\'audio/pcm' in debug_response:
                        debug_response = debug_response.split('data=')[0] + 'data=<audio data>' + debug_response.split('mime_type=')[1]
                    logger.debug(f"Received response from Gemini: {debug_response}")

                    # If there's a tool call, add it to the queue and continue
                    if response.tool_call:
                        await tool_queue.put(response.tool_call)
                        continue  # Continue processing other responses while tool executes
                    # Process server content (including audio) immediately
                    if response.session_resumption_update:
                        update = response.session_resumption_update
                        if update.resumable and update.new_handle:
                            logger.info(f"Session resumption update: {update.new_handle}")
                    if response.go_away:
                        go_away = response.go_away
                        # 收到过期信号，开始重连流程
                        logger.info(f"Session goaway: {go_away}")
                        if go_away.time_left:
                            if hasattr(session.genai_session, '__aexit__'):
                                await session.genai_session.__aexit__(None, None, None)  # 关闭旧连接
                            await websocket.send(json.dumps({"type": "state", "data": "start reconnect"})) # 通知客户端
                            new_gemini_session = await create_gsession(session.BaseInfo)
                            await new_gemini_session.__aenter__()  # 手动进入异步上下文
                            session.genai_session = new_gemini_session # 通知客户端
                            logger.info("handle_gemini_responses genai_session Client reconnect normally")
                            await websocket.send(
                                json.dumps({"reconnect": True, "data": "reconnected successfully"}))
                            # 🆕 关键修改：重连成功后，抛出异常以强制重启 handle_messages 任务
                            raise ReconnectionCompleted("Gemini session reconnected. Triggering task restart.")
                    await process_server_content(websocket, session, response.server_content, input_transcriptions, output_transcriptions)
                except ConnectionClosedOK as e:
                    logger.info(f"handle_gemini_responses WebSocket closed normally during Gemini response handling: {e}")
                except ConnectionClosedError as e:
                    logger.info(f"handle_gemini_responses WebSocket closed unexpectedly (no close frame): {e}")
                except ReconnectionCompleted:
                    raise ReconnectionCompleted("Gemini session reconnected. Triggering task restart.")
                except Exception as e:
                    logger.error(f"Error handling Gemini response: {e}")
                    logger.error(f"Full traceback:\n{traceback.format_exc()}")
    except ConnectionClosedOK as e:
        logger.info(f"handle_gemini_responses Gemini WebSocket closed normally (1000): {e}")
    except ConnectionClosedError as e:
        logger.info(f"handle_gemini_responses WebSocket closed unexpectedly (no close frame): {e}")
        new_gemini_session = await create_gsession(session.BaseInfo)
        await new_gemini_session.__aenter__()  # 手动进入异步上下文
        session.genai_session = new_gemini_session
        logger.info("handle_gemini_responses genai_session Client reconnect normally")
        await websocket.send(json.dumps({"reconnect": True, "data": "reconnected successfully"}))
        # 🆕 关键修改：重连成功后，抛出异常以强制重启 handle_messages 任务
        raise ReconnectionCompleted("Gemini session reconnected. Triggering task restart.")
    except ReconnectionCompleted:
        raise ReconnectionCompleted("Gemini session reconnected. Triggering task restart.")
    finally:
        # Cancel and clean up tool processor
        if tool_processor and not tool_processor.done():
            tool_processor.cancel()
            try:
                await tool_processor
            except asyncio.CancelledError:
                pass

        # Clear any remaining items in the queue
        while not tool_queue.empty():
            try:
                tool_queue.get_nowait()
                tool_queue.task_done()
            except asyncio.QueueEmpty:
                break

async def process_tool_queue(queue: asyncio.Queue, websocket: Any, session: SessionState):
    """Process tool calls from the queue."""
    while True:
        tool_call = await queue.get()
        try:
            function_responses = []
            for function_call in tool_call.function_calls:
                # Store the tool execution in session state
                session.current_tool_execution = asyncio.current_task()

                # Send function call to client (for UI feedback)
                await websocket.send(json.dumps({
                    "type": "function_call",
                    "data": {
                        "name": function_call.name,
                        "args": function_call.args
                    }
                }))

                tool_result = await execute_tool(function_call.name, function_call.args)

                # Send function response to client
                await websocket.send(json.dumps({
                    "type": "function_response",
                    "data": tool_result
                }))

                function_responses.append(
                    types.FunctionResponse(
                        name=function_call.name,
                        id=function_call.id,
                        response={ "result":"ok" }
                    )
                )

                session.current_tool_execution = None

            if function_responses and function_call.name != 'startLiveVideoChat':
                tool_response = types.LiveClientToolResponse(
                    function_responses=function_responses
                )
                await session.genai_session.send(input=tool_response)
        except Exception as e:
            logger.error(f"Error processing tool call: {e}")
        finally:
            queue.task_done()


def detect_language_ratio(text: str) -> tuple[float, float]:
    """检测中文与英文字符比例"""
    total = len(text)
    if total == 0:
        return 0.0, 0.0
    chinese_count = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_count = len(re.findall(r'[A-Za-z]', text))
    return chinese_count / total, english_count / total


def smart_clean_spaces(text: str) -> str:
    """
    ✅ 保留英文句子及其数字空格 (e.g. "It is 10:32 AM")
    ✅ 中文主导 → 清理所有中文相关空格
    ✅ 混合场景 → 精确处理（中英、数字-中文）
    """
    if not text.strip():
        return text

    ch_ratio, en_ratio = detect_language_ratio(text)

    # 1️⃣ 纯英文或英文为主 → 完全保留
    if en_ratio >= 0.6 and ch_ratio < 0.2:
        return text

    # 2️⃣ 合并多空格为一个
    result = re.sub(r'\s+', ' ', text)

    # 3️⃣ 删除中文之间的空格
    result = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', result)

    # 4️⃣ 删除数字与中文之间的空格（但不影响英文数字）
    result = re.sub(r'(?<=[0-9])\s+(?=[\u4e00-\u9fff])', '', result)
    result = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[0-9])', '', result)

    # 5️⃣ 中文主导 → 去掉中英文间空格
    if ch_ratio >= 0.6:
        result = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z])', '', result)
        result = re.sub(r'(?<=[A-Za-z])\s+(?=[\u4e00-\u9fff])', '', result)
    else:
        # 混合模式 → 保留中英文间一个空格
        result = re.sub(r'(?<=[\u4e00-\u9fff])\s*(?=[A-Za-z])', ' ', result)
        result = re.sub(r'(?<=[A-Za-z])\s*(?=[\u4e00-\u9fff])', ' ', result)

    # 6️⃣ 清除中文标点前后空格
    result = re.sub(r'\s*([，。！？、；：])\s*', r'\1', result)

    return result.strip()

def clean_unbalanced_or_extra_quotes(data: str) -> str:
    """
    清理字符串中不成对、多余或转义的英文双引号。
    - 删除所有英文双引号（包括转义形式 \"）
    - 保留中文引号（“”）
    - 自动修复不成对或多余的情况
    """
    if not isinstance(data, str):
        return data

    # 1️⃣ 去掉转义双引号（\"）
    cleaned = data.replace('\\"', '')

    # 2️⃣ 去掉英文双引号（但保留中文 “ 和 ”）
    cleaned = re.sub(r'"', '', cleaned)

    # 3️⃣ 去除多余空格（双引号删除后可能留下空格）
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()

    return cleaned


async def process_server_content(websocket: Any, session: SessionState, server_content: Any, input_transcriptions,
                                 output_transcriptions):
    """Process server content including audio and text."""
    # Check for interruption first
    if hasattr(server_content, 'interrupted') and server_content.interrupted:
        logger.info("Interruption detected from Gemini")
        await websocket.send(json.dumps({
            "type": "interrupted",
            "data": {
                "message": "Response interrupted by user input"
            }
        }))
        session.is_receiving_response = False
        return

    if hasattr(server_content, "model_turn") and server_content.model_turn:
        session.received_model_response = True
        session.is_receiving_response = True
        for part in server_content.model_turn.parts:
            if part.inline_data:
                audio_base64 = base64.b64encode(part.inline_data.data).decode('utf-8')
                clear_audio_quotes = clean_unbalanced_or_extra_quotes(audio_base64)
                await websocket.send(json.dumps({
                    "type": "audio",
                    "data": clear_audio_quotes
                }))
            elif part.text:
                clear_text = smart_clean_spaces(part.text)
                clear_text_quotes = clean_unbalanced_or_extra_quotes(clear_text)
                await websocket.send(json.dumps({
                    "type": "text",
                    "data": clear_text_quotes
                }))

    if hasattr(server_content, "turn_complete") and server_content.turn_complete:
        await websocket.send(json.dumps({
            "type": "turn_complete"
        }))
        session.received_model_response = False
        session.is_receiving_response = False
        input_str = ''.join(input_transcriptions)
        logger.info(f"Input transcription: {input_str}")
        if input_str:
            if session.BaseInfo.userId not in (None, ""):
                userid = session.BaseInfo.userId
            else:
                userid = 123456
            message_data = create_message_body(str(uuid.uuid4()), userid, "text", input_str, "user")
            logger.info(f"同步发送Input transcription信息：{json.dumps(message_data)}")
            result = session.RocketMQ.send_sync(message_body=message_data, properties={'send': 'sync'},
                                                keys="send_sync")
            logger.info(f"同步发送Input transcription结果：{'成功' if result else '失败'}")
        output_str = ''.join(output_transcriptions)
        logger.info(f"Output transcription: {output_str}")
        if output_str:
            if session.BaseInfo.userId not in (None, ""):
                userid = session.BaseInfo.userId
            else:
                userid = 123456
            message_data = create_message_body(str(uuid.uuid4()), userid, "text", output_str, "assistant")
            logger.info(f"同步发送Output transcription信息：{json.dumps(message_data)}")
            result = session.RocketMQ.send_sync(message_body=message_data, properties={'send':'sync'}, keys="send_sync")
            logger.info(f"同步发送Output transcription结果：{'成功' if result else '失败'}")

    if hasattr(server_content, "input_transcription") and server_content.input_transcription:
        if server_content.input_transcription.text:
            logger.info(f"Input transcription: {server_content.input_transcription.text}")
            clear_text = smart_clean_spaces(server_content.input_transcription.text)
            input_transcriptions.append(clear_text)
        else:
            input_transcriptions.append("")

    if hasattr(server_content, "output_transcription") and server_content.output_transcription:
        if server_content.output_transcription.text:
            logger.info(f"Output transcription: {server_content.output_transcription.text}")
            clear_text = smart_clean_spaces(server_content.output_transcription.text)
            await websocket.send(json.dumps({
                "type": "text",
                "data": clear_text
            }))
            output_transcriptions.append(clear_text)
        else:
            output_transcriptions.append("")
    return None


def create_rocketmq_producer() -> RocketMQProducer:
    # 创建生产者
    producer = RocketMQProducer(
        name_server=os.getenv('NAME_SERVER', 'rmq-cn-to33z1b6t1h.cn-beijing.rmq.aliyuncs.com:8080'),
        access_key=os.getenv('ACCESS_KEY', 'QTI62XJ5K39785B5'),
        secret_key=os.getenv('SECRET_KEY', 'JT7C9w988q2VjE9B'),
        instance_id=os.getenv('INSTANCE_ID', 'rmq-cn-to33z1b6t1h'),
        group_name=os.getenv('GROUP', 'google_chatlog_group'),
        topic=os.getenv('TOPIC', 'google_chatlog')
    )
    return producer


async def handle_client(websocket: Any, gemini_session: Any, base_info: BaseInfo, session_id: str) -> None:
    reconnecting = False  # ✅ 用于标记是否重连中
    try:
        #await robustness_middleware(websocket)
        # create and initialize RocketMQ producer

        """Handles a new client connection."""
        session = create_session(session_id)
        await session_manager.add(websocket, session_id, session)

        session.genai_session = gemini_session
        session.BaseInfo = base_info


        producer = create_rocketmq_producer()
        session.RocketMQ = producer
        while True:
            try:
                # Start message handling
                await handle_messages(websocket, session)
                # 如果 handle_messages 正常退出，则退出主循环
                break
            except ReconnectionCompleted as e:
                reconnecting = True
                logger.info(f"🎯 [DEBUG] Caught ReconnectionCompleted in handle_client: {e}")
                logger.info(f"🔄 [DEBUG] reconnecting flag set to: {reconnecting}")
                logger.info(f"📊 [DEBUG] Current session object: {id(session.genai_session)}")
                # 遇到重连完成信号，不进行 cleanup，而是继续外层 while 循环，重新执行 handle_messages
                continue
            except Exception as e:
                reconnecting = False
                if "code = 1006" in str(e) or "connection closed abnormally" in str(e).lower():
                    logger.info(f"Browser disconnected or refreshed for session {session_id}")
                    await send_error_message(websocket, {
                        "message": "Connection closed unexpectedly",
                        "action": "Reconnecting...",
                        "error_type": "connection_closed"
                    })
                else:
                    raise
                break
    except ConnectionClosedOK:
        logger.info("Gemini WebSocket closed normally (1000)")
    except ConnectionClosedError as e:
        logger.info(f"WebSocket closed unexpectedly (no close frame): {e}")
    except asyncio.TimeoutError:
        logger.info(f"Session {session_id} timed out - this is normal for long idle periods")
        await send_error_message(websocket, {
            "message": "Session timed out due to inactivity.",
            "action": "You can start a new conversation.",
            "error_type": "timeout"
        })
    except Exception as e:
        reconnecting = False
        logger.error(f"Error in handle_client: {e}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")

        if "connection closed" in str(e).lower() or "websocket" in str(e).lower():
            logger.info(f"WebSocket connection closed for session {session_id}")
            # No need to send error message as connection is already closed
        else:
            await send_error_message(websocket, {
                "message": "An unexpected error occurred.",
                "action": "Please try again.",
                "error_type": "general"
            })
    finally:
        # ✅ 仅非重连时才清理
        if reconnecting:
            logger.info(f"🔄 [DEBUG] Finally block - reconnecting={reconnecting}, skipping cleanup")
        else:
            logger.info(f"🧹 [DEBUG] Finally block - reconnecting={reconnecting}, performing cleanup")
            # Always ensure cleanup happens
            await cleanup_session(session, session_id)
            await session_manager.remove(websocket, session_id)

            # ✅ 强制关闭 WebSocket
            try:
                await websocket.close()
                logger.info(f"WebSocket connection closed for session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to close websocket: {e}")