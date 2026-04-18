"""
Modelos Pydantic para validar datos en todo el proyecto.
Define las estructuras de Producto, Análisis y Respuesta.
"""

from pydantic import BaseModel, Field


class Producto(BaseModel):
    """Producto extraído de MercadoLibre."""

    titulo: str
    precio: float
    envio: str = "No especificado"
    es_tienda_oficial: bool = False
    calificacion_producto: str = "Sin calificación"
    destacado: str = ""
    url: str


class AnalisisProducto(BaseModel):
    """Análisis generado por GPT-4o mini para un producto."""

    titulo: str
    precio: float
    url: str
    es_confiable: bool = Field(description="Si el vendedor parece confiable")
    razon_confianza: str = Field(description="Por qué es o no confiable")
    vale_la_pena: bool = Field(description="Si vale la pena comprarlo")


class ResultadoAnalisis(BaseModel):
    """Resultado completo del análisis de una búsqueda."""

    query: str
    mejor_opcion: AnalisisProducto
    resumen: str = Field(description="Resumen general de la búsqueda")
    total_productos: int
    productos_analizados: list[AnalisisProducto]


