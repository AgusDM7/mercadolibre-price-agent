<div align="center">

# MercadoLibre Price Agent

**Agente inteligente que analiza productos de MercadoLibre Argentina y recomienda la mejor compra usando IA.**

[![Live Demo](https://img.shields.io/badge/Live_Demo-Online-22c55e?style=for-the-badge&logo=render&logoColor=white)](https://mercadolibre-price-agent.onrender.com/)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/GPT--4o_mini-412991?style=for-the-badge&logo=openai&logoColor=white)](https://openai.com/)
[![LangChain](https://img.shields.io/badge/LangChain-1C3C3C?style=for-the-badge&logo=langchain&logoColor=white)](https://www.langchain.com/)
[![Redis](https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white)](https://redis.io/)
[![Celery](https://img.shields.io/badge/Celery-37814A?style=for-the-badge&logo=celery&logoColor=white)](https://docs.celeryq.dev/)
[![HTMX](https://img.shields.io/badge/HTMX-3366CC?style=for-the-badge&logo=htmx&logoColor=white)](https://htmx.org/)

### [**→ Probar la app en vivo**](https://mercadolibre-price-agent.onrender.com/)

</div>

---

## Descripción

**MercadoLibre Price Agent** es una aplicación web full-stack que automatiza el proceso de decisión de compra en MercadoLibre Argentina. El usuario ingresa un término de búsqueda y el sistema:

1. Realiza **web scraping asíncrono** del listado de productos.
2. Normaliza y ordena los datos para eliminar sesgo posicional.
3. Envía la información a **GPT-4o mini** vía **LangChain** con un prompt cuidadosamente diseñado.
4. Devuelve una recomendación **fundamentada** en señales verificables: Tienda oficial, envío, calificación del producto y coherencia de precio.

El resultado es una UI reactiva —sin frameworks JS pesados— que muestra el progreso en tiempo real mientras el backend hace el trabajo pesado.

---

## Habilidades técnicas demostradas

| Área | Tecnologías y prácticas |
|---|---|
| **Backend asíncrono** | FastAPI, `httpx` async, corrutinas, manejo de I/O concurrente |
| **Ingeniería de IA** | Prompt engineering, LangChain, GPT-4o mini, parseo tolerante de JSON |
| **Web scraping** | BeautifulSoup4 + lxml, rotación de User-Agent, uso de proxy residencial (ScrapFly) |
| **Procesamiento en background** | Celery con pool de threads embebido en el mismo proceso |
| **Cache y optimización** | Redis con TTL, hashing MD5 de queries normalizadas |
| **Rate limiting** | Ventana deslizante por IP en memoria |
| **Frontend sin build** | HTMX + Jinja2, polling reactivo, Server-Side Rendering |
| **Testing** | pytest con mocks de servicios externos (OpenAI, Redis, scraper) |
| **Diseño de sistemas** | Arquitectura monoproceso consciente de restricciones del free tier |

---

## Decisiones de diseño destacables

### Monoproceso por diseño
FastAPI, el worker de Celery y el scheduler corren dentro del mismo proceso Python. El worker se lanza como thread daemon con `--pool=threads` al arrancar, lo que permite desplegar todo el stack en un único dyno del free tier **sin renunciar al modelo de tareas asíncronas de Celery**.

### Anti-sesgo posicional en el prompt
Los LLMs tienden a priorizar los primeros elementos de una lista. Antes de enviar los productos al modelo, se los **reordena por precio ascendente**, eliminando la influencia del orden original (promocionados, relevancia) y forzando al modelo a justificar la elección con criterios explícitos.

### Reglas de confiabilidad codificadas
El system prompt fija la jerarquía de criterios: `Tienda oficial` pesa más que `envío gratis`, la ausencia de reseñas no descalifica un producto, y los empates técnicos se rompen por precio. El modelo no improvisa.

### Interfaz reactiva sin build step
Cero React, cero Vue, cero bundler. HTMX inserta fragmentos de HTML devueltos por el backend con polling cada 2 segundos. El estado de la tarea (`scraping` → `analizando` → `completado`) se refleja en la UI sin JavaScript escrito a mano.

### Cache determinista
Queries normalizadas comparten un hash MD5 como clave en Redis con TTL configurable, evitando llamadas repetidas —y costosas— a OpenAI.

---

## Stack técnico

| Componente | Tecnología |
|---|---|
| Backend | FastAPI + Uvicorn |
| Frontend | Jinja2 + HTMX |
| Scraping | httpx (async) + BeautifulSoup4 + lxml + ScrapFly |
| Tareas asíncronas | Celery con pool de threads |
| Broker y cache | Redis |
| IA | LangChain + GPT-4o mini |
| Deploy | Render (free tier) |

---

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

---

## Estructura del proyecto

```
app/
  config.py      Configuración cargada desde .env (pydantic-settings)
  models.py      Modelos Pydantic: Producto, AnalisisProducto, ResultadoAnalisis
  scraper.py     Scraping async de MercadoLibre con rotación de User-Agent
  analyzer.py    Pipeline LangChain: prompt + GPT-4o mini + parser JSON
  tasks.py       Tareas Celery (scraping + análisis)
  main.py        App FastAPI, endpoints, worker Celery embebido
  templates/     HTML con HTMX
  static/        Assets estáticos
tests/           Tests con mocks de ML, OpenAI y Redis
```

---

## Puesta en marcha local

**Requisitos:** Python 3.11+, Redis accesible, API key de OpenAI.

```bash
python -m venv venv
source venv/bin/activate         # En Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Editar con OPENAI_API_KEY y REDIS_URL
uvicorn app.main:app --reload
```

La app queda disponible en `http://localhost:8000`. El worker de Celery arranca automáticamente como thread dentro del mismo proceso.

### Tests

```bash
pytest tests/ -v
pytest tests/ --cov=app --cov-report=term-missing
```

Los tests usan mocks para no depender de servicios externos.

---

## Variables de entorno

| Variable | Obligatoria | Default | Descripción |
|---|---|---|---|
| `OPENAI_API_KEY` | Sí | — | API key de OpenAI |
| `REDIS_URL` | Sí | `redis://localhost:6379/0` | URL del broker/cache Redis |
| `MAX_PRODUCTOS` | No | `10` | Productos a extraer por búsqueda |
| `SCRAPING_DELAY` | No | `2.0` | Segundos entre requests a MercadoLibre |
| `CACHE_TTL_MINUTOS` | No | `15` | Duración del cache de resultados |
| `MAX_BUSQUEDAS_POR_HORA` | No | `5` | Límite de búsquedas por IP |
| `DEBUG` | No | `false` | Modo debug |

---

## Deploy en Render

El repositorio incluye `render.yaml` y `Procfile`. Flujo resumido:

1. Provisionar Redis (por ejemplo Redis Cloud free tier) y copiar la URL.
2. Crear un Web Service en Render apuntando al repo; configurar `OPENAI_API_KEY` y `REDIS_URL`.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## Consideraciones técnicas

- El scraper depende de los selectores CSS actuales de MercadoLibre. Cambios en el markup requieren actualizar [app/scraper.py](app/scraper.py).
- Las tareas Celery tienen un timeout suave de 120 segundos; búsquedas más lentas devuelven un error controlado en lugar de colgar la UI.
- El rate limit es por proceso; al escalar a múltiples instancias convendría moverlo a Redis.

---

## Autor

**Agustín Del Monte** — Desarrollador backend con foco en Python, IA aplicada y arquitecturas eficientes.

- GitHub: [@AgusDM7](https://github.com/AgusDM7)
- Email: delmonteagustin1@gmail.com


