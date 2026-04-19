"""
Tests del módulo de análisis IA.
Usa mocks para no depender de OpenAI durante los tests.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.analyzer import _formatear_productos, analizar_productos
from app.models import Producto


# Productos de ejemplo para tests
PRODUCTOS_EJEMPLO = [
    Producto(
        titulo="iPhone 15 128GB Nuevo",
        precio=1200000.0,
        envio="Envío gratis",
        url="https://mercadolibre.com.ar/p1",
    ),
    Producto(
        titulo="iPhone 15 128GB Usado",
        precio=900000.0,
        envio="A coordinar",
        url="https://mercadolibre.com.ar/p2",
    ),
]

# Respuesta simulada de GPT-4o mini
# Tras ordenar PRODUCTOS_EJEMPLO por precio: índice 1 = Usado ($900K), índice 2 = Nuevo ($1.2M)
RESPUESTA_MOCK = {
    "mejor_opcion_indice": 2,
    "resumen": "Se encontraron 2 opciones. La mejor relación precio-confianza es el nuevo con envío gratis.",
    "productos_analizados": [
        {
            "indice": 1,
            "es_confiable": False,
            "razon_confianza": "Sin envío gratis, producto usado",
            "vale_la_pena": False,
        },
        {
            "indice": 2,
            "es_confiable": True,
            "razon_confianza": "Envío gratis sugiere vendedor verificado",
            "vale_la_pena": True,
        },
    ],
}


class TestFormatearProductos:
    """Tests para el formateo de productos antes de enviar a GPT."""

    def test_formatea_lista(self):
        texto = _formatear_productos(PRODUCTOS_EJEMPLO)
        assert "iPhone 15 128GB Nuevo" in texto
        assert "Producto 1" in texto
        assert "Producto 2" in texto

    def test_formatea_precios(self):
        texto = _formatear_productos(PRODUCTOS_EJEMPLO)
        assert "$" in texto

    def test_lista_vacia(self):
        texto = _formatear_productos([])
        assert texto == ""


class TestAnalizarProductos:
    """Tests del análisis completo con mock de OpenAI."""

    @pytest.mark.asyncio
    async def test_analisis_sin_productos_lanza_error(self):
        with pytest.raises(ValueError, match="No hay productos"):
            await analizar_productos("iphone", [])

    @pytest.mark.asyncio
    async def test_analisis_retorna_resultado(self):
        """Verifica que analizar_productos() retorna un ResultadoAnalisis válido."""
        # Mockear la cadena completa (prompt | llm | parser) a nivel de ainvoke
        mock_chain = AsyncMock()
        mock_chain.ainvoke.return_value = RESPUESTA_MOCK

        with patch("app.analyzer.ChatPromptTemplate") as mock_prompt_cls, \
             patch("app.analyzer.ChatOpenAI"), \
             patch("app.analyzer.JsonOutputParser"):
            # Hacer que prompt | llm | parser retorne nuestro mock_chain
            mock_prompt = mock_prompt_cls.from_messages.return_value
            mock_prompt.__or__ = lambda self, other: mock_chain
            mock_chain.__or__ = lambda self, other: mock_chain

            resultado = await analizar_productos("iPhone 15", PRODUCTOS_EJEMPLO)

        assert resultado.mejor_opcion.titulo == "iPhone 15 128GB Nuevo"
        assert resultado.mejor_opcion.es_confiable is True
        assert resultado.mejor_opcion.vale_la_pena is True
        assert resultado.total_productos == 2
        assert len(resultado.productos_analizados) == 2
        assert resultado.resumen == RESPUESTA_MOCK["resumen"]
