"""hermes-skills-helper — punto de entrada del plugin.

Estado: **andamio**. Este repositorio contiene el diseño, no la lógica todavía
(ver ``docs/DESIGN.md``). El módulo existe para que el plugin se pueda instalar y
validar con las herramientas del arnés desde el primer día, y para que la
ausencia de lógica sea explícita en lugar de un error de carga.

Las etapas se implementan en el orden fijado por el diseño, empezando por la
medición (etapa 5, *¿sirvió?*): sin poder calificar un cambio, aplicar cambios
es exactamente el problema que este proyecto viene a resolver.

Por qué no se declara ningún hook todavía
-----------------------------------------
``plugin.yaml`` declara únicamente lo que el plugin **registra de verdad**. Un
hook declarado y no registrado produce un aviso del validador (``manifest
declares hook ... but registration did not add it``), y declarar una capacidad
que no existe es la clase de detalle que hace desconfiar del resto. Cuando la
etapa 5 esté implementada, ``on_session_end`` se registra aquí y se declara en
el manifest en el mismo commit.

Restricción de diseño
---------------------
El núcleo de Hermes **no se toca**. Todo lo que sigue usa APIs que el arnés ya
expone (``on_session_end``, ``ctx.llm.complete_structured``). Si una etapa
necesitara algo que el host no ofrece, se rediseña — no se parchea el motor.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    """Punto de entrada que el arnés invoca al cargar el plugin.

    Sin registros por ahora: el diseño precede al código. Se mantiene la firma
    completa para que el andamio sea el real y no haya sorpresas al cargar.
    """
    return None


__all__ = ["register", "__version__"]
