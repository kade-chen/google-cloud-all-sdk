# liveai-service 

一个python开发的服务端，接收websocket的请求通信，实现全双工链路的语音交互，并实现技能处理。

启动文件： server.py

服务启动入口：__main__ 方法

端口：8080

python 的版本为3.13

所有依赖都由requirements.txt进行管理




这段代码是一个基于WebSocket的Gemini多模态实时代理服务器的核心逻辑，负责客户端与Gemini服务之间的消息交互、会话管理、重连机制及资源清理。以下是各个方法的具体作用：


### **1. 异常类：`ReconnectionCompleted`**  
```python
class ReconnectionCompleted(Exception):
    """当Gemini会话重连成功且需要重启任务时触发"""
```  
- 自定义异常，用于在会话重连成功后，通知上层逻辑（如`handle_messages`）重启消息处理任务，确保新会话能正常收发消息。  


### **2. 工具方法：`send_error_message`**  
```python
async def send_error_message(websocket: Any, error_data: dict) -> None:
    """向客户端发送格式化的错误消息"""
```  
- 封装错误消息格式（`{"type": "error", "data": ...}`），通过WebSocket发送给客户端，用于UI展示错误提示（如配额超限、连接中断等）。  


### **3. 资源清理：`cleanup_session`**  
```python
async def cleanup_session(session: Optional[SessionState], session_id: str) -> None:
    """清理会话相关资源（任务、Gemini会话、RocketMQ生产者等）"""
```  
- **核心操作**：  
  - 取消当前运行的工具执行任务（`session.current_tool_execution`）；  
  - 关闭Gemini会话（优先通过异步上下文管理器`__aexit__`，兼容旧版本的`close`方法）；  
  - 关闭RocketMQ生产者（`session.RocketMQ.shutdown()`）；  
  - 从活跃会话列表中移除当前会话，释放资源。  


### **4. 消息协调：`handle_messages`**  
```python
async def handle_messages(websocket: Any, session: SessionState) -> None:
    """协调客户端与Gemini之间的双向消息流（启动并管理两个核心任务）"""
```  
- **核心操作**：  
  - 通过`asyncio.TaskGroup`启动两个并行任务：  
    - `client_task`：调用`handle_client_messages`处理客户端发来的消息；  
    - `gemini_task`：调用`handle_gemini_responses`处理Gemini返回的响应；  
  - 捕获并处理任务中的异常（如`ReconnectionCompleted`重连信号、配额超限、连接关闭等）；  
  - 重连时跳过任务取消（`is_reconnecting`标志），非重连时则清理未完成的任务。  


### **5. 客户端消息处理：`handle_client_messages`**  
```python
async def handle_client_messages(websocket: Any, session: SessionState) -> None:
    """接收客户端消息（文本、音频、图像等），转发给Gemini，并处理控制指令"""
```  
- **核心操作**：  
  - 持续监听客户端WebSocket消息（`async for message in websocket`）；  
  - 根据消息类型（`text`/`audio`/`image`）格式化数据，转发给Gemini服务（`session.genai_session.send(...)`）；  
  - 处理客户端控制指令（`type: "state"`）：  
    - `stop`：关闭当前Gemini会话，终止消息循环；  
    - `reconnect`：强制关闭当前会话，触发重连逻辑；  
  - 捕获连接关闭异常（正常关闭`ConnectionClosedOK`、异常关闭`ConnectionClosedError`），确保优雅退出。  


### **6. Gemini响应处理：`handle_gemini_responses`**  
```python
async def handle_gemini_responses(websocket: Any, session: SessionState) -> None:
    """接收Gemini的响应（文本、音频、工具调用等），转发给客户端，并处理重连逻辑"""
```  
- **核心操作**：  
  - 启动后台任务`tool_processor`（`process_tool_queue`）处理Gemini的工具调用指令；  
  - 持续监听Gemini的响应（`async for response in session.genai_session.receive()`）：  
    - 若响应包含工具调用（`response.tool_call`），放入`tool_queue`队列等待处理；  
    - 若收到会话恢复标识（`response.session_resumption_update`），记录恢复句柄（用于重连时继承上下文）；  
    - 若收到会话过期信号（`response.go_away`），触发重连流程：关闭旧会话→创建新会话→通知客户端→抛出`ReconnectionCompleted`重启任务；  
  - 调用`process_server_content`处理Gemini的实质内容（文本、音频），转发给客户端；  
  - 异常关闭时自动重连，并重启消息处理任务。  


### **7. 工具调用处理：`process_tool_queue`**  
```python
async def process_tool_queue(queue: asyncio.Queue, websocket: Any, session: SessionState):
    """异步处理Gemini的工具调用指令（从队列中取任务，执行工具并返回结果）"""
```  
- **核心操作**：  
  - 从`tool_queue`队列中获取Gemini的工具调用指令（`tool_call`）；  
  - 向客户端发送工具调用通知（`{"type": "function_call", ...}`），用于UI反馈；  
  - 调用`execute_tool`执行具体工具（如外部API调用），获取结果；  
  - 将工具结果返回给客户端（`{"type": "function_response", ...}`），并同步给Gemini服务。  


### **8. 文本处理辅助函数**  
- **`detect_language_ratio`**：检测文本中中文与英文的占比（用于后续空格清理逻辑）。  
- **`smart_clean_spaces`**：根据语言占比智能清理空格（中文主导时去除多余空格，英文主导时保留格式）。  
- **`clean_unbalanced_or_extra_quotes`**：清理文本中多余或不成对的英文引号（保留中文引号），避免格式错误。  


### **9. 内容转发与日志同步：`process_server_content`**  
```python
async def process_server_content(websocket: Any, session: SessionState, server_content: Any, ...):
    """处理Gemini返回的内容（文本、音频），转发给客户端，并同步日志到RocketMQ"""
```  
- **核心操作**：  
  - 处理Gemini的文本/音频响应：清洗文本（空格、引号）、编码音频（Base64），通过WebSocket转发给客户端；  
  - 会话轮次完成时（`server_content.turn_complete`），将用户输入（`input_transcriptions`）和Gemini输出（`output_transcriptions`）通过RocketMQ生产者同步到消息队列（用于日志存储或后续分析）。  


### **10. RocketMQ生产者创建：`create_rocketmq_producer`**  
```python
def create_rocketmq_producer() -> RocketMQProducer:
    """创建RocketMQ生产者实例（用于发送会话日志到消息队列）"""
```  
- 初始化RocketMQ生产者，配置连接参数（名称服务器、密钥、实例ID等），绑定指定的主题（`google_chatlog`），用于后续发送用户输入和Gemini输出日志。  


### **11. 客户端连接入口：`handle_client`**  
```python
async def handle_client(websocket: Any, gemini_session: Any, base_info: BaseInfo, session_id: str) -> None:
    """客户端连接的入口函数，初始化会话、管理生命周期、处理重连与异常"""
```  
- **核心操作**：  
  - 初始化会话（`create_session`），添加到会话管理器（`session_manager.add`）；  
  - 绑定Gemini会话（`session.genai_session`）和基础信息（`session.BaseInfo`），创建RocketMQ生产者；  
  - 循环调用`handle_messages`处理消息，重连时通过`ReconnectionCompleted`异常继续循环（不清理资源）；  
  - 捕获连接关闭、超时等异常，发送对应错误提示；  
  - 最终通过`finally`块清理资源（非重连时）：关闭会话、移除连接、关闭WebSocket。  


### **总结**  
整个代码通过多个方法的协作，实现了“客户端↔服务端↔Gemini”的实时双向通信：  
- 消息处理层（`handle_client_messages`/`handle_gemini_responses`）负责数据转发；  
- 会话管理层（`handle_client`/`cleanup_session`）负责生命周期与资源清理；  
- 重连机制（`ReconnectionCompleted`/`go_away`处理）确保会话中断后无缝恢复；  
- 辅助工具（文本处理、RocketMQ）负责数据格式化与日志同步。  

各方法分工明确，共同保障了多模态（文本、音频、图像）实时交互的稳定性。


