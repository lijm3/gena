"""
LLM 客户端 - 统一的 LLM API 调用
"""
import json
import requests
from typing import Optional, List, Dict, Any

from config.settings import LLMConfig
from utils.encoding_utils import safe_subprocess_run


class LLMClient:
    """LLM 客户端类"""
    
    def __init__(self):
        """初始化 LLM 客户端"""
        self.base_url = LLMConfig.get_base_url()
        self.auth_token = LLMConfig.get_auth_token()
        self.model = LLMConfig.get_model()
        self.max_tokens = LLMConfig.get_max_tokens()
        self.timeout = LLMConfig.get_timeout()
    
    def call_api(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        调用 LLM API
        
        Args:
            messages: 消息历史
            system: 系统提示词
            tools: 工具定义
            max_tokens: 最大 token 数
            
        Returns:
            API 响应
            
        Raises:
            requests.RequestException: API 调用失败
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
            "x-api-key": self.auth_token,
            "anthropic-version": "2023-06-01"
        }
        
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens
        }
        
        if system:
            payload["system"] = system
        
        if tools:
            payload["tools"] = tools
        
        response = requests.post(
            f"{self.base_url}/v1/messages",
            headers=headers,
            json=payload,
            timeout=self.timeout
        )
        
        response.raise_for_status()
        return response.json()
    
    def create_message(
        self,
        messages: List[Dict[str, Any]],
        system: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        max_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """创建消息（别名）"""
        return self.call_api(messages, system, tools, max_tokens)
