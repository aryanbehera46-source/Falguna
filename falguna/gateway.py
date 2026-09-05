from abc import ABC, abstractmethod
from typing import Dict


class ModelGateway(ABC):
    @abstractmethod
    def configuration(self) -> Dict[str, str]:
        raise NotImplementedError


class OpenAICompatibleGateway(ModelGateway):
    def __init__(self, model: str, base_url: str, api_key_file: str):
        self.model = model
        self.base_url = base_url
        self.api_key_file = api_key_file

    def configuration(self) -> Dict[str, str]:
        return {"model": self.model, "base_url": self.base_url, "api_key_file": self.api_key_file}


class LocalGateway(ModelGateway):
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434/v1"):
        self.model = model
        self.base_url = base_url

    def configuration(self) -> Dict[str, str]:
        return {"model": self.model, "base_url": self.base_url, "api_key_file": ""}

