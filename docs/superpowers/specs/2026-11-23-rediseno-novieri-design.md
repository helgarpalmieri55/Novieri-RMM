# Rediseño de la consola: la organización de Atera con la marca Novieri — diseño

**Estado:** aprobado por el operador (Helgar Palmieri, Novieri) el 2026-11-23, como
diseño conjunto de los proyectos 3 y 4 ("Aprobado tal cual"). La dirección de
marca la eligió aparte, entre tres propuestas (§3.1).

Es el **proyecto 3 de 4** del plan acordado el 2026-10-23
(`2026-10-23-msp-clientes-design.md`): «Rediseño de la interfaz al estilo Atera,
con la skill impeccable». El proyecto 4 (VPS y cambio de marca) tiene su propio
diseño, `2026-11-23-vps-y-marca-novieri-design.md`.

**Decisiones del operador (2026-11-23):**

| # | Pregunta | Respuesta |
|---|---|---|
| A1 | ¿Qué se toma de Atera? | **Su organización, con la marca de Novieri.** Se toman de Atera el menú, las páginas por cliente, las listas con filtros, las fichas con pestañas y el menú «Nuevo». Los colores y las formas son de Novieri, para no parecer un clon de un producto que también se vende. |
| A2 | ¿Hay identidad visual? | **No; la propone el coordinador**: logotipo, símbolo, favicon y paleta, enseñados antes de aplicarlos. |
| A3 | ¿Cómo se llama el producto? | **Novieri RMM**: «Novieri» en la barra; «Página · Novieri RMM» en el título de la pestaña y «Novieri RMM» en los correos. |
| A4 | ¿Hasta dónde llega? | **Las 86 páginas, una a una**, no solo el armazón y el kit. |

Las propuso el coordinador y el operador las aprobó con el diseño:

- **Sin campana de notificaciones.** La consola no tiene un buzón de avisos
  dentro de la aplicación: el módulo de notificaciones envía correos y webhooks
  y su pantalla es de configuración. Una campana sin destino rompe la regla «no
  dead controls» de `DESIGN.md`. El indicador de alertas cumple el papel del
  triángulo de Atera.
- **El selector de cliente se queda en la barra superior**, aunque Atera no lo
  tenga. Decide lo que la sesión ve, por RLS (invariante 5a). Además se añade lo
  que Atera sí tiene: la ficha 360 del cliente con pestañas y un chip «Cliente»
  en las listas.
- **Las dos líneas en paralelo.** Este proyecto toca `frontend/` y `DESIGN.md`;
  el 4 toca `backend/`, `agent/`, `deploy/` y la CI. Llegan a `main` en una sola
  PR.

Las referencias de Atera están en `docs/design/referencias-atera/`: 25 capturas y
el recorrido `Atera_recorrido_menus_UI_UX.md`. Su propia conclusión (líneas
797–817) es la nuestra: **copiar la estructura y competir en coherencia**. Eso
significa un solo modelo de guardado, todo con URL, títulos de pestaña únicos y
estados vacíos que dicen la causa real.

---

## 1. La organización

### 1.1 La barra lateral

- **Es oscura, sin títulos de grupo**, como la de Atera, y conserva su orden.
  Las etiquetas aparecen en español; el catálogo inglés se mantiene.
  1. Inicio (`/dashboard`)
  2. Tickets (`/service-desk`)
  3. Clientes (`/clients`, que sale de Administración)
  4. Equipos (`/devices`)
  5. Alertas (`/health`)
  6. Parches (`/patch`)
  7. Software (`/deploy`)
  8. Copias de seguridad (`/backup`)
  9. Sesiones remotas (`/remote`)
  10. Activos: Licencias, Suscripciones y Direcciones IP
  11. Documentación (`/docs`)
  12. Informes (`/reports`)
  13. Más: Cambios, Pruebas de continuidad y Presupuesto, solo si están activos
      para el cliente elegido
- **Abajo, anclados:**
  - Asistente (IA);
  - Administración, que sustituye la barra por un acordeón de secciones con un
    botón de volver y una búsqueda, como el panel de administración de Atera.
    Las secciones y sus páginas son las de hoy, salvo Clientes, que pasa a la
    barra principal.
- **Los permisos no cambian.** Cada entrada conserva su código de permiso, y un
  técnico ve solo lo que puede abrir.
- **Plegada**, la barra queda solo con iconos. Por debajo de 1024 px pasa a
  cajón, como hoy.

### 1.2 La barra superior

- El menú **«Nuevo»**. Cada entrada es un destino real y se muestra solo con su
  permiso:
  - Ticket;
  - Cliente;
  - Contacto;
  - Equipo (que lleva a instalar el agente);
  - Documento;
  - Cambio, si el módulo está activo.

  Así no queda ningún control muerto.
- La **búsqueda global**, la paleta de hoy con atajo `/`, ensanchada.
- El **selector de cliente**, restilizado. Su comportamiento no cambia: guarda
  la elección, envía `X-Client-Id` y vacía la caché al cambiar.
- El **indicador de alertas** y el de estado de la plataforma.
- La **cuenta**.
- Atera tiene «Instalar agente» como botón propio; aquí entra en el menú
  «Nuevo» y en la página de Equipos.

### 1.3 Las páginas por cliente

- **La ficha 360 de un cliente** reúne sus datos en pestañas con URL propia.
  Lleva Resumen, Sedes, Contactos, Equipos, Tickets, Alertas, Activos y
  Módulos, que sustituyen las secciones de la ficha de hoy.
- **Las pestañas son de una línea.** Atera parte las suyas en dos filas, y su
  propio recorrido lo señala como un defecto.
- **Las listas que pertenecen a un cliente** llevan un chip «Cliente» cuando el
  selector está en «Todos mis clientes».

## 2. Los patrones

Hay un kit, reutilizado en todas las páginas. Ninguna página dibuja su propia
versión de uno de estos patrones.

- **Lista:**
  - título y descripción corta, con la acción principal a la derecha;
  - búsqueda y chips de filtro con su conteo. Los filtros viven en la URL, así
    que una vista filtrada se comparte con un enlace;
  - «Mostrando x de y»;
  - selección múltiple y barra de acciones en lote donde ya existen acciones en
    lote;
  - el estado vacío dice la causa real en una frase: nada todavía, o nada con
    estos filtros.
- **Ficha:**
  - cabecera con estado, contexto (cliente y sede) y acciones;
  - tarjetas de resumen;
  - pestañas que cambian la URL. Así una pestaña se recarga y se enlaza, y
    Atera no lo permite;
  - la ficha de equipo sigue a la de Atera (captura «atera 8»): resumen de
    alertas y de parches arriba, información a la izquierda, métricas y
    actividad a la derecha.
- **Formulario:**
  - página completa, con volver y título;
  - secciones plegables;
  - una sola barra fija de «Cambios sin guardar · Descartar · Guardar» para
    toda la consola. El recorrido cuenta seis formas de guardar distintas en
    Atera; aquí hay una.
- **Ya existentes, que se restilizan:** tablas, campos con ayuda
  (`2026-09-10-settings-ui-rework-design.md`), avisos, diálogos y cajones.

## 3. El sistema visual

### 3.1 La marca

La dirección elegida por el operador se transcribe a `DESIGN.md` y a
`frontend/src/index.css`. Sus valores exactos (tokens, ratios de contraste,
logotipo y símbolo en SVG) están en `docs/design/marca/`, que este proyecto añade.

### 3.2 Lo que se conserva de `DESIGN.md`

`DESIGN.md` pasa a la versión 4, «Novieri».

**Se cambian:**
- la capa de marca: el color de la barra y el acento;
- el logotipo y el favicon. Hoy no hay favicon (`data:,`) y solo hay un escudo
  dibujado.

**Se conservan:**
- un solo color de acento y cinco severidades;
- el contraste AA, medido por `contrast.test`;
- la escala de tipos;
- los tokens como única fuente, guardados por `tokens.test`, `radius.test` y
  `jsx-tokens.test`;
- solo tema claro (`no-dark-variants.test`);
- la regla de no tener controles muertos;
- la puerta de armonía (*harmony gate*) en inglés y en español.

Una regla que la marca obligue a cambiar se cambia primero en `DESIGN.md`, con
su razón, y después en los tokens y en las pruebas.

## 4. Las 86 páginas

- **El orden de trabajo:**
  1. Se fijan la marca y los tokens.
  2. Se hacen el kit y el armazón: barra lateral, barra superior, «Nuevo»,
     selector, logotipo, favicon y título.
  3. Las páginas se trabajan **en oleadas por grupo de módulos**, unas ocho
     oleadas de unas once páginas.
- **Cada página:**
  - se adapta a su patrón (lista, ficha o formulario) y al kit;
  - pasa por la crítica y el pulido de la skill impeccable;
  - se mira en sus capturas en español y en inglés.
- **El portal del usuario final**, las pantallas de acceso y el consentimiento
  OAuth reciben la misma marca dentro de su propio marco.
- **Lo que no cambia:**
  - el comportamiento: rutas, permisos, llamadas a la API y textos de error;
  - las rutas existentes. Si una se mueve, deja una redirección.

## 5. Las pruebas

- **Siguen en verde** el job `frontend` (vitest, `tsc -b` y build), `a11y` y la
  puerta de armonía en los dos idiomas.
- **Las pruebas que fijan la navegación** (`nav.test`, `routes-covered.test`,
  las del armazón y las del selector) se actualizan a propósito, con la nueva
  organización. No se borran.
- **Pruebas nuevas:**
  - el menú «Nuevo», que solo muestra destinos con permiso y cada uno lleva a
    su ruta;
  - las pestañas con URL;
  - la barra de guardado, que una página con cambios avisa antes de salir;
  - los filtros en la URL;
  - el título de la pestaña («Página · Novieri RMM»);
  - el nombre «ITMS» ya no aparece en `frontend/src` (la misma técnica que
    `test_no_vendor_names_backend.py`).
- **Cada oleada de páginas** tiene su revisión, y al final hay una revisión de
  toda la rama.

## 6. Fuera de alcance

- Un tema oscuro.
- Una campana de notificaciones: no hay buzón detrás.
- Vistas guardadas en el servidor. Los filtros en la URL cubren compartir una
  vista.
- Cambios de backend, salvo los que una pantalla necesite y no existan. Esos
  se nombran en el plan.
