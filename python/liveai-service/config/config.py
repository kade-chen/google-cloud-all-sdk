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
Configuration for Vertex AI Gemini Multimodal Live Proxy Server
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from google.cloud import secretmanager
from google.genai.types import StartSensitivity, EndSensitivity, SessionResumptionConfig

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

class ConfigurationError(Exception):
    """Custom exception for configuration errors."""
    pass

def get_secret(secret_id: str) -> str:
    """Get secrets from Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    project_id = os.environ.get('PROJECT_ID')
    
    if not project_id:
        raise ConfigurationError("PROJECT_ID environment variable is not set")
    
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    
    try:
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        raise


class ApiConfig:
    """API configuration handler."""
    
    def __init__(self):
        # Determine if using Vertex AI
        self.use_vertex = os.getenv('VERTEX_API', 'false').lower() == 'true'
        
        self.api_key: Optional[str] = None
        
        logger.info(f"Initialized API configuration with Vertex AI: {self.use_vertex}")
    
    async def initialize(self):
        """Initialize API credentials."""
        try:
            # Always try to get OpenWeather API key regardless of endpoint
            self.weather_api_key = get_secret('OPENWEATHER_API_KEY')
        except Exception as e:
            logger.warning(f"Failed to get OpenWeather API key from Secret Manager: {e}")
            self.weather_api_key = os.getenv('OPENWEATHER_API_KEY')
            if not self.weather_api_key:
                raise ConfigurationError("OpenWeather API key not available")

        if not self.use_vertex:
            try:
                self.api_key = get_secret('GOOGLE_API_KEY')
            except Exception as e:
                logger.warning(f"Failed to get API key from Secret Manager: {e}")
                self.api_key = os.getenv('GOOGLE_API_KEY')
                if not self.api_key:
                    raise ConfigurationError("No API key available from Secret Manager or environment")

# Initialize API configuration
api_config = ApiConfig()

# Model configuration
if api_config.use_vertex:
    MODEL = os.getenv('MODEL_VERTEX_API', 'gemini-2.0-flash-exp')
    VOICE = os.getenv('VOICE_VERTEX_API', 'Aoede')
else:
    MODEL = os.getenv('MODEL_DEV_API', 'models/gemini-2.0-flash-exp')
    VOICE = os.getenv('VOICE_DEV_API', 'Puck')

# Cloud Function URLs with validation
CLOUD_FUNCTIONS = {
    "get_weather": os.getenv('WEATHER_FUNCTION_URL'),
    # "get_weather_forecast": os.getenv('FORECAST_FUNCTION_URL'),
    # "get_next_appointment": os.getenv('CALENDAR_FUNCTION_URL'),
    # "get_past_appointments": os.getenv('PAST_APPOINTMENTS_FUNCTION_URL'),
}

product = os.getenv("PRODUCT")
one_observation = os.getenv('ONE_OBSERVATION')
api_key = os.getenv('OPENWEATHER_API_KEY')

# Validate Cloud Function URLs
for name, url in CLOUD_FUNCTIONS.items():
    if not url:
        logger.warning(f"Missing URL for cloud function: {name}")
    elif not url.startswith('https://'):
        logger.warning(f"Invalid URL format for {name}: {url}")

# Load system instructions
try:
    with open('config/system-instructions.txt', 'r', encoding="utf-8") as f:
        SYSTEM_INSTRUCTIONS = f.read()
except Exception as e:
    logger.error(f"Failed to load system instructions: {e}")
    SYSTEM_INSTRUCTIONS = ""

# Gemini Configuration
CONFIG = {
    "generation_config": {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config" :{
                "prebuilt_voice_config":{
                    "voice_name":VOICE
                }
            },
            #"language_code" : "cmn-CN"
            "language_code" : "en-US"
        }

    },
    "session_resumption":{
            "transparent":True,
            "handle": None
        },
    "realtime_input_config": {
        "automatic_activity_detection": {
            "disabled": False,
            "start_of_speech_sensitivity":StartSensitivity.START_SENSITIVITY_LOW,
            "end_of_speech_sensitivity":EndSensitivity.END_SENSITIVITY_HIGH,
            "prefix_padding_ms":0,
            "silence_duration_ms":1000,
        }
    },
    "tools": [
        # {"function_declarations": [
        #     # {
        #     #     "name": "get_weather",
        #     #     "description": "Retrieves weather information for a specific location or the system's default location. This function should be invoked when: 1. The user explicitly asks about the weather (e.g., 'What's the weather like?', 'How is the weather today?'). 2. The user mentions feelings, conditions, or predictions related to weather (e.g., 'It's so hot today!', 'I feel the temperature.', 'Is it raining?'). 3. The user specifies a particular city or location (e.g., 'What's the weather like in London?', 'Check the weather in Tokyo tomorrow?'). If a specific location is provided, use it. Otherwise, use the system's default location. Supports multilingual requests and attempts to respond in the language used by the user.",
        #     #     "parameters": {
        #     #         "type": "object",
        #     #         "properties": {
        #     #             "city": {
        #     #                 "type": "string",
        #     #                 "description": "The city or location to get weather for. This parameter should be populated if the user specifies a city or location. If no city is specified and the user is still asking about the weather, this parameter can be omitted or set to null to indicate using the system's default location."
        #     #             }
        #     #         },
        #     #         "required": []
        #     #     }
        #     # },
        #     {
        #         "name": "pauseOrResumeChat",
        #         "description": "Temporarily pauses or resumes an ongoing chat session (video or voice) when the user clearly requests a short break. "
        #                 "This function is for *temporary interruptions only*, not for ending the chat. "
        #                 "Use when the user says things like '暂停聊天'(pause chat), '先停一下'(stop for now), '稍后再继续'(resume later), "
        #                 "'continue conversation', or 'resume chat'. "
        #                 "The 'action' parameter must specify either 'pause' (to temporarily halt) or 'resume' (to continue).",
        #         "parameters": {
        #           "type": "object",
        #           "properties": {
        #             "action": {
        #               "type": "string",
        #               "description": "Indicates whether to 'pause' or 'resume' the chat session.",
        #               "enum": [
        #                 "pause",
        #                 "resume"
        #               ]
        #             }
        #           },
        #           "required": [
        #             "action"
        #           ],
        #           "propertyOrdering": [
        #             "action"
        #           ]
        #         }
        #       },
        #       {
        #         "name": "startLiveVideoChat",
        #         "description": "Starts a live video chat by turning on the camera when the user explicitly requests it. "
        #                 "Trigger only for direct instructions such as '开始视频聊天'(start video chat), '打开摄像头'(turn on camera), "
        #                 "'initiate video call', or 'begin live video'. Avoid vague or implied requests.",
        #         "parameters":{
        #             "type": "object",
        #             "properties": {}
        #         }
        #       },
        #       {
        #         "name": "switchToSpeechChat",
        #         "description": "Switches from video chat back to voice-only mode by turning off the camera. "
        #                 "Call when the user explicitly says things like '关闭摄像头'(turn off camera), '切换到语音聊天'(switch to voice chat), "
        #                 "'go back to voice mode', or 'end video feed'. This is for stopping video while continuing the conversation.",
        #         "parameters":{
        #             "type": "object",
        #             "properties": {}
        #         }
        #       },
        #       {
        #         "name": "endChat",
        #         "description": "Completely terminates the current chat session (voice or video) when the user clearly indicates they are finished. "
        #             "This is for *permanent endings*, not temporary pauses. "
        #             "Typical phrases include '结束/停止/终止聊天'(end chat), '再见'(goodbye), '下次见'(see you next time), "
        #             "'close session', 'quit Rayneo AI', 'hang up', or 'finish chat'."
        #             "暂时中断/暂时停止聊天/end the chat for now请使用 pauseOrResumeChat。",
        #         "parameters":{
        #             "type": "object",
        #             "properties": {}
        #         }
        #       },
        #       {
        #         "name": "pay",
        #         "description": "Handles payment or purchase actions when the user explicitly requests to buy, pay, send money, or confirm a transaction. "
        #                 "Covers commands like '买单'(pay the bill), '结账'(checkout), '确认交易'(confirm payment), 'purchase item', "
        #                 "or 'send money'. Supports multilingual payment instructions, including scanning QR codes or barcodes if needed.",
        #         "parameters":{
        #             "type": "object",
        #             "properties": {}
        #         }
        #       },
        #       # {
        #       #   "name": "getCurrentTime",
        #       #   "description": "Responds to user requests to know the current time. This function retrieves the local time for the user's current geographical location (inferred from latitude and longitude) and informs the user. Supports multilingual requests and responses. Invoke when the user asks 'What time is it?', 'Tell me the current time', 'What's the time in [implied location]?', or similar time-related queries.",
        #       # }
        # ]},
         {"google_search":{}}
    ],
    "system_instruction": SYSTEM_INSTRUCTIONS,
    "input_audio_transcription": {},
    "output_audio_transcription": {},
    "context_window_compression":{
        "trigger_tokens": 12800,
        "sliding_window": {
            "target_tokens": 10240
        }
    }
}

# --- 新增：保存 system_instruction 模板 ---
SYSTEM_INSTRUCTION_TEMPLATE = CONFIG["system_instruction"]