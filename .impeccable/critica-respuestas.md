# Respuestas a las preguntas de las críticas

Archivo del coordinador (plan del rediseño, «Cierre de cada oleada», paso 3). Las preguntas de una crítica de
impeccable no van al operador: el coordinador las contesta con `DESIGN.md`, el diseño aprobado y las decisiones
del operador, y las respuestas quedan aquí como vigentes. Cada oleada lee este archivo antes de su crítica y
añade al final las respuestas de las suyas; solo sube al operador, a través del coordinador, la pregunta que
estas fuentes no zanjan. Cada respuesta dice qué se eligió, por qué y en qué regla se apoya.

---

## `/__preview` (`frontend/src/preview/PreviewPage.tsx`), Task 3

Crítica `.impeccable/critique/2026-11-24T20-11-53Z__frontend-src-preview-previewpage-tsx.md` (28/40, cerrada).
Respondidas por el coordinador el 2026-11-24 y aplicadas en la ronda 2 de correcciones de la Task 3.

### 1. La ficha y el formulario dentro de la tarjeta «Kit v4»

- **Respuesta: (c).** Tres secciones, cada una dibujada como la dibujarán sus páginas: la lista en un panel
  desnudo (`Panel bare`), y la ficha (cabecera, pestañas y los paneles Detalles y Actividad) y el formulario
  (la sección y la barra de guardado) sobre el fondo de la página. El banco no muestra ninguna tarjeta dentro
  de otra.
- **Por qué.** El banco es la referencia que copian las ocho oleadas y el lugar donde `a11y-browser.mjs` mide
  cada pieza en píxeles. Dentro de la tarjeta, la ficha se medía sobre blanco cuando las páginas la ponen sobre
  el fondo de la página, y enseñaba una composición que ninguna página debe repetir.
- **Regla.** `DESIGN.md`, «Elevation & Depth»: una sola sombra, sin escala de elevación (una tarjeta dentro de
  otra es una segunda elevación); `DESIGN.md`, «Lists», «Records (detail)» y «Forms»: cómo se dibuja cada
  patrón; el suelo de oficio de impeccable: «nested cards are always wrong».

### 2. Los estados de fallo y de bloqueo de la barra de guardado

- **Respuesta: (c).** `SaveBar` acepta un nombre de región opcional (`label`). El banco dibuja la barra con un
  cambio, la que falló, la que no puede guardar y la de «Guardado.», una debajo de otra, y cada región tiene su
  nombre, así que harmony y `a11y-browser.mjs` las miden sin romper `landmark-unique`.
- **Por qué.** Son los estados que más importan cuando un guardado sale mal (la línea de fallo en tinta
  crítica, el motivo en tinta atenuada) y ninguna pasada los veía. Una página dibuja una sola barra y no la
  nombra: el nombre por defecto sigue siendo «Cambios sin guardar».
- **Regla.** `PRODUCT.md`, «Accessibility & Inclusion»: WCAG 2.2 AA medido, no afirmado; `DESIGN.md`, «Forms»:
  una sola barra para toda la consola; axe `landmark-unique`.

### 3. «Cerrar sesión» en el marco del portal incrustado

- **Respuesta: (c).** Oculto cuando `embedded`. La oleada O2 lo conserva cuando repinte el portal.
- **Por qué.** Un marco incrustado es una imagen del marco, sin sesión que cerrar. Un botón activo cerraría la
  sesión de quien desarrolla, y uno desactivado sería un control muerto.
- **Regla.** `DESIGN.md`, «Don't ship a dead control»; `PRODUCT.md`, principio 2: «Every control leads somewhere
  real».
