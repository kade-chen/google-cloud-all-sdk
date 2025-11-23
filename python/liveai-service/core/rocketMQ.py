import json
import logging
import time

from rocketmq.v5.client import Credentials, ClientConfiguration
from rocketmq.v5.model import Message
from rocketmq.v5.producer import Producer

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RocketMQProducer")


class RocketMQProducer:
    def __init__(self, name_server, access_key, secret_key, instance_id,
                 group_name="GID_DEFAULT_PRODUCER", topic="DEFAULT_TOPIC"):
        """
        :param name_server:
        :param access_key:
        :param secret_key:
        :param instance_id:
        :param group_name:
        :param topic:
        """
        self.name_server = name_server
        self.group_name = group_name
        self.topic = topic
        self.producer = None
        self.initialized = False
        self.access_key = access_key
        self.secret_key = secret_key
        self.instance_id = instance_id  # 企业版实例ID

    def initialize(self):
        try:
            credentials = Credentials(self.access_key, self.secret_key)
            config = ClientConfiguration(self.name_server, credentials, self.instance_id, 30)
            self.producer = Producer(config)
            # 启动生产者
            self.producer.startup()
            self.initialized = True
            logger.info(f"生产者初始化成功 | 组名: {self.group_name} | 主题: {self.topic}")

        except Exception as e:
            logger.exception(f"初始化失败：{e}")
            self.initialized = False

    def send_sync(self, message_body, properties, keys, tags="*", topic=None):
        """同步发送消息"""
        if not self.initialized:
            self.initialize()
            if not self.initialized:
                return False

        target_topic = topic or self.topic

        try:
            if not target_topic:
                logger.error("发送失败：未指定消息主题")
                return False
            msg = self.create_message(message_body, target_topic, tags, keys, properties)
            print(f"待发送的消息为：{msg}")
            res = self.producer.send(msg)
            logger.info(f"消息发送成功 | 响应结果: {res}")
            return True
        except Exception as e:
            logger.exception(f"同步发送失败：{e}")
            return False



    def create_message(self, message_body, topic, tags, keys, properties):
        """创建消息对象"""
        if isinstance(message_body, dict):
            message_body = json.dumps(message_body).encode("utf-8")
        elif not isinstance(message_body, str):
            message_body = str(message_body).encode("utf-8")
        else:
            message_body = message_body

        msg = Message()
        msg.topic = topic
        msg.body = message_body

        if tags:
            msg.tag = tags

        if keys:
            if isinstance(keys, list):
                keys = ",".join(keys)
            msg.keys = keys

        if properties:
            for key, value in properties.items():
                msg.add_property(key, str(value))

        return msg

    def shutdown(self):
        """安全关闭生产者"""
        if self.producer and self.initialized:
            try:
                self.producer.shutdown()
                self.initialized = False
                logger.info("生产者已安全关闭")
            except Exception as e:
                logger.exception(f"关闭生产者失败：{e}")

def create_message_body(message_id, user_id, message_type, content, role):
    message_data = {
            "messageId": message_id,
            "userId": int(user_id),
            "role": role,
            "messageType": message_type,
            "content": content,
            "createdTime": int(round(time.time() * 1000)),
            "assistantId": "99999999",
            "cmType": "text_chat"
    }
    return message_data