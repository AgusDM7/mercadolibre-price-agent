"""
Tareas Celery para procesamiento asíncrono.
Orquesta el flujo: scraping → análisis IA → guardar resultado en Redis.
"""

import asyncio
import logging

from dotenv import load_dotenv

# Cargar .env ANTES de leer configuración (Celery importa este módulo temprano)
load_dotenv()

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# --- Configuración de Celery ---
# Usa Redis como broker (cola de mensajes) y backend (almacén de resultados)
celery_app = Celery(
    "mercadolibre_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    # Serialización JSON para compatibilidad
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Timeout de tareas: 2 minutos máximo
    task_soft_time_limit=120,
    task_time_limit=150,
    # Resultados expiran después del TTL configurado
    result_expires=settings.cache_ttl_minutos * 60,
    # Worker optimizado para un solo proceso
    worker_concurrency=2, # 2 workers simultáneos
    worker_prefetch_multiplier=1, # cada worker agarra 1 tarea a la vez

    #worker_hijack_root_logger=False, #en casos dode no se nuestren los Logs especificos de app.*
)


def _ejecutar_async(coroutine):
    """
    Helper para ejecutar coroutines async dentro de tareas Celery (que son sync).
    Celery con --pool=threads no tiene un event loop propio, así que asyncio.run() es suficiente.
    """
    return asyncio.run(coroutine)


@celery_app.task(bind=True, name="buscar_y_analizar") # referencia a la tarea misma como `self`
def buscar_y_analizar(self, query: str) -> dict:
    
    # Importaciones dentro de la tarea para evitar imports circulares
    from app.scraper import scrape_mercadolibre
    from app.analyzer import analizar_productos
    
    logger.info(f"[Tarea {self.request.id}] Iniciando búsqueda: '{query}'")

    try:
        # Paso 1: Actualizar estado a "procesando"
        self.update_state(state="PROCESANDO", meta={"etapa": "scraping"}) # meta = información adicional

        # Paso 2: Scraping de MercadoLibre
        logger.info(f"[Tarea {self.request.id}] Scrapeando MercadoLibre...")
        productos = _ejecutar_async(scrape_mercadolibre(query))

        if not productos:
            return {
                "estado": "sin_resultados",
                "error": f"No se encontraron productos para '{query}'",
                "query": query,
            }

        logger.info(f"[Tarea {self.request.id}] {len(productos)} productos encontrados")

        # Paso 3: Análisis con GPT-4o mini
        self.update_state(state="PROCESANDO", meta={"etapa": "analizando"})
        logger.info(f"[Tarea {self.request.id}] Analizando con IA...")

        resultado = _ejecutar_async(analizar_productos(query, productos))

        logger.info(f"[Tarea {self.request.id}] Análisis completado exitosamente")


        #Celery guarda esto en Redis automáticamente
        return {
            "estado": "completado",
            "resultado": resultado.model_dump(),
        }

    except SoftTimeLimitExceeded: # ← se dispara cuando pasan 120 segundos
        logger.error(f"[Tarea {self.request.id}] Timeout excedido")
        return {
            "estado": "error",
            "error": "La búsqueda tardó demasiado. Intentá de nuevo.",
            "query": query,
        }
    

    except Exception as e:
        logger.error(f"[Tarea {self.request.id}] Error: {e}", exc_info=True)
        return {
            "estado": "error",
            "error": f"Error procesando la búsqueda: {str(e)}",
            "query": query,
        }
