"""
Tests del módulo de tareas Celery.
Verifica la orquestación del flujo scraping → análisis.
"""

from unittest.mock import patch, AsyncMock

from app.tasks import _ejecutar_async


class TestEjecutarAsync:
    """Tests del helper para ejecutar coroutines en contexto sync."""

    def test_ejecuta_coroutine(self):
        """Verifica que puede ejecutar una coroutine async."""
        async def suma():
            return 2 + 2

        resultado = _ejecutar_async(suma())
        assert resultado == 4

    def test_ejecuta_con_await(self):
        """Verifica ejecución con await interno."""
        import asyncio

        async def con_delay():
            await asyncio.sleep(0.01)
            return "ok"

        resultado = _ejecutar_async(con_delay())
        assert resultado == "ok"


class TestBuscarYAnalizar:
    """Tests de la tarea principal (con mocks)."""

    # @patch reemplaza get_settings con un objeto falso (MagicMock) evita que el import de app.tasks falle por falta de variables de entorno
    @patch("app.tasks.get_settings") 
    def test_tarea_registrada(self, mock_settings):
        """Verifica que la tarea está registrada en Celery."""
        from app.tasks import celery_app

        assert "buscar_y_analizar" in celery_app.tasks
