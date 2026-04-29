"""
Tests de los endpoints FastAPI.
Usa TestClient de httpx para simular requests HTTP.
"""

import pytest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from app.main import app, _verificar_rate_limit, rate_limit_registro


@pytest.fixture
def client():
    """Cliente de test que no inicia Celery. Simula requests HTTP sin iniciar un servidor real."""
    with patch("app.main._iniciar_worker_celery"):
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def limpiar_rate_limit():
    """Limpia el registro de rate limiting entre tests."""
    rate_limit_registro.clear()
    yield
    rate_limit_registro.clear()





class TestHealth:
    """Tests del endpoint de salud."""

    def test_health_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestHome:
    """Tests de la página principal."""

    def test_home_renderiza(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "MercadoLibre Agent" in response.text
        assert "htmx" in response.text


class TestBuscar:
    """Tests del endpoint de búsqueda."""

    def test_query_vacio(self, client):
        response = client.post("/buscar", data={"query": ""})
        assert response.status_code == 200
        assert "error-msg" in response.text

    def test_query_muy_corto(self, client):
        response = client.post("/buscar", data={"query": "ab"})
        assert response.status_code == 200
        assert "muy corto" in response.text

    def test_query_muy_largo(self, client):
        response = client.post("/buscar", data={"query": "a" * 101})
        assert response.status_code == 200
        assert "muy largo" in response.text

    @patch("app.main.buscar_y_analizar")
    @patch("app.main.redis")
    def test_busqueda_inicia_tarea(self, mock_redis, mock_task, client):
        """Verifica que una búsqueda válida crea una tarea Celery."""
        # Mock de Redis (sin cache)
        mock_r = MagicMock()
        mock_r.get.return_value = None
        mock_redis.from_url.return_value = mock_r

        # Mock de Celery
        mock_result = MagicMock()
        mock_result.id = "test-task-123"
        mock_task.delay.return_value = mock_result

        response = client.post("/buscar", data={"query": "iPhone 15"})
        assert response.status_code == 200
        assert "test-task-123" in response.text
        assert "hx-get" in response.text  # Polling HTMX configurado


class TestRateLimit:
    """Tests del rate limiting."""

    def test_permite_primera_busqueda(self):
        assert _verificar_rate_limit("192.168.1.1") is True

    def test_bloquea_exceso(self):
        ip = "10.0.0.1"
        # Simular 5 búsquedas (el límite por defecto)
        for _ in range(5):
            _verificar_rate_limit(ip)
        # La sexta debe ser rechazada
        assert _verificar_rate_limit(ip) is False
