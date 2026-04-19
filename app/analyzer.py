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
- Antes de responder, verificá que tu "mejor_opcion_indice" corresponda al producto con el precio más bajo entre los productos con señales de confianza comparables. Si no es así, cambiá la elección.
- NUNCA asumas ni inventes la condición (nuevo/usado) de un producto. Si no se indica condición, no menciones si es nuevo o usado.
- Sé directo y útil, como un amigo que sabe de tecnología.

Referenciá a los productos por el número de índice (el "Producto 1", "Producto 2"... que ves en la entrada). NO repitas títulos ni URLs en tu respuesta: el sistema ya los tiene.

Respondé ÚNICAMENTE con un JSON válido (sin markdown, sin backticks) con esta estructura exacta.

{{
    "mejor_opcion_indice": 1,
    "resumen": "Resumen general de 2-3 oraciones sobre la búsqueda",
    "productos_analizados": [
        {{
            "indice": 1,
            "es_confiable": true,
            "razon_confianza": "explicación breve, máximo 20 palabras",
            "vale_la_pena": true
        }}
    ]
}}

"mejor_opcion_indice" es el índice (1-based) del producto recomendado.
"productos_analizados" debe incluir a TODOS los productos de la entrada, identificados por su "indice" (1-based). No omitas ninguno."""

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
        texto += f"  Envío: {p.envio}"
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
    # response_format=json_object: OpenAI garantiza JSON parseable a nivel API,
    # evita que GPT devuelva números con separadores tipo Python (1_749_999.0) que rompen el parser.
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.2,
        api_key=settings.openai_api_key,
        max_tokens=1500,
        model_kwargs={"response_format": {"type": "json_object"}},
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
    # Reconstruir cada AnalisisProducto combinando:
    #   - datos factuales (título/precio/URL) tomados del scraper, nunca del LLM
    #   - campos de juicio (es_confiable, razon_confianza, vale_la_pena) devueltos por GPT
    # Así el LLM no puede inventar ni alterar datos, y la salida de GPT es predecible
    # en tamaño (no depende del largo de títulos/URLs).
    analisis_por_indice = {
        a["indice"]: a
        for a in resultado_json.get("productos_analizados", [])
        if isinstance(a.get("indice"), int)
    }

    def _armar_analisis(idx: int, producto: Producto) -> AnalisisProducto:
        analisis = analisis_por_indice.get(idx, {})
        return AnalisisProducto(
            titulo=producto.titulo,
            precio=producto.precio,
            url=producto.url,
            es_confiable=analisis.get("es_confiable", False),
            razon_confianza=analisis.get("razon_confianza", "No analizado"),
            vale_la_pena=analisis.get("vale_la_pena", False),
        )

    productos_analizados = [
        _armar_analisis(i, p) for i, p in enumerate(productos_ordenados, 1)
    ]

    # Validar el índice de la mejor opción; fallback al producto más barato si es inválido
    indice_mejor = resultado_json.get("mejor_opcion_indice")
    if not isinstance(indice_mejor, int) or not 1 <= indice_mejor <= len(productos_analizados):
        logger.warning(
            f"GPT devolvió mejor_opcion_indice inválido ({indice_mejor!r}); "
            f"fallback al producto más barato"
        )
        indice_mejor = 1
    mejor_opcion = productos_analizados[indice_mejor - 1]

    resultado = ResultadoAnalisis(
        query=query,
        mejor_opcion=mejor_opcion,
        resumen=resultado_json.get("resumen", "Sin resumen disponible"),
        total_productos=len(productos),
        productos_analizados=productos_analizados,
    )

    logger.info(f"Análisis completado. Mejor opción: {mejor_opcion.titulo} (${mejor_opcion.precio:,.0f})")

    return resultado
