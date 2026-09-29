# Diseño — hermes-learning-helper

Documento de diseño. **No hay código todavía.** Cada etapa se implementa y se prueba
por separado antes de pasar a la siguiente.

## Restricción que ordena todo el diseño

> El núcleo de Hermes no se toca.

Todo lo que sigue usa **únicamente** APIs que el arnés ya expone. Si una etapa necesita
algo que el host no tiene, la etapa se rediseña — no se parchea el motor.

## APIs del arnés utilizadas

Verificadas contra el host en uso (Hermes 0.21.4):

| Necesidad | API | Estado |
|:---|:---|:---|
| Punto de entrada al cerrar sesión | hook `on_session_end` | existe en `VALID_HOOKS` |
| Llamada al modelo activo del usuario | `ctx.llm` → `agent.plugin_llm.PluginLlm` | existe |
| Salida estructurada del modelo | `ctx.llm.complete_structured(...)` | existe |
| Leer trayectoria pasada | SQLite del arnés, abierto `mode=ro` | existe |

`PluginLlm` se documenta en el propio host como *"completions on the user's active
model/auth"*. Es exactamente lo que la etapa 3 necesita, y **no requiere el parche**.

## Etapa 1 — Recurrencia (determinista)

**Pregunta:** ¿este fallo se repite, o pasó una sola vez?

Un error que ocurrió una vez no justifica un cambio permanente en las instrucciones del
agente. Solo se propone un cambio sobre fallos **recurrentes**.

```
leer errores de la trayectoria (state.db, mode=ro)
    ↓
normalizar → fingerprint (no el texto crudo: el mismo fallo se escribe distinto)
    ↓
¿aparece ≥ N veces, o en ≥ 2 sesiones distintas?
    ├── no  → STOP (sin propuesta, sin costo de modelo)
    └── sí  → evidencia para la etapa 3
```

**Por qué primero:** es el filtro más barato y el que evita el problema que motivó este
proyecto — un revisor que propone cambios sin control. El filtro corre antes de gastar
una sola llamada al modelo.

**Criterio de cierre:** reproducir la detección sobre trayectoria real y comprobar que
el umbral descarta el ruido de un solo evento.

## Etapa 2 — Techo (determinista)

**Pregunta:** ¿queda presupuesto hoy?

Límites duros, tomados del proyecto original porque son sensatos:

```
≤ 3 cambios por día       (radio de impacto)
≤ 30 llamadas por día     (costo)
```

Se evalúan **antes** de llamar al modelo. Si no hay presupuesto, la etapa termina sin
consumir nada.

**Advertencia de concurrencia (heredada del original):** el contador es
leer-y-actuar, y el registro es leer-modificar-escribir. Bajo canales concurrentes hay
ventana de carrera. Debe resolverse con bloqueo, no ignorarse.

**Criterio de cierre:** con el techo agotado, ninguna llamada al modelo ocurre.

## Etapa 3 — Propuesta (la única con modelo)

**Pregunta:** ¿cuál es el cambio mínimo que evita que este fallo se repita?

Entrada: la evidencia de la etapa 1. Salida: **una** propuesta, validada contra un
esquema:

```
action      : patch | create | no_op
target      : skill | memoria | prompt
nombre      : del skill o de la memoria afectada
contenido   : el cambio propuesto, mínimo
justificación
resultado esperado (cómo se sabrá si sirvió)
```

Se implementa con `ctx.llm.complete_structured(...)`, que devuelve salida validada
contra esquema. **Es la pieza que reemplaza al parche**: el proyecto original inventó
una API (`PluginInvocationRoute`) para enrutar la llamada dentro del turno; el host ya
ofrece una vía soportada.

**`no_op` es una salida legítima.** Poder responder "no corresponde ningún cambio" es
lo que separa un revisor de un generador de parches.

**Criterio de cierre:** una propuesta válida contra el esquema, una llamada, y
verificación de que el techo se respetó.

## Etapa 4 — Journal y rollback (determinista)

Todo cambio aplicado queda registrado y es **reversible**.

```
journal: quién, qué, cuándo, contenido anterior (hash), contenido nuevo (hash)
rollback: un comando revierte el último cambio
```

**Requisito duro:** antes de escribir se guarda el estado anterior. Sin rollback
verificado no hay aplicación de cambios — se propone y se detiene.

**Criterio de cierre:** aplicar y revertir, comprobando que el contenido vuelve byte a
byte al estado previo.

## Etapa 5 — ¿Sirvió? (determinista)

**Pregunta:** el cambio se hizo. ¿Funcionó?

Esta etapa **no lleva modelo**, y ese es un punto de diseño, no una omisión. Tres
comparaciones reproducibles:

```
1. ¿el cambio sobrevivió?     sha256 del contenido que escribimos
                              vs sha256 de lo que hay ahora
                              → si el agente lo editó después, no nos acreditamos

2. ¿se usó?                   conteo de usos sobre el registro del arnés
                              desde el momento del cambio

3. ¿el error volvió?          fingerprint del error registrado
                              vs fingerprint de los errores posteriores
```

Veredicto: `working` / `unused` / `unreliable` / `too_new`.

**Por qué determinista:** la pregunta es *"¿el cambio sobrevivió y se usó?"* — no
*"¿qué opinás del cambio?"*. La primera se responde con hashes y conteos; la segunda con
una opinión. Cambiar la primera por la segunda degrada la medición.

El chequeo 1 existe porque **el propio agente edita los mismos skills**. Sin él, el
plugin se acreditaría un cambio que en realidad hizo otro. Es honestidad de medición.

**Límite honesto:** estas tres comparaciones no responden *"¿el error paró por esta
causa?"*. Responden *"el cambio sobrevivió, se usó, y el error no reapareció"*. Atribuir
causalidad requiere leer las sesiones posteriores — razonamiento sobre trayectorias, no
una comparación. Queda fuera del alcance de esta versión, y se declara.

**Criterio de cierre:** los cuatro veredictos se reproducen sobre casos construidos.

## Lo que este plugin NO hace

- **No toca el núcleo de Hermes.** Si una versión futura lo exigiera, se rediseña.
- **No aplica cambios sin poder revertirlos.**
- **No propone sobre un fallo que ocurrió una sola vez.**
- **No supera el techo diario**, aunque haya evidencia abundante.
- **No usa un modelo para decidir si un cambio sirvió.**
- **No conversa con el usuario** ni participa del turno: corre al cierre.

## Orden de implementación

```
1. Etapa 5  (¿sirvió?)      ← sin ella, no hay forma de saber si el resto sirve
2. Etapa 1  (recurrencia)
3. Etapa 4  (journal + rollback)
4. Etapa 2  (techo)
5. Etapa 3  (propuesta)     ← la única con modelo, y la última en escribirse
```

Se empieza por la medición: sin poder calificar un cambio, aplicar cambios es
exactamente el problema que este proyecto viene a resolver.

## Créditos

El diseño de las cinco etapas proviene de **Refine Cycle for Hermes Agent**, de
**Taras Boiko** (`Bergschloss`), MIT. Ver la sección de créditos en el
[`README`](../README.md).
