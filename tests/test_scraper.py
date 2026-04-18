"""
Tests del módulo de scraping.
Usa HTML de ejemplo para verificar la extracción sin depender de MercadoLibre.
"""

import pytest

from app.scraper import _extraer_precio, _construir_url, _parsear_productos


class TestExtraerPrecio:
    """Tests para la función de extracción de precios."""

    def test_precio_normal(self):
        assert _extraer_precio("$150.000") == 150000.0

    def test_precio_con_millones(self):
        assert _extraer_precio("$1.250.500") == 1250500.0

    def test_precio_sin_signo(self):
        assert _extraer_precio("85.000") == 85000.0

    def test_precio_simple(self):
        assert _extraer_precio("$500") == 500.0

    def test_precio_invalido(self):
        assert _extraer_precio("") is None

    def test_precio_texto_basura(self):
        assert _extraer_precio("gratis") is None


class TestConstruirUrl:
    """Tests para la construcción de URLs de búsqueda."""

    def test_query_simple(self):
        url = _construir_url("iphone 15")
        assert "listado.mercadolibre.com.ar" in url
        assert "iphone" in url

    def test_query_con_caracteres_especiales(self):
        url = _construir_url("notebook 15.6\" lenovo")
        assert "listado.mercadolibre.com.ar" in url


class TestParsearProductos:
    """Tests con HTML simulado para verificar el parseo."""

    # HTML que replica la estructura real de MercadoLibre (selectores poly-card)
    HTML_EJEMPLO = """
    <html><body>
    <ol>
        <li class="ui-search-layout__item">
            <a class="poly-component__title" href="https://www.mercadolibre.com.ar/producto-1">
                iPhone 15 128GB Nuevo Sellado
            </a>
            <div class="poly-price__current">
                <span class="andes-money-amount__fraction">1.250.000</span>
            </div>
            <span class="poly-component__shipping">Envío gratis</span>
            <span class="poly-component__seller">Tienda Oficial</span>
            <svg aria-label="Tienda oficial"></svg>
            <span class="poly-component__review-compacted">4.8</span>
            <span class="poly-component__highlight">MÁS VENDIDO</span>
        </li>
        <li class="ui-search-layout__item">
            <a class="poly-component__title" href="https://www.mercadolibre.com.ar/producto-2">
                iPhone 15 128GB Usado
            </a>
            <div class="poly-price__current">
                <span class="andes-money-amount__fraction">950.000</span>
            </div>
            <span class="poly-component__shipping">A coordinar</span>
        </li>
    </ol>
    </body></html>
    """

    def test_extrae_productos(self):
        productos = _parsear_productos(self.HTML_EJEMPLO, max_productos=10)
        assert len(productos) == 2

    def test_datos_del_producto(self):
        productos = _parsear_productos(self.HTML_EJEMPLO, max_productos=10)
        p = productos[0]
        assert "iPhone" in p.titulo
        assert p.precio == 1250000.0
        assert "mercadolibre.com.ar" in p.url
        assert p.envio == "Envío gratis"
        assert p.es_tienda_oficial is True
        assert p.calificacion_producto == "4.8"
        assert p.destacado == "MÁS VENDIDO"

    def test_datos_producto_sin_extras(self):
        """Producto sin calificación/destacado usa defaults."""
        productos = _parsear_productos(self.HTML_EJEMPLO, max_productos=10)
        p = productos[1]
        assert p.es_tienda_oficial is False
        assert p.calificacion_producto == "Sin calificación"
        assert p.destacado == ""

    def test_respeta_max_productos(self):
        productos = _parsear_productos(self.HTML_EJEMPLO, max_productos=1)
        assert len(productos) == 1

    def test_html_vacio(self):
        productos = _parsear_productos("<html><body></body></html>", max_productos=10)
        assert len(productos) == 0
