"""
Configuración centralizada del proyecto.
Carga variables de entorno desde .env usando pydantic-settings.
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Configuración del proyecto cargada desde variables de entorno."""

    # --- OpenAI ---
    openai_api_key: str

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Scraping ---
    # Máximo de productos a extraer por búsqueda
    max_productos: int = 10
    # Segundos entre requests a MercadoLibre
    scraping_delay: float = 2.0
    # Minutos que dura el cache de resultados
    cache_ttl_minutos: int = 15

    # --- Rate Limiting ---
    # Máximo de búsquedas por IP por hora
    max_busquedas_por_hora: int = 5

    # --- App ---
    debug: bool = False

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    """Retorna la configuración cacheada (singleton)."""
    return Settings()
