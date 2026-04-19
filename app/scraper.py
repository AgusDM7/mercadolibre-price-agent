"""
Scraper de MercadoLibre Argentina.
Usa httpx para requests HTTP y BeautifulSoup4 para parsear HTML.
Extrae: título, precio, condición, envío y URL de cada producto.
"""

import asyncio
import logging
import random
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.models import Producto

logger = logging.getLogger(__name__)

# Pool de User-Agents reales para rotar y evitar bloqueos
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# URL base de búsqueda en MercadoLibre Argentina
ML_BASE_URL = "https://listado.mercadolibre.com.ar"


def _construir_headers() -> dict[str, str]:
    """Construye headers HTTP que imitan un navegador real."""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-AR,es;q=0.9,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _construir_url(query: str) -> str:
    """
    Construye la URL de búsqueda de MercadoLibre.
    Envolvemos la query en comillas dobles para forzar búsqueda por frase.
    Sin comillas, ML cae en "búsqueda ampliada" cuando el último término tiene
    
    """
    query_entrecomillada = f'"{query}"'
    return f"{ML_BASE_URL}/{quote_plus(query_entrecomillada)}"


def _extraer_precio(texto: str) -> float | None:
    """
    Convierte texto de precio de ML a float.
    Ejemplo: "1.399.999" → 1399999.0
    """
    try:
        limpio = texto.strip().replace("$", "").replace(".", "").replace(",", ".")
        numeros = "".join(c for c in limpio if c.isdigit() or c == ".")
        return float(numeros) if numeros else None
    except (ValueError, AttributeError):
        return None


def _parsear_productos(html: str, max_productos: int) -> list[Producto]:
    """
    Parsea el HTML de resultados de MercadoLibre.
    ML usa tarjetas "poly-card" dentro de elementos <li> del layout.
    Extrae título, precio, condición, envío y URL de cada producto.
    """
    soup = BeautifulSoup(html, "lxml")
    productos: list[Producto] = []

    # Selector principal: items del grid de resultados
    items = soup.select("li.ui-search-layout__item")

    logger.info(f"Se encontraron {len(items)} items en el HTML")

    for item in items[:max_productos]:
        try:
            # --- Título (link dentro de la poly-card) ---
            titulo_elem = item.select_one("a.poly-component__title")
            titulo = titulo_elem.get_text(strip=True) if titulo_elem else None

            # --- URL (del mismo link del título) ---
            # Cortamos en '#' para descartar el fragment de tracking de ML (polycard_client, tracking_id, etc.)
            # que infla los tokens del prompt al LLM sin aportar información útil.
            url_raw = titulo_elem.get("href", "") if titulo_elem else ""
            url = url_raw.split("#", 1)[0]

            # --- Precio (primer fracción dentro del precio actual) ---
            precio_elem = item.select_one(
                ".poly-price__current .andes-money-amount__fraction"
            )
            precio_texto = precio_elem.get_text(strip=True) if precio_elem else None
            precio = _extraer_precio(precio_texto) if precio_texto else None

            # --- Envío ---
            envio_elem = item.select_one(".poly-component__shipping")
            envio = envio_elem.get_text(strip=True) if envio_elem else "No especificado"

            # --- Badge verificado por ML (no manipulable por el vendedor) ---
            es_tienda_oficial = item.select_one('svg[aria-label="Tienda oficial"]') is not None

            # --- Calificación ---
            review_elem = item.select_one(".poly-component__review-compacted")
            calificacion_producto = review_elem.get_text(strip=True) if review_elem else "Sin calificación"

            # --- Destacado (ej: "MÁS VENDIDO") ---
            highlight_elem = item.select_one(".poly-component__highlight")
            destacado = highlight_elem.get_text(strip=True) if highlight_elem else ""


            # Solo agregar si tenemos datos mínimos (título, precio, url)
            if titulo and precio and url:
                productos.append(
                    Producto(
                        titulo=titulo,
                        precio=precio,
                        envio=envio,
                        es_tienda_oficial=es_tienda_oficial,
                        calificacion_producto=calificacion_producto,
                        destacado=destacado,
                        url=url,
                    )
                )
        except Exception as e:
            logger.warning(f"Error parseando un producto: {e}")
            continue

    return productos


async def scrape_mercadolibre(query: str) -> list[Producto]:
    """
    Función principal de scraping.
    Busca productos en MercadoLibre Argentina y retorna una lista parseada.

    Args:
        query: Término de búsqueda (ej: "iPhone 15 128gb")

    Returns:
        Lista de Producto con los resultados encontrados

    Raises:
        httpx.HTTPStatusError: Si ML responde con error HTTP
        Exception: Otros errores de red o parseo
    """
    settings = get_settings()
    url_ml = _construir_url(query)
    headers = _construir_headers()
    usando_proxy = bool(settings.scrapfly_api_key)

    if usando_proxy:
        logger.info(f"Scrapeando vía ScrapFly: {url_ml}")
    else:
        logger.info(f"Scrapeando MercadoLibre (directo): {url_ml}")

    async with httpx.AsyncClient(
        follow_redirects=True,
        # ScrapFly con render_js+residential puede tardar 30-60s: arranca un browser,
        # rota IPs residenciales AR y espera que renderice JS de ML.
        timeout=httpx.Timeout(90.0 if usando_proxy else 15.0),
    ) as client:
        # Delay para respetar rate limiting
        await asyncio.sleep(settings.scraping_delay)

        if usando_proxy:
            # Config confirmada que pasa el bloqueo de ML: residential + render_js + ASP.
            # Costo: 30 créditos/scrape (25 residential + 5 browser). ~33 búsquedas/mes
            # con el free tier de 1000 créditos.
            response = await client.get(
                "https://api.scrapfly.io/scrape",
                params={
                    "key": settings.scrapfly_api_key,
                    "url": url_ml,
                    "country": "ar",
                    "proxy_pool": "public_residential_pool",
                    "render_js": "true",
                    "asp": "true",
                },
            )
        else:
            response = await client.get(url_ml, headers=headers)

        response.raise_for_status()

        # ScrapFly envuelve la respuesta en JSON: {"result": {"content": "<html>...", "status_code": 200, ...}}
        # En modo directo httpx ya nos da el HTML en response.text.
        if usando_proxy:
            data = response.json()
            result = data.get("result", {})
            html = result.get("content", "")
            status_destino = result.get("status_code", response.status_code)
            logger.info(f"Respuesta de ML vía ScrapFly: status={status_destino}, largo={len(html)}")
        else:
            html = response.text
            logger.info(f"Respuesta de ML: status={response.status_code}, largo={len(html)}")

            # Detectar bloqueo anti-bot cuando vamos sin proxy: redirige a /gz/account-verification.
            url_final = str(response.url)
            if "account-verification" in url_final:
                logger.error(
                    f"ML bloqueó la request redirigiendo a verificación: {url_final}. "
                    "Configurar SCRAPFLY_API_KEY para pasar el bloqueo."
                )
                return []

    productos = _parsear_productos(html, settings.max_productos)
    logger.info(f"Se extrajeron {len(productos)} productos para '{query}'")

    return productos