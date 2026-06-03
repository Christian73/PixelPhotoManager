from abc import abstractmethod
from PIL import Image
from .base_plugin import BasePlugin


class ProcessorPlugin(BasePlugin):
    name: str = ""
    description: str = ""
    category: str = "Général"
    icon: str = ""
    supports_preview: bool = True
    supports_batch: bool = True

    @abstractmethod
    def process(self, image: Image.Image, params: dict) -> Image.Image: ...

    @abstractmethod
    def get_default_params(self) -> dict: ...

    def validate_params(self, params: dict) -> dict:
        return {**self.get_default_params(), **params}

    def estimate_duration(self, image: Image.Image, params: dict) -> float:
        return 1.0
