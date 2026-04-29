# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos

Entorno virtual (Windows, bash):
```bash
source venv/Scripts/activate
```

Instalar dependencias:
```bash
pip install -r requirements.txt
```

Levantar la app en local (arranca también el worker Celery como thread interno):
```bash
uvicorn app.main:app --reload
```

Comando limpiar cache redis para test
```bash
python -c "import redis; from app.config import get_settings; r = redis.from_url(get_settings().redis_url); keys = r.keys('cache:busqueda:*'); print(f'Borrando {len(keys)} claves'); [r.delete(k) for k in keys]"
```

Tests:
```bash
pytest tests/ -v                      # Todos
pytest tests/test_scraper.py -v       # Un módulo
pytest tests/test_api.py::TestBuscar::test_query_vacio -v  # Un solo test
pytest tests/ --cov=app --cov-report=term-missing          # Con coverage
```

Redis local (requerido para Celery y cache):
```bash
redis-cli ping   # Debe responder PONG
```

## Arquitectura

Monoproceso por diseño (Render free tier): FastAPI + worker Celery corren dentro del mismo proceso Python. El keep-alive vive afuera, en un workflow de GitHub Actions.

Flujo de una búsqueda:
1. `POST /buscar` ([app/main.py](app/main.py)) valida input, chequea rate limit en memoria y mira cache en Redis.
2. Si no hay cache, encola `buscar_y_analizar.delay(query)` y devuelve HTML parcial con polling HTMX cada 2s.
3. La tarea Celery ([app/tasks.py](app/tasks.py)) ejecuta `scrape_mercadolibre` → `analizar_productos`, actualizando `state`/`meta.etapa` (`scraping` → `analizando`) para que el polling muestre progreso.
4. `GET /resultado/{task_id}` ([app/main.py:244](app/main.py#L244)) lee el estado de la tarea; al completarse guarda el resultado en Redis (`cache:busqueda:<md5(query)>`, TTL configurable) y renderiza HTML con `_renderizar_resultado`.

Puntos de arquitectura no obvios:
- **Worker Celery como thread daemon**: `_iniciar_worker_celery` en [app/main.py:70](app/main.py#L70) arranca Celery con `--pool=threads` dentro del mismo proceso FastAPI. No se corre `celery worker` por separado. Esto es intencional para caber en un solo dyno.
- **Celery usa Redis como broker Y backend**: la misma `REDIS_URL` sirve para cola de mensajes, resultados de tareas y cache de búsquedas.
- **Async dentro de tareas sync**: las tareas Celery son sync; usan el helper `_ejecutar_async` (que es `asyncio.run`) para invocar `scrape_mercadolibre` y `analizar_productos`, que son corrutinas.
- **Keep-alive externo**: el workflow [.github/workflows/keep-alive.yml](.github/workflows/keep-alive.yml) hace `curl` a `/health` cada 10 min desde GitHub Actions. Render duerme el servicio a los 15 min sin tráfico externo, y un self-ping interno (loopback) no contaba como tráfico — por eso el ping vive afuera del proceso.
- **Rate limiting en memoria**: `rate_limit_registro` es un `defaultdict` in-process ([app/main.py:40](app/main.py#L40)). No sobrevive a reinicios y no es compartido entre instancias — aceptable por el diseño monoproceso.
- **Orden anti-sesgo para el LLM**: `analizar_productos` ordena los productos por precio ascendente antes de armar el prompt ([app/analyzer.py:107](app/analyzer.py#L107)) para que GPT-4o mini no se deje llevar por la posición original del listado.
- **Prompt con reglas explícitas de confiabilidad**: las reglas (Tienda oficial como señal fuerte, calificación del PRODUCTO no del vendedor, empates van al precio más bajo) están hardcodeadas en `SYSTEM_PROMPT` de [app/analyzer.py](app/analyzer.py) — cambiarlas impacta directamente la recomendación.
- **Parseo tolerante del JSON de GPT**: `_parsear_producto` rellena con defaults los campos de análisis que GPT pueda omitir ([app/analyzer.py:167](app/analyzer.py#L167)).
- **Tests sin dependencias externas**: `tests/test_api.py` parchea `_iniciar_worker_celery` en la fixture `client` para que `TestClient` no dispare el worker real.

## Configuración

Variables en `.env` (cargadas por `pydantic-settings` en [app/config.py](app/config.py)). `tasks.py` llama `load_dotenv()` antes de importar settings porque Celery importa ese módulo temprano en el arranque.

Obligatorias: `OPENAI_API_KEY`, `REDIS_URL`.
Opcionales: `MAX_PRODUCTOS` (10), `SCRAPING_DELAY` (2.0), `CACHE_TTL_MINUTOS` (15), `MAX_BUSQUEDAS_POR_HORA` (5), `DEBUG` (false).

## Scraper

Depende de selectores CSS de MercadoLibre Argentina (`li.ui-search-layout__item`, `a.poly-component__title`, `.poly-price__current .andes-money-amount__fraction`, etc. en [app/scraper.py:64](app/scraper.py#L64)). Si ML cambia el markup, el scraper devuelve `[]` y la tarea retorna `estado: sin_resultados` — verificar los selectores antes de asumir otro bug.
