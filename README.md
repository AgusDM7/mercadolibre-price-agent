# MercadoLibre Agent

Agente web que busca productos en MercadoLibre Argentina, los analiza con un modelo de lenguaje (GPT-4o mini) y devuelve una recomendación de compra fundamentada: qué producto conviene, por qué, y cuán confiable es el vendedor.

El usuario escribe un término de búsqueda, el sistema hace scraping del listado, ordena los resultados y pide al LLM una evaluación basada en señales verificables (Tienda oficial, envío, calificación, precio relativo). La respuesta llega en pocos segundos a través de una interfaz reactiva sin frameworks JS pesados.

## Lo que hace distinto a este proyecto

**Monoproceso por diseño.** FastAPI, el worker de Celery y el scheduler de keep-alive corren dentro del mismo proceso Python. El worker se lanza como thread daemon con `--pool=threads` al arrancar la app, lo que permite desplegar todo el stack en un único dyno del free tier de Render sin perder el modelo de tareas asíncronas de Celery.

**Anti-sesgo posicional en el prompt.** Antes de enviar los productos al LLM, se ordenan por precio ascendente. MercadoLibre devuelve los resultados en un orden mixto (promocionados, relevancia), y los LLM tienden a priorizar lo que aparece primero. Reordenar elimina esa influencia y obliga al modelo a justificar la elección con criterios explícitos: Tienda oficial verificada, envío, calificación del producto y coherencia de precio.

**Reglas de confiabilidad codificadas en el system prompt.** La lógica de decisión del modelo está fijada: `Tienda oficial` pesa más que `envío gratis`, la ausencia de reseñas no es motivo para desconfiar, y ante empates técnicos gana el precio más bajo. El modelo no improvisa criterios.

**Interfaz reactiva con HTMX.** No hay React, Vue ni build step de frontend. El backend devuelve fragmentos de HTML que HTMX inserta en el DOM, con polling cada 2 segundos al endpoint de resultado. Mientras la tarea Celery avanza por sus etapas (`scraping` → `analizando`), el estado se refleja en la UI sin código JavaScript escrito a mano.

**Cache determinista y rate limiting por IP.** Las búsquedas idénticas (normalizadas) comparten un hash MD5 como clave en Redis con TTL configurable, evitando llamadas repetidas a OpenAI. El rate limit es una ventana deslizante por IP mantenida en memoria del proceso, suficiente dado el diseño monoproceso.

**Keep-alive doble.** APScheduler hace self-ping al endpoint `/health` cada 10 minutos como respaldo interno; UptimeRobot como monitor externo. Render free tier duerme servicios inactivos a los 15 minutos, y esta redundancia mantiene la app despierta sin depender de un único mecanismo.

## Stack

| Componente        | Tecnología                              |
| ----------------- | --------------------------------------- |
| Backend           | FastAPI + Uvicorn                       |
| Frontend          | Jinja2 + HTMX                           |
| Scraping          | httpx (async) + BeautifulSoup4 + lxml   |
| Tareas asíncronas | Celery con pool de threads              |
| Broker y cache    | Redis                                   |
| IA                | LangChain + GPT-4o mini                 |
| Scheduler         | APScheduler                             |
| Deploy            | Render (free tier)                      |

## Arquitectura

```
Usuario
   |
   | POST /buscar  (HTMX)
   v
FastAPI  ---->  Rate limit (in-memory)
   |       \
   |        ---> Cache Redis (hit: responde HTML directo)
   |
   | celery.delay()
   v
Cola Redis  ---->  Worker Celery (thread del mismo proceso)
                         |
                         | 1. scrape_mercadolibre()   [httpx + BS4]
                         | 2. analizar_productos()    [LangChain + GPT-4o mini]
                         v
                   Resultado en Redis
   ^
   | GET /resultado/{task_id}  (polling HTMX cada 2s)
   |
Usuario
```

## Estructura del proyecto

```
app/
  config.py      Configuración cargada desde .env (pydantic-settings)
  models.py      Modelos Pydantic: Producto, AnalisisProducto, ResultadoAnalisis
  scraper.py     Scraping async de MercadoLibre con rotación de User-Agent
  analyzer.py    Pipeline LangChain: prompt + GPT-4o mini + parser JSON
  tasks.py       Tareas Celery (scraping + análisis)
  main.py        App FastAPI, endpoints, worker embebido, scheduler
  templates/     HTML con HTMX
  static/        Assets estáticos
tests/           Tests con mocks de ML, OpenAI y Redis
```

## Puesta en marcha

Requisitos: Python 3.11+, Redis accesible, API key de OpenAI.

```bash
python -m venv venv
source venv/bin/activate         # En Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Editar con OPENAI_API_KEY y REDIS_URL
uvicorn app.main:app --reload
```

La app queda disponible en `http://localhost:8000`. El worker de Celery y el scheduler de keep-alive arrancan automáticamente dentro del mismo proceso.

### Tests

```bash
pytest tests/ -v
pytest tests/ --cov=app --cov-report=term-missing
```

Los tests usan mocks para no depender de servicios externos.

## Variables de entorno

| Variable                  | Obligatoria | Default                    | Descripción                             |
| ------------------------- | ----------- | -------------------------- | --------------------------------------- |
| `OPENAI_API_KEY`          | Sí          | —                          | API key de OpenAI                       |
| `REDIS_URL`               | Sí          | `redis://localhost:6379/0` | URL del broker/cache Redis              |
| `MAX_PRODUCTOS`           | No          | `10`                       | Productos a extraer por búsqueda        |
| `SCRAPING_DELAY`          | No          | `2.0`                      | Segundos entre requests a MercadoLibre  |
| `CACHE_TTL_MINUTOS`       | No          | `15`                       | Duración del cache de resultados        |
| `MAX_BUSQUEDAS_POR_HORA`  | No          | `5`                        | Límite de búsquedas por IP              |
| `DEBUG`                   | No          | `false`                    | Modo debug                              |

## Deploy en Render

El repositorio incluye `render.yaml` y `Procfile`. El flujo resumido es:

1. Provisionar una base Redis (por ejemplo Redis Cloud free tier) y copiar la URL.
2. Crear un Web Service en Render apuntando al repo; configurar `OPENAI_API_KEY` y `REDIS_URL` en las variables de entorno.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Agregar un monitor externo (UptimeRobot) al endpoint `/health` cada 5 minutos.

## Consideraciones

- El scraper depende de los selectores CSS actuales de MercadoLibre (`li.ui-search-layout__item`, `poly-component__title`, etc.). Cambios en el markup de ML requieren actualizar `app/scraper.py`.
- Las tareas Celery tienen un timeout suave de 120 segundos; búsquedas más lentas devuelven un error controlado en lugar de colgar la UI.
- El rate limit es por proceso; al correr múltiples instancias convendría moverlo a Redis.

## Licencia

MIT.
