"""
Scraper de MercadoLibre Argentina.
Usa httpx para requests HTTP y BeautifulSoup4 para parsear HTML.
Extrae: título, precio, condición, envío y URL de cada producto.
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings
from app.models import Producto

logger = logging.getLogger(__name__)

# --- Diagnóstico ---
# Directorio donde se vuelcan HTMLs de respuestas problemáticas, para poder
# inspeccionar después qué pasó (bloqueo nuevo vs. selectores viejos) sin
# depender de reproducir el error en vivo.
DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug" / "ml_debug"


# Frases/strings típicos de páginas de bloqueo, challenge o captcha.
# Si aparecen en el HTML, lo más probable es que NO haya sido un cambio de
# selectores sino que directamente no llegó la página de resultados.
MARCADORES_BLOQUEO = [
    "account-verification",
    "captcha",
    "verificá que sos humano",
    "verifica que sos humano",
    "unusual traffic",
    "tráfico inusual",
    "trafico inusual",
    "px-captcha",
    "perimeterx",
    "distil",
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "attention required",
    "acceso denegado",
    "access denied",
]

# Selectores alternativos a probar cuando el principal (li.ui-search-layout__item)
# no matchea nada. Sirve para distinguir "ML renombró la clase" (algún candidato
# devuelve > 0) de "no llegó el HTML de resultados" (todos dan 0).
SELECTORES_CANDIDATOS = [
    "li.ui-search-layout__item",
    "ol.ui-search-layout li",
    "li[class*='ui-search-layout']",
    "div[class*='poly-card']",
    "div.poly-card",
    "[class*='poly-component__title']",
    "a[class*='poly-component__title']",
    "[class*='andes-money-amount__fraction']",
]

# Cantidad máxima de intentos por búsqueda. El bloqueo de ML (redirect a
# account-verification) resultó ser intermitente: confirmado con datos reales
# de ScrapFly donde el mismo tipo de búsqueda fue bloqueada una vez y, reintentada
# poco después, pasó sin problema. Por eso vale la pena un reintento antes de
# rendirse. No subir mucho este número: cada intento con proxy tarda 15-30s y
# el task_soft_time_limit de Celery es 120s (incluye después el análisis con IA).
MAX_INTENTOS = 2
ESPERA_ENTRE_INTENTOS_SEGUNDOS = 2.0


def _url_indica_bloqueo(url: str) -> bool:
    """True si la URL final de la request es la de verificación anti-bot de ML."""
    return "account-verification" in (url or "")


def _html_indica_bloqueo(html: str) -> bool:
    """True si el HTML contiene algún marcador conocido de bloqueo/challenge/captcha."""
    html_lower = (html or "").lower()
    return any(m in html_lower for m in MARCADORES_BLOQUEO)


def _volcar_debug(contenido: str, query: str, sufijo: str = "") -> None:
    """
    Escribe `contenido` a un archivo en DEBUG_DIR para inspección manual.
    Si DEBUG_DIR no es escribible (permisos, filesystem read-only, etc.)
    cae a tempfile.gettempdir() en vez de fallar en silencio sin dejar rastro.
    """
    import tempfile

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    query_slug = re.sub(r"[^a-zA-Z0-9]+", "-", query).strip("-")[:40] or "query"
    nombre = f"{timestamp}_{query_slug}{('_' + sufijo) if sufijo else ''}.html"

    for destino in (DEBUG_DIR, Path(tempfile.gettempdir()) / "ml_debug"):
        try:
            destino.mkdir(parents=True, exist_ok=True)
            ruta = destino / nombre
            ruta.write_text(contenido, encoding="utf-8", errors="replace")
            logger.error(f"[diagnóstico] Volcado a {ruta} para inspección manual.")
            return
        except Exception as e:
            logger.warning(f"[diagnóstico] No se pudo escribir en {destino}: {e}")

    logger.error(
        "[diagnóstico] No se pudo volcar el debug a NINGÚN directorio "
        f"(probados: {DEBUG_DIR}, {Path(tempfile.gettempdir()) / 'ml_debug'}). "
        "Revisar permisos de escritura del contenedor."
    )


def _diagnosticar_html_sin_resultados(html: str, query: str, usando_proxy: bool) -> None:
    """
    Se llama cuando el scraping devolvió 0 productos.
    No cambia el comportamiento (sigue devolviendo lista vacía más arriba),
    pero deja rastro suficiente para diagnosticar SIN tener que reproducir
    el fallo en vivo: guarda el HTML crudo y logea qué selectores matchean.
    """
    largo = len(html)
    html_lower = html.lower()

    marcadores_encontrados = [m for m in MARCADORES_BLOQUEO if m in html_lower]

    conteos = {}
    try:
        soup = BeautifulSoup(html, "lxml")
        for sel in SELECTORES_CANDIDATOS:
            try:
                conteos[sel] = len(soup.select(sel))
            except Exception:
                conteos[sel] = -1
    except Exception as e:
        logger.warning(f"[diagnóstico] No se pudo parsear el HTML para diagnóstico: {e}")

    if marcadores_encontrados:
        logger.error(
            f"[diagnóstico] Posible BLOQUEO/CHALLENGE detectado en la respuesta "
            f"(proxy={usando_proxy}). Marcadores encontrados: {marcadores_encontrados}"
        )
    elif any(c > 0 for c in conteos.values()):
        logger.error(
            f"[diagnóstico] El HTML parece ser una página de resultados válida "
            f"(largo={largo}) pero el selector principal 'li.ui-search-layout__item' "
            f"no matcheó nada. Conteos de selectores alternativos: {conteos}. "
            "Esto sugiere que ML renombró las clases CSS y hay que actualizar los "
            "selectores en _parsear_productos()."
        )
    else:
        logger.error(
            f"[diagnóstico] 0 productos y ningún selector candidato matcheó nada "
            f"(largo={largo}, proxy={usando_proxy}). No se detectaron marcadores de "
            "bloqueo conocidos tampoco — podría ser una página distinta a la esperada "
            "(ej: error genérico de ML, redirect, página vacía). Revisar el HTML volcado."
        )

    # Volcar el HTML crudo a disco para poder inspeccionarlo después.
    _volcar_debug(html, query, sufijo="0_productos")


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
    return f"{ML_BASE_URL}/{query_entrecomillada}"


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


async def _intentar_request(
    client: httpx.AsyncClient,
    query: str,
    url_ml: str,
    headers: dict[str, str],
    usando_proxy: bool,
    settings,
) -> tuple[str, bool, str]:
    """
    Hace UN intento de request (proxy o directo) y devuelve (html, bloqueado, url_final).

    `bloqueado=True` significa que la request "funcionó" en términos de HTTP
    (no lanzó excepción) pero la página de destino fue la de verificación
    anti-bot de ML, o el HTML contiene algún marcador de challenge/captcha
    conocido — en ambos casos NO es un resultado real de búsqueda.

    Raises:
        httpx.HTTPStatusError: si ScrapFly/ML responde con status de error.
        Exception: si ScrapFly responde 200 pero el body no es JSON válido.
    """
    if usando_proxy:
        # NOTA: este comentario decía "sin render_js" pero el parámetro de abajo
        # está en "true" desde el commit 21f8ee6 (14/05). Si el bloqueo actual
        # empezó después de esa fecha, probar desactivarlo (render_js aumenta
        # el tiempo de respuesta y puede disparar detección de bot en algunos WAF).
        # Costo: 25 créditos/scrape (residential). ~40 búsquedas/mes free tier.
        response = await client.get(
            "https://api.scrapfly.io/scrape",
            params={
                "key": settings.scrapfly_api_key,
                "url": url_ml,
                "country": "ar",
                "proxy_pool": "public_residential_pool",
                "asp": "true",
                "render_js": "true",          # ejecuta el JS del challenge
                "wait_for_selector": "li.ui-search-layout__item",  # espera a que carguen los productos
            },
        )
    else:
        response = await client.get(url_ml, headers=headers)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Si ScrapFly/ML responde con un status de error (429, 401, 422, 5xx),
        # la excepción sale ANTES de llegar a parsear/diagnosticar el HTML.
        # ScrapFly casi siempre manda el motivo exacto en el body (JSON), así
        # que lo logueamos y lo volcamos a disco antes de relanzar. No se
        # reintenta este caso: un 429 de cuota no se arregla reintentando.
        cuerpo = e.response.text
        logger.error(
            f"[diagnóstico] Falló la request (proxy={usando_proxy}): "
            f"HTTP {e.response.status_code}. Body: {cuerpo[:2000]}"
        )
        _volcar_debug(
            f"HTTP_ERROR_{e.response.status_code}\n\n{cuerpo}",
            query,
            sufijo="error",
        )
        raise

    # ScrapFly envuelve la respuesta en JSON: {"result": {"content": "<html>...", "status_code": 200, "url": "...", ...}}
    # En modo directo httpx ya nos da el HTML en response.text.
    if usando_proxy:
        try:
            data = response.json()
        except Exception as e:
            # ScrapFly devolvió 200 pero el body no es JSON válido (raro, pero
            # posible si cambiaron el formato de respuesta de su API).
            logger.error(
                f"[diagnóstico] Respuesta 200 de ScrapFly pero no es JSON válido: {e}. "
                f"Body: {response.text[:2000]}"
            )
            _volcar_debug(response.text, query, sufijo="non_json")
            raise

        result = data.get("result", {})
        html = result.get("content", "")
        status_destino = result.get("status_code", response.status_code)
        # `result.url` es la URL FINAL a la que llegó ScrapFly después de
        # seguir redirects — acá es donde se ve si terminamos en la página
        # de verificación de ML, aunque ScrapFly haya devuelto 200 igual.
        url_final = result.get("url", "") or url_ml
        logger.info(
            f"Respuesta de ML vía ScrapFly: status={status_destino}, "
            f"largo={len(html)}, url_final={url_final}"
        )

        fallo_destino = not result.get("success", True) or (status_destino and status_destino >= 400)
        if fallo_destino:
            logger.warning(
                f"[diagnóstico] ScrapFly respondió 200 pero el fetch a ML falló: "
                f"success={result.get('success')}, status_destino={status_destino}, "
                f"error={result.get('error') or data.get('error')}"
            )
    else:
        html = response.text
        status_destino = response.status_code
        url_final = str(response.url)
        logger.info(f"Respuesta de ML: status={status_destino}, largo={len(html)}")
        fallo_destino = False

    bloqueado = _url_indica_bloqueo(url_final) or _html_indica_bloqueo(html) or fallo_destino
    return html, bloqueado, url_final


async def scrape_mercadolibre(query: str) -> list[Producto]:
    """
    Función principal de scraping.
    Busca productos en MercadoLibre Argentina y retorna una lista parseada.

    Reintenta hasta MAX_INTENTOS veces si detecta que ML bloqueó la request
    (redirect a account-verification, challenge/captcha, o fetch fallido del
    lado de ScrapFly): confirmado con logs reales que este bloqueo es
    intermitente y un segundo intento suele pasar.

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

    html = ""
    bloqueado = True
    url_final = ""

    async with httpx.AsyncClient(
        follow_redirects=True,
        # ScrapFly con residential+ASP tarda 10-30s rotando IPs AR.
        timeout=httpx.Timeout(60.0 if usando_proxy else 15.0),
    ) as client:
        for intento in range(1, MAX_INTENTOS + 1):
            # Delay para respetar rate limiting (también sirve como backoff
            # extra en los reintentos, no solo antes del primer intento).
            await asyncio.sleep(settings.scraping_delay)

            html, bloqueado, url_final = await _intentar_request(
                client, query, url_ml, headers, usando_proxy, settings
            )

            if not bloqueado:
                break

            if intento < MAX_INTENTOS:
                logger.warning(
                    f"[intento {intento}/{MAX_INTENTOS}] Bloqueo detectado para '{query}' "
                    f"(url_final={url_final}). Reintentando en {ESPERA_ENTRE_INTENTOS_SEGUNDOS}s..."
                )
                await asyncio.sleep(ESPERA_ENTRE_INTENTOS_SEGUNDOS)
            else:
                logger.error(
                    f"[intento {intento}/{MAX_INTENTOS}] Sigue bloqueado tras agotar reintentos "
                    f"para '{query}' (url_final={url_final})."
                )

    if bloqueado:
        # Bloqueo confirmado y persistente: no tiene sentido intentar parsear.
        _volcar_debug(html, query, sufijo="bloqueado_persistente")
        return []

    productos = _parsear_productos(html, settings.max_productos)
    logger.info(f"Se extrajeron {len(productos)} productos para '{query}'")

    if not productos:
        _diagnosticar_html_sin_resultados(html, query, usando_proxy)

    return productos