# hermes-skills-helper

Capa de **medición** para la auto-mejora de los **skills** de un arnés de agente:
encuentra fallos que se repiten entre sesiones, propone **un** cambio mínimo en un
skill y **califica si ese cambio sirvió**.

El mecanismo no conversa con el usuario ni ejecuta la tarea del turno. Corre al cierre
de la sesión (`on_session_end`) y trabaja sobre evidencia ya escrita en disco.

Es un **plugin de Hermes Agent** — no un skill, no un gancho de shell.

> **Estado: diseño.** Este repositorio contiene el diseño y su justificación. El código
> se escribe etapa por etapa, con tests, y no existe todavía.

## Por qué desde cero

El diseño de este plugin proviene de un proyecto anterior que **no funciona sin
parchear el núcleo de Hermes**. Su propia documentación lo declara:

> *the plugin refuses to run unbound: without the patch, `refine_run` stops at
> `llm_invocation_unavailable` and does nothing*

El parche son **+977 / −14 líneas en 8 archivos del núcleo**:

```
agent/plugin_llm.py           187      agent/turn_context.py            15
agent/auxiliary_client.py     144      tui_gateway/methods_tools.py     11
hermes_cli/plugins.py         106      agent/turn_facade.py             10
gateway/run_inbound.py         37      cli.py                            9
```

Eso choca con una política explícita de esta instalación:

> **Si algo se puede resolver con un plugin, no se toca el motor.**

No es una preferencia estética. Un parche al núcleo tiene tres costos concretos:

1. **`hermes update` reemplaza esos archivos** y el parche desaparece sin aviso.
2. **Hay que rehacerlo en cada versión.** El proyecto original rebaseó siete veces en
   seis semanas (v0.21.0 → 9.10 → 9.14 → 9.16 → 9.23).
3. **Un rebase mal verificado rompe en silencio.** Ya ocurrió en ese proyecto: un
   rebase "sin conflictos" dejó todos los comandos de plugin del gateway inoperantes,
   tapados por un `except` del propio gateway.

Este repositorio es una **reimplementación independiente escrita desde cero**. La razón
no es licencia ni desacuerdo con el autor: es que **la misma funcionalidad se logra con
APIs que el arnés ya expone**.

| Etapa | Proyecto original | Este plugin |
|:---|:---|:---|
| 1. Recurrencia | Python puro · lectura `ro` | igual |
| 2. Techo diario | SQLite propio | igual |
| 3. Propuesta | API que el host no tiene → **parche al núcleo** | `ctx.llm.complete_structured` |
| 4. Journal / rollback | Python puro | igual |
| 5. ¿Sirvió? | Determinista | igual |

**Solo una de las cinco etapas necesitaba el parche.** Esa es la que se reemplaza, y con
ella desaparece la dependencia del núcleo.

## Punto de partida y créditos

El diseño —las cinco etapas, su criterio de verificación y los límites duros— proviene
de **Refine Cycle for Hermes Agent**, de **Taras Boiko** (`Bergschloss`), publicado
bajo licencia MIT.

**Este repositorio no es un fork, ni un derivado, ni su sucesor.** Es un proyecto
independiente: no hay relación de git con el original, no hay upstream que seguir, no
hay merge pendiente, y no se copia código de él. Se reconoce el **diseño** como punto
de partida; el código es propio y se escribe aquí.

De ahí se siguen tres cosas concretas:

- **Su proyecto sigue siendo suyo.** Vive en su repositorio y lo mantiene él. Nada de
  este trabajo pretende reemplazarlo ni continuarlo.
- **Nuestro trabajo va contra nuestro repositorio** — `maurorosero/hermes-skills-helper`.
  El original no recibe nuestros commits ni nuestros push.
- **No se copia código de él.** La licencia MIT no impone obligaciones sobre una
  reimplementación independiente; el crédito es una decisión de atribución, no una
  exigencia legal.

Si este trabajo resulta útil, el mérito del enfoque es de él.

## Las cinco etapas

```
1. RECURRENCIA   ¿este fallo se repite?          determinista
                 fingerprint del error + umbral; si no se repite → STOP

2. TECHO         ¿queda presupuesto hoy?         determinista
                 ≤ 3 cambios/día · ≤ 30 llamadas/día

3. PROPUESTA     un cambio mínimo, estructurado  MODELO  ← ctx.llm
                 el skill afectado + justificación + resultado esperado

4. JOURNAL       registro + rollback             determinista
                 todo cambio es reversible con un comando

5. ¿SIRVIÓ?      working / unused / unreliable   determinista
                 el cambio sobrevivió, se usó, y el error no volvió
```

El detalle de cada etapa, con las APIs del arnés que usa, está en
[`docs/DESIGN.md`](docs/DESIGN.md).

## Alcance

El dominio es **skills**, y solo skills: es el único target cuya señal de uso permite
calificar si un cambio sirvió.

- **No escribe en `MEMORY.md` ni en `USER.md`.** Solo los lee, para saber cuánto espacio
  libre queda y no proponer algo que el tope del host vaya a rechazar.
- **No toca la capa de memoria holográfica.** Es un proyecto aparte.
- **No tiene un target `prompt`.** El proyecto de origen lo incluía; su señal de uso no
  existe, así que la etapa 5 no podría calificarlo.

**Principio de diseño:** un solo punto del pipeline usa un modelo. Los otros cuatro son
comparaciones reproducibles — hashes, conteos y strings—. Poner un modelo donde alcanza
una comparación cambia una verificación reproducible por una opinión.

## Instalación

Todavía no aplica: no hay código. Cuando lo haya:

```bash
hermes plugins install maurorosero/hermes-skills-helper
hermes plugins enable hermes-skills-helper
```

## Licencia

MIT © 2026 Mauro Rosero. Ver [`LICENSE`](LICENSE).
