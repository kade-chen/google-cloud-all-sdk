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
Gemini client initialization and connection management
"""
import logging
import os
from datetime import datetime

from google import genai
from google.oauth2 import service_account

from config.config import MODEL, CONFIG, ConfigurationError, api_config, SYSTEM_INSTRUCTION_TEMPLATE
from core.base_info import BaseInfo

logger = logging.getLogger(__name__)

async def create_gsession(base_info: BaseInfo):
    """Create and initialize the Gemini client and session"""
    try:
        # Initialize authentication
        # await api_config.initialize()

        if api_config.use_vertex:
            # Vertex AI configuration
            location = os.getenv('VERTEX_LOCATION', 'us-central1')
            # project_id = os.environ.get('PROJECT_ID')
            project_id = "wdtest-001"

            if not project_id:
                raise ConfigurationError("PROJECT_ID is required for Vertex AI")

            logger.info(f"Initializing Vertex AI client with location: {location}, project: {project_id}")

            service_account_key_path = "secrets/wdtest-001-f7a598d7d5e2.json"
            credentials = service_account.Credentials.from_service_account_file(
                service_account_key_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )

            # Initialize Vertex AI client
            client = genai.Client(
                vertexai=True,
                location=location,
                project=project_id,
                credentials=credentials
                # http_options={'api_version': 'v1beta'}
            )
            logger.info(f"Vertex AI client initialized with client: {client}")
        else:
            # Development endpoint configuration
            logger.info("Initializing development endpoint client")

            # Initialize development client
            client = genai.Client(
                vertexai=False,
                http_options={'api_version': 'v1alpha'},
                api_key=api_config.api_key
            )

        CONFIG["generation_config"]["speech_config"]["voice_config"]["prebuilt_voice_config"][
                "voice_name"] = base_info.voice
        CONFIG["system_instruction"] = SYSTEM_INSTRUCTION_TEMPLATE.format(Time=base_info.date, Location=base_info.location)
        logger.debug(f"System instruction: {CONFIG['system_instruction']}")
        # Create the session
        session = client.aio.live.connect(
            model=MODEL,
            config=CONFIG
        )
        return session

    except ConfigurationError as e:
        logger.error(f"Configuration error while creating Gemini session: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while creating Gemini session: {str(e)}")
        raise