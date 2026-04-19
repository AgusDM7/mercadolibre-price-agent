"""
Aplicación principal FastAPI.
- Sirve la interfaz web (Jinja2 + HTMX)
- Endpoints para búsqueda y consulta de resultados
- Inicia el worker Celery como thread interno
- APScheduler para keep-alive
"""

import hashlib # generar hashes MD5 para claves de cache
import html
import json
import logging
import threading # Correr Celery como thread   
import time
from collections import defaultdict # Diccionario con valor por defecto (para rate limiting)
from contextlib import asynccontextmanager # Decorador para el lifespan de FastAPI  
from urllib.parse import quote_plus

import redis
from apscheduler.schedulers.background import BackgroundScheduler # Scheduler para self-ping
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.tasks import buscar_y_analizar, celery_app

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    #resultado 2026-04-16 14:23:45 [INFO] app.scraper: Scrapeando MercadoLibre: https://...
)

# Silenciamos httpx: por defecto loggea cada request con URL completa, lo que
# filtra la SCRAPER_API_KEY (va en query string) y las llamadas a OpenAI.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Rate limiting en memoria (por IP) ---
# Estructura: {ip: [timestamp1, timestamp2, ...]}
rate_limit_registro: dict[str, list[float]] = defaultdict(list)


def _verificar_rate_limit(ip: str) -> bool:
    """
    Verifica si una IP superó el límite de búsquedas por hora.
    Retorna True si está dentro del límite, False si lo superó.
    """
    settings = get_settings()
    ahora = time.time()
    una_hora = 3600

    # Limpiar registros viejos (más de 1 hora)
    rate_limit_registro[ip] = [
        t for t in rate_limit_registro[ip] if ahora - t < una_hora
    ]

    if len(rate_limit_registro[ip]) >= settings.max_busquedas_por_hora:
        return False

    rate_limit_registro[ip].append(ahora)
    return True


def _cache_key(query: str) -> str:
    """Genera una clave de cache consistente para una búsqueda."""
    query_normalizado = query.strip().lower()
    return f"cache:busqueda:{hashlib.md5(query_normalizado.encode()).hexdigest()}"


def _iniciar_worker_celery():
    """
    Inicia el worker de Celery en un thread separado.
    Esto permite correr todo en un solo proceso (requerido por Render free tier).
    """
    def run_worker():
        logger.info("Iniciando worker Celery en thread interno...")
        celery_app.worker_main([
            "worker",
            "--loglevel=info", # Nivel de log info para ver tareas ejecutándose
            "--concurrency=2", # Máximo 2 tareas en paralelo
            "--pool=threads",  # Usar threads en vez de procesos (compatible con un solo dyno)
            "-Q", "celery", # Escuchar solo la cola "celery" (la que usamos para nuestras tareas)
            "--without-heartbeat", # Desactiva el chequeo de heartbeat (no hay múltiples workers)
            "--without-mingle", # Desactiva sincronización entre workers (no hay otros)
            "--without-gossip", # Desactiva comunicación entre workers
        ])

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    logger.info("Worker Celery iniciado como thread daemon")


def _iniciar_scheduler(): #A PScheduler es un **scheduler** (planificador) de tareas en Python.
    """
    Inicia APScheduler para el self-ping keep-alive.
    Hace ping al propio servicio cada 10 minutos para evitar que Render lo duerma.
    """
    import httpx

    def self_ping():
        try:
            with httpx.Client(timeout=10) as client:
                response = client.get("http://127.0.0.1:8000/health")
                logger.debug(f"Self-ping: {response.status_code}")
        except Exception as e:
            logger.warning(f"Self-ping falló: {e}")

    scheduler = BackgroundScheduler()
    scheduler.add_job(self_ping, "interval", minutes=10, id="keep_alive")
    scheduler.start()
    logger.info("APScheduler keep-alive iniciado (cada 10 min)")


# --- Pool de conexiones Redis (reutilizable) ---
redis_client: redis.Redis | None = None


def _get_redis() -> redis.Redis:
    """Retorna el cliente Redis con connection pool."""
    global redis_client
    if redis_client is None:
        # Crear por primera vez
        settings = get_settings()
        pool = redis.ConnectionPool.from_url(settings.redis_url)
        redis_client = redis.Redis(connection_pool=pool)
    return redis_client


# --- Lifecycle de FastAPI ---
@asynccontextmanager # Convierte la función en un context manager asíncrono. FastAPI lo llama automáticamente cuando inicia y cierra.
async def lifespan(app: FastAPI):
    """Maneja el inicio y cierre de la aplicación."""
    logger.info("Iniciando aplicación MercadoLibre Agent...")
    _iniciar_worker_celery()
    _iniciar_scheduler()
    yield # se ejecuta al cerrar
    logger.info("Cerrando aplicación...")
    if redis_client:
        redis_client.close()


# --- App FastAPI ---
app = FastAPI(
    title="MercadoLibre Agent",
    description="Agente IA para analizar productos de MercadoLibre Argentina",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# --- Endpoints ---

# Endpoint 1: `/health` (para keep-alive)
@app.get("/health")
async def health():
    """Endpoint de salud para UptimeRobot y self-ping."""
    return {"status": "ok"}


# Endpoint 2: `/` (página principal)
@app.get("/", response_class=HTMLResponse) # Le dice a FastAPI que esta respuesta es HTML, no JSON.
async def home(request: Request):
    """Página principal con el buscador."""
    return templates.TemplateResponse("index.html", {"request": request}) # Renderiza el archivo `index.html` usando Jinja2. El `{"request": request}` es necesario para que Jinja2 tenga acceso al request


# Endpoint 3: `/buscar` (para iniciar la búsqueda, recibe el query por form data)
@app.post("/buscar", response_class=HTMLResponse)
async def buscar(request: Request):
    """
    Inicia una búsqueda asíncrona.
    Recibe el query por form data (HTMX POST).
    Retorna HTML parcial con el loader y el polling configurado.
    """
    form = await request.form()
    query = form.get("query", "").strip()


    # Validación 1: query vacío
    if not query:
        return HTMLResponse(
            '<div class="error-msg">Escribí un producto para buscar</div>'
        )

    # Validación 2: query muy corto
    if len(query) < 3:
        return HTMLResponse(
            '<div class="error-msg">El término de búsqueda es muy corto (mínimo 3 caracteres)</div>'
        )

    # Validación 3: query muy largo
    if len(query) > 100:
        return HTMLResponse(
            '<div class="error-msg">El término de búsqueda es muy largo (máximo 100 caracteres)</div>'
        )

    # Validación 4: rate limit por IP
    ip = request.client.host if request.client else "unknown"
    if not _verificar_rate_limit(ip):
        return HTMLResponse(
            '<div class="error-msg">Superaste el límite de búsquedas por hora. Intentá en unos minutos.</div>'
        )

    # Verificar cache en Redis devolver HTML de resultados directamente (sin tarea)
    try:
        r = _get_redis()
        cache = r.get(_cache_key(query))
        if cache:
            logger.info(f"Cache hit para '{query}'")
            resultado = json.loads(cache)
            return HTMLResponse(_renderizar_resultado(resultado))
    except Exception as e:
        logger.warning(f"Error accediendo al cache: {e}")
    

    # Lanzar tarea Celery
    task = buscar_y_analizar.delay(query)
    logger.info(f"Tarea creada: {task.id} para '{query}'")

    # Retornar HTML con polling HTMX (consulta cada 2 segundos)
    query_encoded = html.escape(quote_plus(query))
    return HTMLResponse(f"""
        <div hx-get="/resultado/{task.id}?query={query_encoded}"
             hx-trigger="every 2s"
             hx-target="#resultado"
             hx-swap="innerHTML">
            <div class="loader">
                <div class="spinner"></div>
                <p class="loader-text">Buscando productos en MercadoLibre...</p>
                <p class="loader-subtext">Analizando con IA — esto puede tardar unos segundos</p>
            </div>
        </div>
    """)

# html.escape`: Codifica para HTML (`<` → `&lt;`, `"` → `&quot;`). **Previene XSS**.
# XSS (Cross-Site Scripting)** es un ataque donde alguien mete código HTML/JS malicioso en inputs del usuario. Si no lo escapás, se ejecuta en el navegador de otros usuarios.



# Endpoint 4: `/resultado/{task_id}` (polling)
@app.get("/resultado/{task_id}", response_class=HTMLResponse)
async def obtener_resultado(task_id: str, query: str = ""):
    """
    Consulta el estado de una tarea Celery.
    HTMX hace polling a este endpoint cada 2 segundos.
    Cuando la tarea termina, retorna el HTML con los resultados.
    """
    task = buscar_y_analizar.AsyncResult(task_id)

    # Tarea aún en progreso
    if task.state in ("PENDING", "PROCESANDO", "STARTED"):
        etapa = ""
        if task.info and isinstance(task.info, dict):
            etapa = task.info.get("etapa", "")

        texto_etapa = {
            "scraping": "Extrayendo productos de MercadoLibre...",
            "analizando": "Analizando productos con IA...",
        }.get(etapa, "Procesando tu búsqueda...")

        query_encoded = html.escape(quote_plus(query))
        return HTMLResponse(f"""
            <div hx-get="/resultado/{task_id}?query={query_encoded}"
                 hx-trigger="every 2s"
                 hx-target="#resultado"
                 hx-swap="innerHTML">
                <div class="loader">
                    <div class="spinner"></div>
                    <p class="loader-text">{texto_etapa}</p>
                </div>
            </div>
        """)

    # Tarea falló
    if task.state == "FAILURE":
        return HTMLResponse("""
            <div class="error-msg">
                Ocurrió un error inesperado. Intentá de nuevo.
            </div>
        """)

    # Tarea completada
    resultado = task.result

    if not resultado:
        return HTMLResponse("""
            <div class="error-msg">No se pudo obtener el resultado. Intentá de nuevo.</div>
        """)

    # Error controlado (sin resultados, timeout, etc.)
    if resultado.get("estado") in ("error", "sin_resultados"):
        return HTMLResponse(f"""
            <div class="error-msg">{resultado.get("error", "Error desconocido")}</div>
        """)

    # Guardar en cache para futuras búsquedas idénticas
    try:
        settings = get_settings()
        r = _get_redis()
        r.setex(
            _cache_key(query),
            settings.cache_ttl_minutos * 60,
            json.dumps(resultado["resultado"]),
        )
    except Exception as e:
        logger.warning(f"Error guardando cache: {e}")

    return HTMLResponse(_renderizar_resultado(resultado["resultado"]))


def _renderizar_resultado(data: dict) -> str:
    """
    Genera el HTML de resultados directamente.
    Evita complejidad de templates parciales — HTMX solo necesita HTML.
    Todos los datos dinámicos se escapan con html.escape() para prevenir XSS.
    """
    esc = html.escape
    mejor = data.get("mejor_opcion", {})
    productos = data.get("productos_analizados", [])
    resumen = esc(data.get("resumen", ""))

    # Tarjeta de mejor opción
    confiable_badge = (
        '<span class="badge badge-ok">Vendedor confiable</span>'
        if mejor.get("es_confiable")
        else '<span class="badge badge-warn">Vendedor no verificado</span>'
    )

    vale_badge = (
        '<span class="badge badge-ok">Vale la pena</span>'
        if mejor.get("vale_la_pena")
        else '<span class="badge badge-warn">No recomendado</span>'
    )

    mejor_titulo = esc(mejor.get("titulo", "N/A"))
    mejor_url = esc(mejor.get("url", "#"))
    mejor_razon = esc(mejor.get("razon_confianza", ""))

    resultado_html = f"""
        <div class="resumen-card">
            <h2>Resumen del análisis</h2>
            <p>{resumen}</p>
        </div>

        <div class="mejor-opcion-card">
            <h2>⭐ Mejor opción</h2>
            <h3>{mejor_titulo}</h3>
            <p class="precio-grande">${mejor.get("precio", 0):,.0f}</p>
            <div class="badges">
                {confiable_badge}
                {vale_badge}
            </div>
            <p class="razon">{mejor_razon}</p>
            <a href="{mejor_url}" target="_blank" rel="noopener" class="btn-ver">
                Ver en MercadoLibre →
            </a>
        </div>

        <h2 class="tabla-titulo">Todos los productos analizados ({len(productos)})</h2>
        <div class="tabla-container">
            <table>
                <thead>
                    <tr>
                        <th>Producto</th>
                        <th>Precio</th>
                        <th>Confiable</th>
                        <th>¿Vale la pena?</th>
                        <th>Link</th>
                    </tr>
                </thead>
                <tbody>
    """

    for p in productos:
        conf = "✅" if p.get("es_confiable") else "⚠️"
        vale = "✅" if p.get("vale_la_pena") else "❌"
        p_titulo = esc(p.get("titulo", "N/A"))
        p_url = esc(p.get("url", "#"))
        resultado_html += f"""
                    <tr>
                        <td class="td-titulo">{p_titulo}</td>
                        <td class="td-precio">${p.get("precio", 0):,.0f}</td>
                        <td class="td-center">{conf}</td>
                        <td class="td-center">{vale}</td>
                        <td><a href="{p_url}" target="_blank" rel="noopener">Ver →</a></td>
                    </tr>
        """

    resultado_html += """
                </tbody>
            </table>
        </div>
    """

    return resultado_html


# --- Punto de entrada para desarrollo local ---
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
