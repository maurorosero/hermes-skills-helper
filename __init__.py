"""hermes-skills-helper — punto de entrada del plugin.

Estado: **las cinco etapas del pipeline están implementadas** como biblioteca
determinista, salvo la etapa 3 que es la única con modelo (ver ``docs/DESIGN.md``).

```
5. ¿SIRVIÓ?      effect/       tres chequeos deterministas + veredicto
1. RECURRENCIA   recurrence.py + trajectory.py   el filtro de patrones
4. JOURNAL       journal.py    registro append-only + rollback verificado
2. TECHO         budget.py     techos diarios, con la carrera cerrada
3. PROPUESTA     proposal.py   la llamada al modelo y su verificación
```

Por qué todavía no se declara ningún hook ni tool
-------------------------------------------------
``plugin.yaml`` declara únicamente lo que el plugin **registra de verdad**. Un
hook declarado y no registrado produce un aviso del validador (``manifest
declares hook ... but registration did not add it``), y declarar una capacidad
que no existe es la clase de detalle que hace desconfiar del resto.

Los módulos funcionan y están verificados (315 tests, y mediciones contra el
arnés real), pero el plugin **todavía no decide nada por su cuenta**: no hay hook
que dispare el ciclo ni tool que aplique un cambio. Falta el disparador y la vía
de aplicación — y esa vía se decide junto con el ``no_op`` del diseño, no antes.

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

    Sin registros por ahora: los módulos están implementados y verificados, pero
    el disparador del ciclo y la vía de aplicación faltan. Se registran cuando
    exista la vía completa, para que el manifest no anuncie una capacidad que
    todavía no está en pie.
    """
    return None


__all__ = ["register", "__version__"]
