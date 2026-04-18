"""
Analizador de productos con LangChain + GPT-4o mini.
Recibe productos scrapeados y genera un análisis de compra inteligente.
"""

import logging

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from app.config import get_settings
from app.models import AnalisisProducto, Producto, ResultadoAnalisis

logger = logging.getLogger(__name__)

# Prompt del sistema para el análisis de productos
SYSTEM_PROMPT = """Sos un experto en compras online en Argentina, especializado en MercadoLibre.
Tu trabajo es analizar una lista de productos y dar una recomendación de compra clara.

Los productos que te paso ya fueron filtrados por el buscador de MercadoLibre como resultados relevantes para la búsqueda del usuario. NO descartes productos por diferencias de nombre, variantes de título o presentación — asumí que TODOS son candidatos válidos para lo que el usuario busca. La lista te llega ordenada del más barato al más caro.

Para cada producto evaluá (de mayor a menor):
1. Si el precio es competitivo comparado con los demás resultados
2. Si el vendedor es confiable, basándote en las señales verificadas (Tienda oficial, tipo de envío).
3. La calificación del producto (reseñas), pero sin sobrevalorarla.

IMPORTANTE sobre el campo "Calificación del producto":
- Corresponde a las reseñas del PRODUCTO, no del vendedor.
- "Sin calificación" significa que el producto todavía no tiene reseñas — NO que el vendedor sea poco confiable.
- No uses la ausencia de reseñas como razón principal para desconfiar de un vendedor.

Reglas de confiabilidad (en orden de importancia):
- "Tienda oficial: Sí" es una señal FUERTE de confianza: es un badge verificado por MercadoLibre, no manipulable por el vendedor.
- "Envío gratis" es señal positiva pero más débil que la anterior.
- La ausencia de Tienda oficial es NEUTRAL, no negativa: muchos vendedores legítimos no tienen ese badge.
- Productos marcados como "MÁS VENDIDO" tienen preferencia.
- Precios muy por debajo del promedio pueden ser sospechosos; precios coherentes con el resto del listado indican oferta legítima.
- Cuando dos productos tienen señales de confiabilidad comparables (mismo estado de Tienda oficial, mismo tier de calificación), SIEMPRE gana el de menor precio, sin importar el orden en que aparezcan ni diferencias menores en el título.
- Antes de responder, verificá que tu "mejor_opcion" tenga efectivamente el precio más bajo entre los productos con señales de confianza comparables. Si no es así, cambiá la elección.
- NUNCA asumas ni inventes la condición (nuevo/usado) de un producto. Si no se indica condición, no menciones si es nuevo o usado.
- Sé directo y útil, como un amigo que sabe de tecnología.

Respondé ÚNICAMENTE con un JSON válido (sin markdown, sin backticks) con esta estructura exacta.

{{
    "mejor_opcion": {{
        "titulo": "...",
        "precio": 0.0,
        "url": "...",
        "es_confiable": true/false,
        "razon_confianza": "...",
        "vale_la_pena": true/false
    }},
    "resumen": "Resumen general de 2-3 oraciones sobre la búsqueda",
    "productos_analizados": [
        {{
            "titulo": "...",
            "precio": 0.0,
            "url": "...",
            "es_confiable": true/false,
            "razon_confianza": "...",
            "vale_la_pena": true/false
        }}
    ]
}}"""

HUMAN_PROMPT = """Analizá estos {total} productos de MercadoLibre Argentina para la búsqueda "{query}":

{productos_texto}

Recordá: respondé SOLO con JSON válido, sin texto adicional."""


def _formatear_productos(productos: list[Producto]) -> str:
    """Formatea la lista de productos como texto para el prompt."""
    lineas = []
    for i, p in enumerate(productos, 1):
        texto = (
            f"Producto {i}:\n"
            f"  Título: {p.titulo}\n"
            f"  Precio: ${p.precio:,.0f}\n"
            f"  Tienda oficial: {'Sí' if p.es_tienda_oficial else 'No'}\n"
            f"  Calificación del producto: {p.calificacion_producto}\n"
        )
        if p.destacado:
            texto += f"  Destacado: {p.destacado}\n"
        texto += (
            f"  Envío: {p.envio}\n"
            f"  URL: {p.url}"
        )
        lineas.append(texto)
    return "\n\n".join(lineas)



# tasks.py pasa la query y la lista de productos
async def analizar_productos(query: str, productos: list[Producto]) -> ResultadoAnalisis:
    

    # 1. Validar entrada
    if not productos:
        raise ValueError("No hay productos para analizar")

    
    # 2. Preparar datos — ordenados por precio ascendente para eliminar sesgo posicional del LLM
    productos_ordenados = sorted(productos, key=lambda p: p.precio)
    productos_texto = _formatear_productos(productos_ordenados)
    logger.info(f"Enviando {len(productos_ordenados)} productos a GPT-4o mini para análisis")


    # 3. Armar pipeline LangChain
    settings = get_settings()

    # Construir el prompt con LangChain (el molde del mensaje)
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", HUMAN_PROMPT),
    ])


    # Inicializar modelo con temperatura baja para respuestas consistentes (la conexión a GPT)
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.2,
        api_key=settings.openai_api_key,
        max_tokens=3000,
    )


    # Convierte la respuesta en datos
    parser = JsonOutputParser()

    
    # Crear cadena: prompt → LLM → parser JSON
    chain = prompt | llm | parser



    # 4. Ejecutar
    # cadena completa guarda el resultado en resultado_json
    resultado_json = await chain.ainvoke({

        # datos concretos que se insertan en HUMAN_PROMPT
        "total": len(productos_ordenados),
        "query": query,
        "productos_texto": productos_texto,
    })


    # 5. Parsear respuesta

    # Parsear a modelos Pydantic
    # GPT no siempre incluye los campos de análisis — asignar defaults si faltan
    campos_analisis_defaults = {
        "es_confiable": False,
        "razon_confianza": "No analizado",
        "vale_la_pena": False,
    }




    # convertir el dict crudo que devolvió GPT en un objeto Pydantic AnalisisProducto de todos los productos
    # El spread (**) combina ambos diccionarios y en caso de claves duplicadas, 
    # el segundo (data) gana, asegurando que los valores de GPT se mantengan aunque falten algunos campos.
    def _parsear_producto(data: dict) -> AnalisisProducto:
        return AnalisisProducto(**{**campos_analisis_defaults, **data})

    # producto mejor opción: el dict que GPT puso en "mejor_opcion", convertido a AnalisisProducto
    mejor_opcion = _parsear_producto(resultado_json["mejor_opcion"])

    # lista de productos analizados: cada dict en "productos_analizados" convertido a AnalisisProducto
    productos_analizados = [
        _parsear_producto(p) for p in resultado_json.get("productos_analizados", [])
    ]

    # resultado final del análisis
    resultado = ResultadoAnalisis(
        query=query,
        mejor_opcion=mejor_opcion,
        resumen=resultado_json.get("resumen", "Sin resumen disponible"),
        total_productos=len(productos),
        productos_analizados=productos_analizados,
    )

    logger.info(f"Análisis completado. Mejor opción: {mejor_opcion.titulo} (${mejor_opcion.precio:,.0f})")

    return resultado
