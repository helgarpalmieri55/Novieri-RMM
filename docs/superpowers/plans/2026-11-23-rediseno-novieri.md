# Rediseño de la consola: la organización de Atera con la marca Novieri. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** La consola pasa a llamarse Novieri RMM, adopta la marca «Cordillera» y la organización de Atera (barra lateral en su orden, «Nuevo», ficha 360 del cliente, listas, fichas y formularios de un solo kit), y las 86 páginas `*Page.tsx` se rehacen una a una sobre ese kit sin cambiar rutas, permisos, llamadas a la API ni textos de error.

**Architecture:** Primero, en serie, la base: el contexto de la skill impeccable, `DESIGN.md` v4 y los tokens de la marca (Task 1); el nombre, el logotipo, el favicon y el título de pestaña (Task 2); el kit de patrones lista / ficha / formulario (Task 3); el armazón (Task 4) y la ficha 360 del cliente (Task 5). Después, ocho oleadas de páginas por grupo de módulos (Tasks 6 a 13), sin archivos en común, que corren en paralelo de cuatro en cuatro en worktrees separadas. Cada oleada termina con la crítica y el pulido de impeccable sobre sus páginas, la puerta de armonía y axe en sus rutas en los dos idiomas y las capturas para el revisor. La Task 14 revisa la rama entera y deja el camino a `main`.

**Tech Stack:** React 19, TypeScript 7 en modo estricto, Vite 8, Tailwind v4 (CSS primero, sin `tailwind.config`), react-router-dom 7, TanStack Query 5, vitest 4 con Testing Library y jsdom, Playwright con Chromium (`scripts/harmony.mjs`, `scripts/a11y-browser.mjs`), skill impeccable 4.7.0 (CLI 0.1.16, `.claude/skills/impeccable/scripts/impeccable`).

**Diseño (vinculante):** `docs/superpowers/specs/2026-11-23-rediseno-novieri-design.md`, aprobado por el operador el 2026-11-23. **Marca:** `docs/design/marca/` (`novieri-tokens.json`, `novieri-{wordmark,symbol,favicon,lockup}.svg`; la dirección A, «Cordillera»). **Decisiones del operador:** `/home/user/Novieri-RMM/.git/sdd/decisiones-p34.md` (A1 a A4; A4: las 86 páginas, una a una; y las respuestas del operador y las resoluciones del coordinador del 2026-11-24, que este plan ya aplica). Ruta absoluta a propósito: dentro de una worktree enlazada `.git` es un archivo, no una carpeta, y `.git/sdd/…` no existe; desde cualquier checkout la misma carpeta es `"$(git rev-parse --git-common-dir)/sdd/"`. El coordinador copia en el encargo de cada oleada lo que esa oleada necesita de estos archivos. **Referencia:** `docs/design/referencias-atera/` (25 capturas y `Atera_recorrido_menus_UI_UX.md`). Cada tarea cita la sección del diseño que implementa.

---

## Global Constraints

- **Seguridad.** Las invariantes 1, 1b, 1c, 2, 2a, 3, 4, 5, 5a y 6 de `CLAUDE.md` no se tocan. La consola no protege nada: ocultar un enlace es cortesía (invariante 6). Ningún código de permiso cambia; cada entrada de la barra y del menú «Nuevo» conserva el código que su ruta exige en el servidor.
- **Territorio.** Este proyecto toca `frontend/`, `DESIGN.md`, `PRODUCT.md`, `.impeccable/` y `docs/product.md` §9 y §11. **No toca** `backend/`, `agent/`, `deploy/` ni `.github/` (son del proyecto 4). Lo que una pantalla necesitaría de ellos está en el Apéndice E.
- **Nombre.** «Novieri» en la barra lateral; «`<Página>` · Novieri RMM» en el título de la pestaña (`<Registro> · <Página> · Novieri RMM` en una ficha); «Novieri RMM» en el `<title>` de `index.html`, en las pantallas de acceso, en el portal y en el consentimiento OAuth. «ITMS» no aparece en `frontend/src` salvo los literales de protocolo de la lista de `src/test/no-itms.test.ts` (Task 2).
- **Marca, valores exactos** (`novieri-tokens.json`): riel `#101b30`, riel en hover y activo `#1b2945`, texto del riel `#a9b6cc`, atenuado `#8592ab`, marca del activo `#8fa6ff`; página `#f3f5f8`, superficie `#ffffff`, fila en hover `#f7f9fc`, borde `#e1e6ee`, borde de control `#7b8599`, texto `#111a2b`, texto secundario `#536077`; acento `#2747c9`, hover `#203cac`, presionado `#1a3190`, lavado `#e9edfc`, tinta sobre acento `#ffffff`; correcto `#17703f`/`#e5f4ea`, advertencia `#8d5a00`/`#fcf1dc`, crítico `#b8172c`/`#fcebed`, desconocido `#586274`/`#edf0f4`; información = acento. Sombra única `0 1px 2px rgba(16,27,48,.05),0 6px 20px rgba(16,27,48,.06)`.
- **Radios:** 4 px etiquetas de estado (`--r-badge`), 6 px controles y chips (`--r-ctl`), 10 px paneles (`--r-panel`); `--r-pill` (999 px) solo para la pista del interruptor y la barra de progreso; 50 % avatares y pips; 2 y 3 px celdas de calor. Los chips y las etiquetas dejan de ser píldoras.
- **Tipografía:** Inter autoalojada; la escala 22 / 15 / 13,5 / 12 / 10,5 y 26 (KPI), sin cambios; pesos 400, 500 y 600.
- **Solo tema claro** (`no-dark-variants.test.ts`). **Un acento** y cinco severidades; el color nunca es el único portador (icono en cada etiqueta). **Ningún control muerto**: sin campana, y cada entrada de «Nuevo» es un destino real.
- **Comportamiento.** Las rutas existentes no cambian. Este plan no mueve ninguna; si una oleada descubre que debe mover una, deja un `<Navigate replace>` en `App.tsx` a través del integrador (ver «Archivos compartidos»). Las llamadas a `ApiClient`, los códigos de permiso que una página comprueba y los textos de error no cambian salvo lo que este plan nombra.
- **Un solo kit.** Ninguna página dibuja su propia lista, ficha, pestañas, barra de filtros, estado vacío o barra de guardado: usa las piezas de `src/shell/listKit.tsx`, `detailKit.tsx` y `formKit.tsx` (Task 3), reexportadas por `src/shell/components.tsx`.
- **Todo con URL.** Filtros, pestañas y diálogos de alta (`?new=<tipo>`) viven en la query de la URL. La página por defecto no escribe parámetros.
- **Textos.** Todo texto nuevo pasa por `t()` con su clave en `src/i18n/en/` y `src/i18n/es/` (lo exigen `i18n/coverage.test.ts` y `catalogue.identical.test.ts`). Glosario de `src/i18n/es/index.ts`. Sin raya («—») en la interfaz, sin mayúsculas tecleadas, frase en minúscula, la voz de `docs/superpowers/specs/2026-09-09-console-copy-voice.md`: decir lo que pasó y lo que sigue. Un estado vacío es una frase con su causa real y, si existe, una acción.
- **Pruebas.** Ninguna prueba existente se borra, se salta ni se debilita. Las que fijan algo que el diseño cambia a propósito (navegación, armazón, nombre, tokens) se corrigen en la misma tarea, con un comentario que cite el diseño, y el informe de la tarea las lista. Línea base medida el 2026-11-23: `npx vitest run` → **365 archivos, 7200 pruebas, 0 fallos, ~5 min**.
- **Verificación, desde `frontend/`:**
  - una prueba: `npx vitest run <ruta>`;
  - todo: `npx vitest run`, `npm run typecheck` y `npm run build` (los tres del job `frontend` de la CI);
  - navegador: `node scripts/harmony.mjs --strict-fixtures --lang en --routes <lista>` y lo mismo con `--lang es`; `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs` (mide `/__preview`, donde vive el kit). Desde la Task 1 (Step 12) harmony **falla** cuando una ruta de la consola no dibuja el armazón (sin `.topbar`) o cuando el servidor de desarrollo responde 5xx a un archivo de la propia página (`504 (Outdated Optimize Dep)` es lo que se ve cuando dos servidores Vite comparten la caché de dependencias): antes de ese paso, una página que no se dibujó salía como `ok` (revisión previa, C1, reproducido el 2026-11-24).
- **Worktrees** (oleadas en paralelo). Cada worktree tiene **su propia carpeta `node_modules`**, hecha de enlaces a los paquetes de la del checkout principal, para que la caché de Vite (`node_modules/.vite`, `.vite-temp`) sea suya y no la de todas. **Nunca** se enlaza la carpeta entera (`ln -s …/frontend/node_modules`): con ella las ocho worktrees comparten `node_modules/.vite`, cada servidor la reescribe al arrancar (la raíz del proyecto entra en el hash de la caché) y los demás sirven `504 (Outdated Optimize Dep)` a mitad de una pasada. Las órdenes, desde la raíz del checkout principal:
  ```bash
  N=1                                   # el número de la oleada: 1 a 8
  git worktree add "../rmm-w$N" -b "p3/oleada-$N" p3/base
  mkdir "../rmm-w$N/frontend/node_modules"
  for e in /home/user/Novieri-RMM/frontend/node_modules/* /home/user/Novieri-RMM/frontend/node_modules/.bin; do
    ln -s "$e" "../rmm-w$N/frontend/node_modules/"
  done
  ls -A "../rmm-w$N/frontend/node_modules" | grep -c '^\.vite'   # 0: la caché aún no existe y será de esta worktree
  ```
  (`p3/base` es la rama donde quedan las Tasks 1 a 5; si el coordinador la llama de otra forma, la orden usa ese nombre.) Los paquetes siguen siendo los del checkout principal, que ninguna worktree escribe: `npm install` no se ejecuta en una worktree. Cada worktree usa su propio puerto para harmony y para `npx vite` (los dos scripts eligen uno libre; `npx vite` imprime el suyo). Comprobado el 2026-11-24 en copias de trabajo (`p3-fix-report.md`): con la carpeta compartida, cada servidor nuevo reescribe el hash de la caché (`7abef529` → `2f4a4a11` al arrancar desde una segunda copia), y en dos de cinco rondas de pasadas simultáneas una página se quedó sin dibujar (el revisor vio `504 (Outdated Optimize Dep)`; la reproducción, módulos que nunca cargaron) y la pasada de hoy terminó en `ok`; con una carpeta por copia, seis pasadas en dos rondas simultáneas terminaron limpias y la caché del checkout principal no se tocó. (Una página puede quedarse sin dibujar también por el entorno: en una pasada completa con carpeta propia y otras pasadas en marcha, una de 468 cargas perdió módulos con `net::ERR_NETWORK_CHANGED` de Chromium. La puerta nueva la rechazó, que es lo que debe hacer, y se repite: «Cierre de cada oleada», paso 2.)
- **Commits.** Uno por paso «Commit», con el formato de los anteriores (`feat(ui): …`, `test(ui): …`) y estas dos líneas al final del mensaje:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr
  ```
  Nunca se commitea `frontend/.harmony/` (está en `.gitignore`).

## Archivos compartidos (dueño único)

Las oleadas corren en paralelo y **ningún archivo tiene dos dueños**. Cada archivo que dos oleadas a la vez tendrían que tocar está en una de estas tres clases, y no hay una cuarta.

**1. De la base (Tasks 1 a 5): ninguna oleada los edita.**

| Archivo | Dueño |
|---|---|
| `frontend/src/index.css`, `frontend/index.html`, `frontend/public/` | Tasks 1 y 2 |
| `frontend/src/shell/**` (kit, armazón, CSS, `nav.ts`, `viz.*`) | Tasks 1 a 4 |
| `frontend/src/components/**` | Tasks 1 (`chart.tsx`) y 3 |
| `frontend/src/App.tsx` y todos los `frontend/src/App*.test.tsx` salvo `App.portal.test.tsx` | Tasks 3, 4 y 5 |
| `frontend/scripts/**` (`routes.mjs`, `routes.d.mts`, `harmony.mjs`, `harmony.d.mts`, `a11y-browser.mjs`) | Tasks 1 a 5. Las variantes de ruta que las oleadas necesitan ya las añade la Task 5 (Step 8) |
| `frontend/src/lib/**` (`api.ts`, `clientScope.tsx`, `clientChoice.ts`, `useScrollToHash.ts`, `labels.ts`…) | Tasks 2 y 3 |
| `frontend/src/fixtures/**` (`client.ts`, `core.ts` y el registro): `listDevices`, `getDevice`, `getDeviceInventory`, `getDashboardLayout`, `getHealthAlertSummary`, `me` viven aquí | Task 3 |
| `frontend/src/i18n/{en,es}/{shell,components,common,enums,lib,time,validation,clients,index}.ts`, `frontend/src/i18n/test-utils.tsx`, `frontend/src/i18n/identical-ok.ts` (las entradas compartidas) | Tasks 2 a 5 |
| `frontend/src/test/**` | Tasks 1 a 4 |
| `frontend/src/preview/PreviewPage.tsx` | Task 3, que deja hechas las secciones «Kit v4», «AuthCard embedded» y «Portal frame»: el marco de acceso (O8) y el del portal (O2) se repintan solos ahí. Ninguna oleada lo toca |
| `frontend/src/modules/clients/**`, `frontend/src/modules/onboarding/journey.ts` (el parámetro del recorrido, Task 5 Step 3b) | Task 5; después, `onboarding/**` es de la O1 |
| `DESIGN.md`, `PRODUCT.md`, `.impeccable/design.json`, `docs/product.md` | Task 1 (y Task 14 si la revisión final pide un cambio) |
| `.impeccable/config.json` (las excepciones del detector, `impeccable ignores add-value … --shared`) y `.impeccable/critique/ignore.md` | **el coordinador**, en la rama base. Una oleada propone la entrada exacta en su informe |

**2. Partidos por oleada (la Task 3 los parte, Step 3b):** `frontend/src/i18n/identical-ok.o1.ts` a `identical-ok.o8.ts`. Cada oleada pone y quita en el suyo, y solo en el suyo, las entradas de sus catálogos (la que deja idéntica una clave nueva en inglés y en español, y la que borra la entrada de una clave que renombra o borra). `catalogue.identical.test.ts` fija que cada archivo solo nombre las claves de su oleada.

**3. De la oleada, y de nadie más:** sus carpetas `frontend/src/modules/<m>/**` (con `fixtures.ts` y sus pruebas), `frontend/src/i18n/{en,es}/<m>.ts`, su `identical-ok.o<N>.ts`, las críticas que escribe (`.impeccable/critique/<fecha>__<slug>.md`: un archivo nuevo por cada ejecución, que nadie más escribe) y, solo la O2, `frontend/src/App.portal.test.tsx`.

**Lo que un módulo exporta a otro conserva su firma.** Antes de cambiar el nombre o las props de algo exportado, la oleada busca quién lo importa (`grep -rn "from \"\.\./<m>/" frontend/src/modules`); si lo importa un módulo de otra oleada (por ejemplo, `StartSessionDialog` de `remote` lo usa `service-desk`), la firma no cambia: se añade una prop opcional o se deja como está.

**Petición al kit.** Si una oleada necesita algo de la clase 1 (una pieza del kit que falta, una ruta, una clave compartida, un dato de `fixtures/core.ts`), **no lo edita**: lo anota en su informe con el código exacto, el coordinador lo aplica en la rama base (`p3/base`) y la oleada lo trae con `git stash && git rebase p3/base && git stash pop` (sus archivos no son los del kit, así que el `pop` no choca).

## Oleadas y paralelismo

| Fase | Tareas | En paralelo | Depende de |
|---|---|---|---|
| Base | 1 → 2 → 3 → 4 → 5 | no, en serie (comparten `index.css`, `shell/`, `App.tsx`) | — |
| Lote A | 6 (O1 Equipos), 7 (O2 Inicio, tickets y portal), 8 (O3 Alertas, parches, software y sesiones), 9 (O4 Copias, continuidad e imágenes) | sí, cuatro worktrees | 5 |
| Lote B | 10 (O5 Activos), 11 (O6 Documentación, informes, cambios y presupuesto), 12 (O7 Administración y cuenta), 13 (O8 Acceso y consentimiento) | sí, cuatro worktrees | 5 (no depende del lote A) |
| Cierre | 14 | no | 6 a 13 |

```
T1 ─ T2 ─ T3 ─ T4 ─ T5 ─┬─ T6  (O1) ─┐
                        ├─ T7  (O2) ─┤
                        ├─ T8  (O3) ─┤      lote A: 4 implementadores
                        ├─ T9  (O4) ─┤
                        ├─ T10 (O5) ─┤
                        ├─ T11 (O6) ─┤      lote B: 4 implementadores
                        ├─ T12 (O7) ─┤
                        └─ T13 (O8) ─┴─ T14
```

El lote B puede empezar a la vez que el A si hay ocho implementadores; con cuatro, empieza cuando terminan los del A. Cada worktree se prepara como dicen las Global Constraints («Worktrees»): su propia carpeta `node_modules`, nunca la compartida. Las oleadas son disjuntas por archivo («Archivos compartidos»), así que el orden de fusión no importa; el coordinador fusiona cada una cuando su revisión aprueba (`git merge --no-ff p3/oleada-<N>` en `p3/base`) y vuelve a pasar `npx vitest run` completo tras cada fusión.

Choques al fusionar: **ninguno**, y no es una esperanza sino la consecuencia de la tabla de arriba. Si `git merge` informa un choque, la tabla tiene un archivo con dos dueños: el coordinador para, lo resuelve a mano en la rama base y lo anota en el informe como defecto del plan. `frontend/.harmony/` no se versiona. Las críticas de impeccable sí se versionan (resolución del coordinador del 2026-11-24): cada `critique-storage write` crea un archivo nuevo con su fecha (`2026-11-24T00-26-01Z__<slug>.md`, y `…~0001__<slug>.md` si se repite en el mismo segundo), uno por ejecución y no uno por página, así que dos oleadas nunca escriben el mismo.

## Mapa de archivos de la base

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `PRODUCT.md` (nuevo) | El registro de producto que impeccable lee | 1 |
| `DESIGN.md` | v4 «Novieri»: tokens normativos y reglas | 1 |
| `.impeccable/design.json` | Sidecar de `DESIGN.md` (metadatos, componentes, narrativa) | 1 |
| `frontend/src/index.css` | Tokens transcritos de `DESIGN.md` v4 | 1 |
| `frontend/src/test/tokens.test.ts` | `index.css` = `DESIGN.md` = `novieri-tokens.json` | 1 |
| `frontend/src/test/contrast.test.ts`, `radius.test.ts`, `base-styles.test.ts` | Puertas con la marca nueva | 1 |
| `frontend/src/components/chart.tsx`, `frontend/src/shell/viz.css`, `frontend/src/shell/viz.tsx`, `frontend/src/test/second-carrier.test.tsx` (nuevo) | «En curso» y «información» comparten el cobalto (D2): el segundo portador donde no hay etiqueta con icono | 1 |
| `frontend/scripts/harmony.mjs`, `frontend/scripts/routes.mjs` (`FRAMELESS`) | La puerta falla si una página no dibuja el armazón o el servidor de desarrollo responde 5xx | 1 |
| `frontend/public/favicon.svg` (nuevo) | El favicon de la marca | 2 |
| `frontend/src/shell/brand.tsx` (nuevo) | Nombre del producto, símbolo, logotipo y conjunto «Novieri RMM» | 2 |
| `frontend/src/shell/title.tsx` (nuevo) | `documentTitle`, `useRecordTitle`, `useDocumentTitle` | 2 |
| `frontend/src/test/no-itms.test.ts` (nuevo) | Guarda: «ITMS» fuera de `frontend/src` | 2 |
| `frontend/src/shell/listKit.tsx`, `detailKit.tsx`, `formKit.tsx`, `kit.css` (nuevos) | El kit de patrones | 3 |
| `frontend/src/test/renderPage.tsx`, `frontend/src/test/narrowing.ts` (nuevos) | Una página bajo un router de datos, con el título de pestaña del armazón si se pide; un doble de la API que anota a qué cliente se estrechó cada lectura | 3 |
| `frontend/src/lib/api.ts` (`forClient`), `frontend/src/fixtures/client.ts` | Lectura acotada a un cliente para el chip «Cliente» y la ficha 360 | 3 |
| `frontend/src/App.tsx`, `frontend/src/shell/RouteError.tsx` (nuevo) | Router de datos (para avisar antes de salir) y su pantalla de error traducida | 3 |
| `frontend/src/i18n/identical-ok.o1.ts` a `identical-ok.o8.ts` (nuevos) | Las entradas de `identical-ok.ts` de cada oleada, en su propio archivo | 3 |
| `frontend/src/shell/nav.ts`, `Sidebar.tsx`, `AdminPanel.tsx` (nuevo), `NewMenu.tsx` (nuevo), `newMenu.ts` (nuevo), `AppShell.tsx` (con el botón flotante del Asistente), `AreaPage.tsx`, `shell.css` | El armazón | 4 |
| `frontend/src/modules/clients/**`, `frontend/src/modules/onboarding/journey.ts` | Lista de clientes y ficha 360; el recorrido de «Añadir un equipo» deja libre `?client=` | 5 |

---

## Recetas de página (vinculantes para las oleadas)

Las oleadas (Tasks 6 a 13) aplican una de estas cuatro recetas a cada página. Las piezas y sus firmas se definen, con su código completo, en la Task 3; aquí está cómo se usan. **Todo implementador de oleada lee esta sección antes que su tarea.**

### Receta L: lista

Forma (diseño §2, «Lista»; Atera: `Atera_recorrido_menus_UI_UX.md`, «Customers», «Devices», «Alerts»):

1. `PageHead` con título, una frase de 40 a 70 caracteres que dice qué es la página y **una** acción principal a la derecha (`Btn variant="primary"`), si la página crea algo.
2. Un `Panel bare` que contiene, en este orden: `ListToolbar` (búsqueda, `FilterChips` con su conteo, `ClientFilter` cuando la lista es de clientes, y `ShowingCount` al final); `BulkBar` **solo** si la página ya tenía acciones en lote; la tabla (`Table`, `Th`, `Td` de `components/ui.tsx`); el pie (`Pager`) si la página ya paginaba.
3. El estado vacío es `ListEmpty`: «nada todavía» con la acción de crear, o «nada con estos filtros» con «Quitar filtros». Nunca las dos frases a la vez, nunca un vacío sin causa.
4. Los filtros son `useUrlFilters`. Los conteos de los chips salen de la respuesta del servidor cuando la trae (`state_counts`, `by_state`, `total`); si no la trae, el chip va **sin** número (regla «Absent Zero»), nunca con uno calculado sobre la página visible.
5. Una pestaña que filtraba la lista (`Tabs` con `?state=`) pasa a `FilterChips` con el mismo parámetro: la URL no cambia.
6. **El chip «Cliente» estrecha la lectura de la lista, y solo la lectura** (decisión D6). La lista lee con `useApiFor(filters.values.client)`, no lee nada hasta que `useNarrowing(filters.values.client).ready` (hasta que `/me` dice si ese cliente es de la persona), y su clave de consulta lleva `narrowing.client`: cambiar el chip vuelve a leer y nunca deja en pantalla las filas del cliente anterior bajo el chip nuevo. Un identificador que no es de la persona no se manda: `ClientFilter` lo quita de la URL y la lista lee como si no estuviera. **Las escrituras siguen con `useApi()`**: una acción de fila o de lote manda la elección de la barra de arriba, como hoy; el chip no cambia a qué cliente va una escritura.
7. **Una lista que lee por un hook de su módulo** (`useLicenses`, `useSubscriptions`, `useIpamSubnets`, `useHealthAlerts`, `useDeployments`, `useBackupPoints`, `usePlans`, `useChanges`, `useSiteServers`, `usePatchFleetReport`…: hoy llaman a `useApi()` dentro y usan una clave fija) cambia **el hook**, no la llamada: toma el valor del chip, lee con `useApiFor` y pone el cliente en su clave. Es la misma llamada a la API, con los mismos argumentos; cambia a quién estrecha y bajo qué clave se guarda, así que no choca con «Prohibido … cambiar una llamada a la API». Ejemplo exacto, `modules/licenses/api.ts`:
   ```ts
   // Antes:
   export function useLicenses() {
     const api = useApi();
     return useQuery({ queryKey: ["licenses"], queryFn: () => api.listLicenses() });
   }
   // Después: `chip` es el valor de `?client=` ("" sin chip, como lo llaman los demás).
   export function useLicenses(chip = "") {
     const narrowing = useNarrowing(chip);
     const api = useApiFor(chip);
     return useQuery({
       queryKey: ["licenses", narrowing.client],
       queryFn: () => api.listLicenses(),
       enabled: narrowing.ready,
     });
   }
   ```
   Las mutaciones siguen invalidando el prefijo (`invalidateQueries({ queryKey: ["licenses"] })` alcanza `["licenses", null]` y `["licenses", "<id>"]`). Antes de cambiar una clave, la oleada busca sus lecturas y escrituras exactas (`grep -rn 'QueryData(\["licenses"\]' frontend/src`) y las pasa a la clave nueva; hoy no hay ninguna para `licenses`.
8. **Una lista que solo busca** (sin chips: `SoftwarePage`, `VendorsPage`, `ProductsPage`…): `ListToolbar` con la búsqueda en `?q=` y `ShowingCount`; su prueba es la variante de búsqueda de abajo. **Una lista sin ningún filtro** (`MyWorkPage`, `ConfirmationQueuePage`): `ShowingCount` y `ListEmpty filtered={false}`; su prueba es solo «says why the list is empty».

```tsx
// Receta L, esqueleto. Las claves de filtro son las que la página ya usaba en
// la URL (o, si las tenía en useState, las nuevas que nombra su oleada).
const filters = useUrlFilters(["q", "state", "client"] as const);
const narrowing = useNarrowing(filters.values.client);
const api = useApiFor(filters.values.client); // solo para leer la lista
const list = useQuery({
  queryKey: ["<módulo>", "list", filters.values.q, filters.values.state, narrowing.client],
  queryFn: () => api.listX({ q: filters.values.q, state: filters.values.state }),
  enabled: narrowing.ready,
});
const rows = list.data?.items ?? [];

return (
  <>
    <PageHead title={t("<m>.list.title")} description={t("<m>.list.description")} actions={primary} />
    <Panel bare>
      <ListToolbar
        search={
          <SearchField
            aria-label={t("<m>.list.search")}
            placeholder={t("<m>.list.search")}
            value={filters.values.q}
            onChange={(event) => filters.set("q", event.target.value)}
          />
        }
        chips={
          <>
            <FilterChips
              label={t("<m>.list.stateLabel")}
              options={STATES.map((state) => ({ value: state, label: t(STATE_LABEL[state]), count: list.data?.state_counts[state] }))}
              value={filters.values.state}
              onChange={(value) => filters.set("state", value)}
            />
            <ClientFilter value={filters.values.client} onChange={(value) => filters.set("client", value)} />
          </>
        }
        end={<ShowingCount shown={rows.length} total={list.data?.total ?? null} />}
      />
      <QueryState loading={list.isLoading || !narrowing.ready} error={list.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty
            filtered={filters.active}
            onClear={filters.clear}
            nothingYet={t("<m>.list.empty")}
            action={primary}
          />
        ) : (
          <Table label={t("<m>.list.title")}>{/* thead + tbody */}</Table>
        )}
      </QueryState>
    </Panel>
  </>
);
```

Pruebas mínimas que toda lista convertida añade (en el archivo de pruebas de su módulo). Las de chips:

```tsx
it("keeps its filters in the URL, so a filtered view is a link", async () => {
  const { router } = renderPage("/<ruta>?state=<valor>", <Page />);
  expect(await screen.findByRole("button", { name: /<etiqueta del valor>/ })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(screen.getByRole("button", { name: /<otra etiqueta>/ }));
  expect(router.state.location.search).toContain("state=<otro valor>");
});

it("says why the list is empty", async () => {
  renderPage("/<ruta>?state=<valor sin filas en la respuesta>", <Page />);
  expect(await screen.findByText("Nothing matches these filters.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
});
```

La variante de búsqueda, para una lista que solo busca (en lugar de la primera):

```tsx
it("keeps its search in the URL, so a reload finds the same rows", async () => {
  const { router } = renderPage("/<ruta>", <Page />);
  await userEvent.type(await screen.findByRole("searchbox", { name: "<etiqueta de la búsqueda>" }), "<texto>");
  expect(router.state.location.search).toBe("?q=<texto>");
  await act(() => router.navigate("/<ruta>?q=<texto>"));
  expect(screen.getByRole("searchbox", { name: "<etiqueta de la búsqueda>" })).toHaveValue("<texto>");
});
```

Y la del chip «Cliente», en **toda** lista que lo lleve (`recordingNarrowing` es la ayuda de la Task 3, `src/test/narrowing.ts`; `ANDES` es `5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57`, el cliente de `src/fixtures/core.ts`):

```tsx
it("reads the list narrowed to the Client chip's client (decision D6)", async () => {
  const { api, narrowedTo } = recordingNarrowing(fixtureClient());
  mockUseApi.mockReturnValue(api);
  renderPage(`/<ruta>?client=${ANDES}`, <Page />);
  expect(await screen.findByRole("table", { name: "<título de la lista>" })).toBeInTheDocument();
  expect(narrowedTo).toContain(ANDES);
});
```

`renderPage` es el harness de la Task 3, `src/test/renderPage.tsx`. La API se sigue inyectando como en todo el repositorio, con `vi.mock` del módulo `lib/ApiContext` (231 archivos de prueba lo hacen así):

```tsx
const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
// ...
beforeEach(() => mockUseApi.mockReturnValue(api));
const { router } = renderPage("/<ruta>?state=<valor>", <Page />);
```

`renderPage(path, element, pattern?, options?)` monta `element` en un `createMemoryRouter` (router de datos, para que `UnsavedGuard` pueda bloquear) dentro de un `QueryClientProvider` sin reintentos, y devuelve `{ router, queryClient }`; con `options.page` (una clave del catálogo, la etiqueta del riel de la página: `"shell.nav.item.devices"`) envuelve además la página en el `RecordTitleProvider` del armazón, para que una ficha pueda probar su título de pestaña (Receta F). Una prueba existente que monta con `MemoryRouter` sigue valiendo; solo se cambia a `renderPage` la que necesita leer `router.state.location` o el título.

### Receta F: ficha

Forma (diseño §2, «Ficha»; Atera: captura «atera 8»):

1. `DetailHead`: volver a la lista, título (el nombre del registro), etiqueta de estado (`status`), contexto (cliente y sede, con enlace a la ficha 360) y las acciones a la derecha. La página llama a `useRecordTitle(nombre)` para que la pestaña diga `<Registro> · <Página> · Novieri RMM`.
2. Una fila de tarjetas de resumen (`StatsGrid` + `StatCard`/`Tile` con `to`): cada número lleva a donde vive.
3. `UrlTabs` con `useUrlTab`: una sola línea (`scroll`), la pestaña por defecto sin parámetro, una desconocida cae en la por defecto.
4. Cuando la ficha tiene información y actividad, `DetailGrid` con `main` (información, a la izquierda) y `aside` (métricas y actividad, a la derecha).

```tsx
const TABS = ["overview", "hardware", "software"] as const;
type Tab = (typeof TABS)[number];
const [tab, setTab] = useUrlTab<Tab>(TABS, "overview");
useRecordTitle(device.data?.hostname);

return (
  <>
    <DetailHead
      back={{ to: "/devices", label: t("<m>.detail.back") }}
      title={device.data?.hostname ?? t("<m>.detail.title")}
      status={{ kind: online ? "ok" : "mut", label: t(online ? "<m>.online" : "<m>.offline") }}
      context={<ClientContext clientId={device.data?.client_id ?? null} siteName={device.data?.site_name ?? null} />}
      actions={actions}
    />
    <StatsGrid columns={2}>{/* tarjetas */}</StatsGrid>
    <UrlTabs
      label={t("<m>.detail.tabs")}
      items={TABS.map((key) => ({ key, label: t(TAB_LABEL[key]) }))}
      active={tab}
      onSelect={setTab}
    >
      {tab === "overview" ? <Overview /> : tab === "hardware" ? <Hardware /> : <Software />}
    </UrlTabs>
  </>
);
```

Prueba mínima de toda ficha convertida:

```tsx
it("puts the tab in the URL, and an unknown tab opens the default", async () => {
  const { router } = renderPage("/<ruta>/<id>?tab=<pestaña>", <Page />);
  expect(await screen.findByRole("tab", { name: "<Etiqueta>" })).toHaveAttribute("aria-selected", "true");
  await userEvent.click(screen.getByRole("tab", { name: "<Otra>" }));
  expect(router.state.location.search).toBe("?tab=<otra>");
  await act(() => router.navigate("/<ruta>/<id>?tab=nope"));
  expect(await screen.findByRole("tab", { name: "<Por defecto>" })).toHaveAttribute("aria-selected", "true");
});
```

Y la del título de pestaña, en toda ficha (la de una ficha sin pestañas es solo esta). `useRecordTitle` no hace nada fuera del armazón, así que la prueba pide el suyo con `options.page`, la clave de la etiqueta del riel de la página:

```tsx
it("names the record in the browser tab", async () => {
  renderPage("/<ruta>/<id>", <Page />, "/<ruta>/:<param>", { page: "shell.nav.item.<entrada del riel>" });
  await screen.findByRole("heading", { level: 1, name: "<nombre del registro>" });
  expect(document.title).toBe("<nombre del registro> · <etiqueta del riel en inglés> · Novieri RMM");
});
```

### Receta FO: formulario

Forma (diseño §2, «Formulario»; Atera: captura «atera 12»):

1. `FormPage` (volver y título) cuando la página **es** un formulario; si el formulario es una pestaña de una página (Configuración), la pestaña usa directamente las secciones y la barra.
2. `FormSection` por grupo de campos, plegables, abiertas por defecto.
3. Los campos con ayuda son `Field` con `help` (`components/ui.tsx`, diseño de ajustes de 2026-09-10).
4. **Una sola** `SaveBar` por página, fija abajo: «Cambios sin guardar · Descartar · Guardar». Desaparecen los botones «Guardar» sueltos por panel. `useDraft` calcula `dirty`; `UnsavedGuard` avisa antes de salir. Lo que hacía el botón suelto lo hace la barra: «Guardado.» tras el 2xx (`saved`), los problemas del formulario (`problems`), la razón por la que no se puede guardar (`blockedReason`), y Intro en un campo sigue guardando si la barra va dentro del `<Form>` con `submit`.
5. **Salir es cambiar de ruta.** `UnsavedGuard` pregunta cuando la ruta cambia; un cambio solo de la query (una pestaña, un filtro, `?new=`, el registro que un guardado acaba de seleccionar) no es salir. Una página cuya query **es** lo que edita (Configuración: cada pestaña es otro formulario) lo dice con `search`: `<UnsavedGuard dirty={dirty} search />`.
6. **Una página que se va al guardar** (un alta que lleva al registro creado, el portal que abre el ticket enviado) usa `useUnsavedGuard`, que devuelve la guarda y `release()`, y llama a `release()` justo antes de navegar: `useBlocker` lee su condición de un efecto, así que un manejador que guarda y navega en el mismo paso sería juzgado con los cambios todavía puestos (revisión previa, I-1, reproducido con react-router 7.18.1).
7. Una alta pequeña (un diálogo de dos o tres campos) **sigue siendo un diálogo** (`Dialog`); el diseño restiliza los diálogos, no los convierte en páginas.

```tsx
const saved = settings.data;                  // lo que el servidor dice hoy
const { draft, setDraft, dirty, discard } = useDraft(saved);
const save = useMutation({ mutationFn: (body: X) => api.updateX(body), onSuccess: () => settings.refetch() });

return (
  <FormPage back={{ to: "/<lista>", label: t("<m>.back") }} title={t("<m>.title")}>
    <UnsavedGuard dirty={dirty} />
    <FormSection title={t("<m>.section.general")} icon="settings">
      {/* Field ... value={draft?.x} onChange={(e) => setDraft({ ...draft!, x: e.target.value })} */}
    </FormSection>
    <SaveBar
      dirty={dirty}
      saving={save.isPending}
      error={save.error}
      saved={save.isSuccess && !dirty}
      onDiscard={discard}
      onSave={() => draft && save.mutate(draft)}
    />
  </FormPage>
);
```

La forma de la página que se va al guardar (6):

```tsx
const { guard, release } = useUnsavedGuard(dirty);
const create = useMutation({
  mutationFn: (body: X) => api.createX(body),
  onSuccess: (created) => {
    release();
    navigate(`/<lista>/${created.id}`);
  },
});
return (
  <FormPage back={{ to: "/<lista>", label: t("<m>.back") }} title={t("<m>.newTitle")}>
    {guard}
    {/* … */}
  </FormPage>
);
```

Pruebas mínimas de todo formulario convertido:

```tsx
it("shows one save bar only while something changed, and Discard puts it back", async () => {
  renderPage("/<ruta>", <Page />);
  expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  await userEvent.type(await screen.findByLabelText("<Etiqueta>"), "x");
  expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
});

it("asks before leaving with unsaved changes", async () => {
  const { router } = renderPage("/<ruta>", <Page />, "/<ruta>");
  await userEvent.type(await screen.findByLabelText("<Etiqueta>"), "x");
  // Leave as a person does: through a link (the page's back link). A bare
  // router.navigate() held by the blocker leaves a pending transition.
  await userEvent.click(screen.getByRole("link", { name: "<texto del enlace volver>" }));
  expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/<ruta>");
});
```

Y en una página que se va al guardar (6), además:

```tsx
it("leaves for what it saved without asking", async () => {
  const { router } = renderPage("/<ruta>", <Page />, "/<ruta>");
  await userEvent.type(await screen.findByLabelText("<Etiqueta>"), "x");
  await userEvent.click(screen.getByRole("button", { name: "<Guardar o Enviar>" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/<ruta del registro creado>"));
  expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
});
```

### Receta O: otra

Paneles de control (Inicio, informes, estado), visores (sesión remota, grabación, documento), asistentes de pasos y las pantallas de acceso. Usan `PageHead` (o su marco propio), `Panel`, `StatsGrid`, `Tile` y las piezas del kit que apliquen, y conservan su forma. La oleada dice qué cambia en cada una.

---

## Cierre de cada oleada (vinculante)

Cada oleada termina con estos pasos, **después** de que sus pruebas, `npm run typecheck` y `npm run build` estén en verde. Todas las rutas de archivo de la crítica son **relativas a la raíz del checkout** (`frontend/src/modules/<m>/<Pagina>.tsx`), nunca absolutas: el nombre de cada crítica guardada sale de esa ruta (`critique-storage slug`), así que es el mismo en la worktree de la oleada y en la rama fusionada.

1. **Contexto de impeccable, una vez por sesión** (`SKILL.md` §Setup): `.claude/skills/impeccable/scripts/impeccable context --target frontend/src/modules/<m>` desde la raíz de la worktree. Tras la Task 1 no debe decir `NO_PRODUCT_MD` ni `PRODUCT_INIT_REQUIRED`; si lo dice, la oleada para y lo informa. Luego se lee `.claude/skills/impeccable/reference/craft-floor.md` antes de la primera edición de UI del pulido.
2. **Capturas** para la crítica y para el revisor:
   ```bash
   cd frontend
   test -d node_modules && test ! -L node_modules && echo "node_modules propio"   # Global Constraints, «Worktrees»
   node scripts/harmony.mjs --strict-fixtures --lang es --routes <rutas de la oleada>
   node scripts/harmony.mjs --strict-fixtures --lang en --routes <rutas de la oleada>
   ```
   La primera línea imprime `node_modules propio`; si no imprime nada, la worktree comparte la caché de Vite con las demás y se rehace como dicen las Global Constraints antes de seguir. Salida esperada al final de cada pasada: `ok    every route on the tokens, no overflow, the top bar in bounds, no serious axe violation, no fixture miss`. Desde la Task 1 (Step 12) la pasada **falla** si una ruta de la consola no dibuja el armazón (`drew no shell`) o si el servidor de desarrollo respondió 5xx a un archivo de la página (`the dev server answered 504 /node_modules/.vite/deps/…`): eso no es un defecto de la página sino del entorno, y se repite la pasada con la worktree arreglada; nunca se da por buena. Las capturas quedan en `frontend/.harmony/<lang>/<viewport>/<ruta>.png` (`/devices/dev-1` → `devices__dev-1.png`) a 1440x900, 390x844, 390x844-client y 641x900-client. La puerta ya corre axe (WCAG A/AA y buenas prácticas) en cada ruta y viewport: ese es el pase de accesibilidad de las rutas de la oleada.
3. **Crítica de impeccable, página por página** (`reference/critique.md`), sobre el archivo de cada página:
   - `.claude/skills/impeccable/scripts/impeccable critique-storage slug "frontend/src/modules/<m>/<Pagina>.tsx"`;
   - leer `.impeccable/critique/ignore.md` si existe (es del coordinador: la oleada no lo edita);
   - **dos subagentes aislados en paralelo** cuando la sesión tiene la herramienta de subagentes: A (revisión de diseño: lee el archivo de la página y sus capturas en los dos idiomas y en 1440 y 390, y puntúa las 10 heurísticas de Nielsen) y B (`.claude/skills/impeccable/scripts/impeccable detect --json frontend/src/modules/<m>` más la capa en el navegador: `npx vite` desde `frontend/` en esta worktree, que imprime su puerto propio (`Local: http://localhost:<puerto>/`); `impeccable live-server --background`, que imprime el suyo; Chromium por Playwright en una pestaña nueva sobre `http://localhost:<puerto de vite>/<ruta>?__fixtures`, inyectar `http://localhost:<puerto del live-server>/detect.js`, leer los mensajes `impeccable` de la consola y terminar con `impeccable live-server stop --keep-inject` y parando `npx vite`). Si la sesión no tiene herramienta de subagentes, el informe empieza con `⚠️ DEGRADED: single-context (no sub-agent tool exposed)` y se hacen A y B en serie, como manda la referencia;
   - un hallazgo del detector que es un falso positivo no se «corrige»: el informe de la oleada da la orden exacta para el coordinador (`impeccable ignores add-value <regla> <valor> --file <ruta relativa> --reason "<por qué>" --shared`), que la aplica en la rama base porque `.impeccable/config.json` es suyo;
   - el informe completo va en el informe de la oleada; se persiste con `IMPECCABLE_CRITIQUE_META='{…}' .claude/skills/impeccable/scripts/impeccable critique-storage write "frontend/src/modules/<m>/<Pagina>.tsx" <archivo-temporal>` y se lee la tendencia con `critique-storage trend`;
   - **las preguntas de la crítica** (resolución del coordinador del 2026-11-24): no van al operador. Las contesta el coordinador con `DESIGN.md`, el diseño aprobado y las decisiones del operador, y las respuestas viven en **`.impeccable/critica-respuestas.md`**, versionado en la rama base (es del coordinador). La primera crítica del programa (la de `/__preview`, Task 3, Step 11) lo inaugura y la del armazón (Task 4, Step 12) lo amplía; las siguientes (Task 5 y las oleadas) lo leen como respuestas vigentes, y solo la pregunta que esas fuentes no zanjan sube al operador, a través del coordinador. Si una crítica tiene menos de tres asuntos prioritarios, se escribe la línea literal `Questions skipped: <n> priority issues`.
4. **Pulido de impeccable, página por página** (`reference/polish.md`): `critique-storage latest "frontend/src/modules/<m>/<Pagina>.tsx" --json`, corregir los P0 y P1 que la crítica dejó (en este orden: tareas rotas, estados que faltan, jerarquía y deriva del kit, detalles visuales, limpieza), sin rediseño escondido, y al terminar `critique-storage close "frontend/src/modules/<m>/<Pagina>.tsx" "<snapshot_file>"`, donde `<snapshot_file>` es el nombre (sin carpeta) que devolvió `latest --json`. Un `close` cerrado escribe `closed: true` en la cabecera de la crítica, que es lo que la Task 14 cuenta. Lo que el pulido no corrige se anota como P2/P3 en el informe.
5. **Repetir el paso 2** tras el pulido y adjuntar al informe la lista de PNG de 1440x900 y 390x844 de cada ruta en los dos idiomas.
6. `npx vitest run`, `npm run typecheck`, `npm run build` completos: verde.
7. **Revisión de la oleada** (superpowers:requesting-code-review): el revisor lee el diff, las capturas y las críticas, y comprueba los criterios de aceptación de cada página («Acepta:» de cada bloque y los de la oleada).
8. **El commit de la oleada incluye sus críticas**: `git add .impeccable/critique` junto a los archivos de la oleada (cada crítica es un archivo nuevo; ninguna oleada edita la de otra).

---

## Tareas

| Task | Qué | Diseño |
|---|---|---|
| 1 | Contexto de impeccable (`PRODUCT.md`, `design.json`), `DESIGN.md` v4 «Novieri», tokens de la marca y sus puertas, el segundo portador de «en curso» y la puerta de armonía que falla una página sin dibujar | §3.1, §3.2 |
| 2 | Nombre «Novieri RMM», logotipo, símbolo, favicon, título de pestaña y guarda de «ITMS» | §3.1, A3, §5 |
| 3 | El kit: lista, ficha, formulario, lectura por cliente, router de datos con su pantalla de error, entradas idénticas por oleada y restilo por tokens de las piezas que ya existen | §2 |
| 4 | El armazón: barra lateral en el orden de Atera, Administración en acordeón, «Nuevo», barra superior sin el Asistente, el Asistente al pie del riel y en su botón redondo | §1.1, §1.2 |
| 5 | Clientes: la lista y la ficha 360 con pestañas en la URL, el parámetro del recorrido renombrado y las vistas de las rutas nuevas en la puerta | §1.3 |
| 6 | Oleada O1: Equipos (11 páginas) | §4 |
| 7 | Oleada O2: Inicio, tickets y portal (11 páginas) | §4 |
| 8 | Oleada O3: Alertas, parches, software y sesiones (11 páginas) | §4 |
| 9 | Oleada O4: Copias, continuidad, imágenes y servidores de sitio (11 páginas) | §4 |
| 10 | Oleada O5: Activos (11 páginas) | §4 |
| 11 | Oleada O6: Documentación, informes, cambios y presupuesto (9 páginas) | §4 |
| 12 | Oleada O7: Administración y cuenta (6 páginas) | §4 |
| 13 | Oleada O8: Acceso, consentimiento y alta del dueño (12 páginas) | §4 |
| 14 | Revisión de toda la rama y camino a `main` | §5 |

Páginas: 4 en la base (Tasks 4 y 5: `AreaPage`, `ClientsPage`, `ClientDetailPage`; `PreviewPage` en la Task 3) + 82 en las oleadas = **86**. Además, 22 pantallas enrutadas que no se llaman `*Page.tsx` entran en la oleada de su módulo (Apéndice B).

---

### Task 1: Contexto de impeccable, `DESIGN.md` v4 «Novieri» y los tokens de la marca

Diseño §3.1 y §3.2. Cambia la marca **sin tocar todavía ninguna forma**: después de esta tarea la consola es la de hoy con los colores, los radios de etiquetas y chips y la sombra de Novieri, y todas las puertas siguen en verde.

**Files:**
- Create: `PRODUCT.md`
- Modify: `DESIGN.md` (archivo completo, v4)
- Modify: `.impeccable/design.json` (archivo completo, regenerado)
- Modify: `docs/product.md` (título, §1, §9 líneas 238-263 y la fila de `tokens.test.ts` en §11)
- Modify: `frontend/src/index.css:1-103` (comentario de cabecera y primer `:root`), `:123-169` (bloque de alias), `:180-289` (`@theme inline`)
- Modify: `frontend/src/shell/shell.css:139-148` (`.sidebar .ct.hot`), `:350-366` (`.counter-dot`)
- Modify: `frontend/src/shell/components.css:402` (`.pill`), `:649` (`.chip`)
- Modify (el segundo portador de D2): `frontend/src/components/chart.tsx:112` (`SERIES_INK`), `frontend/src/shell/viz.css:110-115` (`.heat .h-info`), `frontend/src/shell/viz.tsx:253-293` (`Donut`)
- Modify (la puerta de armonía, Step 12): `frontend/scripts/harmony.mjs`, `frontend/scripts/routes.mjs`, `frontend/scripts/routes.d.mts`, `frontend/scripts/harmony.d.mts`
- Test: `frontend/src/test/tokens.test.ts` (reescrito), `frontend/src/test/contrast.test.ts`, `frontend/src/test/radius.test.ts`, `frontend/src/test/second-carrier.test.tsx` (nuevo), `frontend/src/test/harmony-client.test.ts` (se ejecuta también `routes-covered.test.ts`, sin cambios)

**Interfaces:**
- Consumes: `docs/design/marca/novieri-tokens.json` (no se edita).
- Produces: los tokens nuevos `--r-badge` (4px), `--rail-mark` (#8fa6ff), `--accent-3` (#1a3190); `--accent-2` pasa a ser el hover de la marca (#203cac); `--ink-2`, `--line-2`, `--sev-run` y `--sev-run-s` pasan al bloque de alias (`var(--ink)`, `var(--line)`, `var(--accent)`, `var(--accent-s)`); `--rail-hot` desaparece. Utilidades Tailwind nuevas: `rounded-xs` (→ `--r-badge`), `text-rail-mark`/`bg-rail-mark`. `DESIGN.md` v4 es la autoridad: su frontmatter nombra cada token con el nombre de la propiedad CSS sin `--`.

**Decisiones de esta tarea** (Apéndice A, D1 a D5):
- **D1.** La autoridad de los tokens pasa de `docs/design/itms-prototype-v3.html` a `DESIGN.md` v4, y la marca (`novieri-tokens.json`) fija su subconjunto. El prototipo v3 queda como historia (`harmony.mjs --proto` lo sigue fotografiando).
- **D2.** La marca tiene dos tintas de texto, un borde y cinco severidades con «información» en el tono del acento. Por eso `--ink-2` pasa a ser `--ink`, `--line-2` pasa a ser `--line` y el estado «en curso» (`--sev-run`) toma el tono de información (el operador lo confirmó el 2026-11-24: «en curso» se funde en el cobalto). La etiqueta «en curso» conserva su icono (`progress`), que es lo que la distingue de «información» (regla del segundo portador). **Donde no hay etiqueta con icono, el segundo portador se añade aquí**, y `second-carrier.test.tsx` lo fija (revisión previa, I-3): la serie 2 de los gráficos deja el cobalto por `--sev-mut` (6,15:1 sobre blanco) y conserva su rayado; la celda de calor de información (en IPAM, una dirección DHCP frente a una asignada) lleva un anillo interior; el anillo de un donut lleva el icono de su tipo junto a su etiqueta; las barras de la línea de tiempo ya lo tenían (cada fila lleva el icono de su tipo, `viz.tsx:345`). Prueba de la marca: la maqueta de la dirección A (`propuestas-2026-11-23.html`, `.m-table td` en `--text`, `.sub` en `--text-2`, una sola `--border`).
- **D3.** El contador de alertas del riel deja de ser texto rosa (`--rail-hot`) y pasa a ser blanco sobre la etiqueta crítica (fila «Contador de alertas» de la tabla de contraste de la marca, 6,57:1). El ítem activo se marca con `--rail-mark` (icono y punto), no con un borde de acento: el acento sobre el riel mide 2,32:1.
- **D4.** `--r-pill` se conserva solo para la pista del interruptor y la barra de progreso: la marca retira la píldora de chips y etiquetas, no de esas dos formas, que el usuario reconoce por su silueta.
- **D5.** `--accent-2` cambia de valor (#1a4fae → #203cac, el hover de la marca) y nace `--accent-3` (#1a3190, presionado).

- [ ] **Step 1: Escribir `PRODUCT.md`, ya confirmado por el operador**

`impeccable context` dice hoy `NO_PRODUCT_MD` y `PRODUCT_INIT_REQUIRED` (comprobado el 2026-11-23). `docs/product.md` ya lleva el marcador `impeccable:product-schema 1`, pero la herramienta busca `PRODUCT.md` en la raíz. `reference/init.md` exige una ronda de respuesta real antes de escribir uno nuevo, y esa ronda ya se hizo: **el operador confirmó este borrador tal cual el 2026-11-24 («Sí, tal cual»**, `decisiones-p34.md`, «Operator answers 2026-11-24»). Este paso escribe el archivo exactamente como está abajo y no vuelve a preguntar. Cada línea sale de `docs/product.md` (§1, §4, §8, §9, §10, §11), `docs/00-vision.md` y las decisiones A1 a A4 y B2.

Crear `PRODUCT.md`:

```markdown
# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The primary user is a technician on an in-house IT team or at an MSP, with the
console open all day on a desk beside a ticket queue: patching, onboarding and
imaging machines, remote sessions, backup assurance, and the evidence an
auditor asks for. They are generalists who touch every module. They also open
the console from a phone during an incident.

Two other audiences are confirmed: the auditor, who reads output (reports, the
audit log) rather than operating the fleet, and the end users of an MSP's
clients, who file and follow their own tickets in the support portal.

## Product Purpose

Novieri RMM is a self-hosted IT management platform by Novieri: one control
plane, one lightweight agent on every computer and server, and optional site
servers for work that must happen on a client's own network. From one web
console a team inventories its fleet, patches it, deploys software, takes
remote sessions, backs it up, documents it and runs the governance workflows
an auditor asks for. Success is an operator who finds the right client,
machine or ticket at once, acts through a signed job, and can prove what
happened afterwards.

## Positioning

The organisation of the category's leader, with a security model it can show.
Every job is signed by the server and verified on the endpoint before it runs;
agents connect outbound only, over mutual TLS; credentials are decrypted only
at the point of use; every privileged action is written to a hash-chained
audit log; and an MSP's clients are kept apart by the database itself, down to
each technician's scope. The platform does not phone home.

## Operating Context

- One console over many client organisations (MSP). A client selector in the
  top bar narrows every request to one client; "All my clients" shows the
  person's whole scope.
- Spanish and English; the operator works in Spanish (Colombia).
- Dense desk use first; a 390px phone is a supported viewport.
- Deployed on the operator's own server (`rmm.novieri.com`); many deployments
  have no internet egress, so nothing is fetched from a CDN.

## Capabilities and Constraints

- Modules: devices and inventory, alerts and health, patching, software
  deployment, remote sessions, backup, imaging, IT documentation, changes,
  continuity tests, reports, licenses, subscriptions, IT budget, IP address
  management, tickets with a support portal, notifications, an AI assistant,
  and the administration around them (enrollment, agent packages, push
  install, credentials, audit and security logs, platform status, settings).
- Authorization is server-side; the console never gates security. A hidden
  link is a courtesy.
- Light theme only. Inter is self-hosted.
- Undecided: dark mode (out of scope), an in-app notification inbox (none
  exists; notifications go by email and webhook), server-saved views.

## Brand Commitments

- Name: **Novieri RMM** (operator, 2026-11-23). "Novieri" in the console's
  bar; "Page · Novieri RMM" in the browser tab; "Novieri RMM" in emails and on
  managed computers.
- Identity: direction A, "Cordillera", chosen by the operator on 2026-11-23
  from three proposals: a night-blue rail, one cobalt accent, an uppercase
  geometric wordmark and a zigzag N read as two peaks and a monitoring pulse.
  The assets and the exact values are in `docs/design/marca/`.
- Atera's organisation, never its look (operator, 2026-11-23): its menu, its
  per-client pages, its filtered lists, its tabbed records and its "New" menu;
  Novieri's colours and shapes.
- Voice: say what happened and what follows from it; name what failed and
  what was and was not changed; no reassurance in place of information.
- No user-visible string names the identity vendor.

## Evidence on Hand

None for marketing: no customer names, testimonials, case studies,
third-party benchmarks, press or published pricing (operator, 2026-08-21).
The only quotable figures are the measured ones in `docs/product.md` and the
test suite that produced them. Nothing may be invented.

## Product Principles

1. Copy the category's structure and compete on consistency: one way to save,
   every view addressable by URL, a unique tab title, an empty state that says
   its real cause.
2. Every control leads somewhere real. Nothing measured is drawn as absent,
   never as zero.
3. Security is enforced by the server and the database, and the console says
   so rather than pretending to guard it.
4. Dense for the people who live in it, legible on a phone during an incident.
5. Two languages, one product: every word exists in both.

## Accessibility & Inclusion

WCAG 2.2 Level AA is a product requirement (operator, 2026-08-21). It is
measured, not asserted: `frontend/src/test/contrast.test.ts` computes every
token pair, the harmony gate runs axe on every route in both languages at four
viewports, and `frontend/scripts/a11y-browser.mjs` measures composited
contrast on the component harness. Touch targets reach 44px on coarse
pointers; nothing renders below 10.5px.
```

- [ ] **Step 2: Escribir la prueba de tokens nueva (falla)**

Reemplazar el contenido completo de `frontend/src/test/tokens.test.ts`:

```ts
/**
 * THE TOKENS ARE A TRANSCRIPTION, AND THREE FILES MUST AGREE.
 *
 * `DESIGN.md` v4 ("Novieri", 2026-11-23) is the design authority: its YAML
 * frontmatter names every token by its custom-property name without the
 * dashes. The FIRST `:root` of `src/index.css` is the transcription the
 * console paints. `docs/design/marca/novieri-tokens.json` is the brand the
 * operator chose (direction A, "Cordillera"): every value in it has to reach
 * the console exactly, under the token that does its job.
 *
 * The v3 prototype (`docs/design/itms-prototype-v3.html`) was the authority
 * from 2026-09-07 to 2026-11-23 and is history now; nothing here reads it.
 *
 * ONLY THE FIRST `:root` of index.css. The second is the legacy-alias block
 * (`--red: var(--sev-crit)`), a compatibility surface: an alias is a
 * reference, not a colour, and "invents no colour" below keeps it that way.
 * Values are compared without whitespace and case, so a formatter cannot
 * fail this; `.07` is still not `0.07`, because that is a change.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const REPO = resolve(import.meta.dirname, "../../..");
const DESIGN_MD = resolve(REPO, "DESIGN.md");
const BRAND_JSON = resolve(REPO, "docs/design/marca/novieri-tokens.json");
const APP_CSS = resolve(REPO, "frontend/src/index.css");

/** Pull the FIRST `:root { ... }` block out of a stylesheet and parse its declarations. */
function rootTokens(path: string): Map<string, string> {
  const source = readFileSync(path, "utf8");
  const start = source.indexOf(":root {");
  if (start === -1) throw new Error(`no ':root {' block in ${path}`);
  const end = source.indexOf("}", start);
  if (end === -1) throw new Error(`unterminated ':root' block in ${path}`);
  const tokens = new Map<string, string>();
  for (const line of source.slice(start + ":root {".length, end).split("\n")) {
    const declaration = line.trim().replace(/;$/, "");
    if (!declaration.startsWith("--")) continue;
    const colon = declaration.indexOf(":");
    if (colon === -1) continue;
    tokens.set(declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim().replace(/\s+/g, " "));
  }
  return tokens;
}

/** The YAML frontmatter of a markdown file, as lines. */
function frontmatter(markdown: string): string[] {
  const body = /^---\n([\s\S]*?)\n---/.exec(markdown)?.[1];
  if (body === undefined) throw new Error("DESIGN.md has no frontmatter");
  return body.split("\n");
}

/** One flat frontmatter block (`colors:`, `rounded:`): key -> value, quotes removed. */
export function frontmatterBlock(markdown: string, block: string): Map<string, string> {
  const out = new Map<string, string>();
  let inside = false;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === `${block}:`;
      continue;
    }
    if (!inside) continue;
    const match = /^ {2}([a-z0-9-]+):\s*"?([^"]*?)"?\s*$/.exec(line);
    if (match) out.set(match[1] as string, (match[2] as string).trim());
  }
  return out;
}

/** `typography.<role>.fontSize` for every role. */
function fontSizes(markdown: string): Map<string, string> {
  const out = new Map<string, string>();
  let inside = false;
  let role: string | null = null;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === "typography:";
      role = null;
      continue;
    }
    if (!inside) continue;
    const head = /^ {2}([a-z0-9-]+):\s*$/.exec(line);
    if (head) {
      role = head[1] as string;
      continue;
    }
    const size = /^ {4}fontSize:\s*(\S+)\s*$/.exec(line);
    if (size && role) out.set(role, size[1] as string);
  }
  return out;
}

/** `typography.code.fontFamily`: the code stack, which `--mono` transcribes. */
function codeFamily(markdown: string): string {
  let inside = false;
  let role: string | null = null;
  for (const line of frontmatter(markdown)) {
    if (/^\S/.test(line)) {
      inside = line.trim() === "typography:";
      role = null;
      continue;
    }
    if (!inside) continue;
    const head = /^ {2}([a-z0-9-]+):\s*$/.exec(line);
    if (head) {
      role = head[1] as string;
      continue;
    }
    const family = /^ {4}fontFamily:\s*(.+?)\s*$/.exec(line);
    if (family && role === "code") return family[1] as string;
  }
  throw new Error("DESIGN.md names no typography.code.fontFamily");
}

const same = (value: string) => value.toLowerCase().replace(/\s+/g, "");

/** A dotted path into the brand JSON, as a string. */
function brandValue(brand: unknown, path: string): string {
  let at: unknown = brand;
  for (const part of path.split(".")) {
    if (typeof at !== "object" || at === null || !(part in at)) throw new Error(`novieri-tokens.json has no ${path}`);
    at = (at as Record<string, unknown>)[part];
  }
  if (typeof at !== "string") throw new Error(`novieri-tokens.json ${path} is not a string`);
  return at;
}

/** Every `#rrggbb` under the brand's `color` subtree. */
function brandColours(node: unknown): string[] {
  if (typeof node === "string") return /^#[0-9a-f]{6}$/i.test(node) ? [node] : [];
  if (typeof node !== "object" || node === null) return [];
  return Object.values(node).flatMap(brandColours);
}

/**
 * WHERE EACH BRAND VALUE LANDS: the token that does the job the brand names.
 * The brand's `rail.activeBackground` equals its `rail.hover` and lands on the
 * same `--rail-2`; `severity.info` is the accent; `brand.*` are the logo's own
 * colours, which are the accent, the ink, the muted ink and white.
 */
const BRAND_MAP: ReadonlyArray<readonly [string, string]> = [
  ["--bg", "color.page.background"],
  ["--surface", "color.page.surface"],
  ["--surface-2", "color.page.surfaceHover"],
  ["--line", "color.page.border"],
  ["--line-control", "color.page.borderControl"],
  ["--ink", "color.page.text"],
  ["--muted", "color.page.textSecondary"],
  ["--rail", "color.rail.background"],
  ["--rail-2", "color.rail.hover"],
  ["--rail-2", "color.rail.activeBackground"],
  ["--rail-ink", "color.rail.text"],
  ["--rail-dim", "color.rail.textDim"],
  ["--rail-mark", "color.rail.activeMark"],
  ["--accent", "color.accent.base"],
  ["--accent", "color.accent.focusRing"],
  ["--accent", "color.severity.info.ink"],
  ["--accent-2", "color.accent.hover"],
  ["--accent-3", "color.accent.pressed"],
  ["--accent-s", "color.accent.wash"],
  ["--accent-s", "color.severity.info.wash"],
  ["--accent-ink", "color.accent.ink"],
  ["--sev-ok", "color.severity.ok.ink"],
  ["--sev-ok-s", "color.severity.ok.wash"],
  ["--sev-warn", "color.severity.warning.ink"],
  ["--sev-warn-s", "color.severity.warning.wash"],
  ["--sev-crit", "color.severity.critical.ink"],
  ["--sev-crit-s", "color.severity.critical.wash"],
  ["--sev-mut", "color.severity.unknown.ink"],
  ["--sev-mut-s", "color.severity.unknown.wash"],
  ["--r-ctl", "radius.control"],
  ["--r-ctl", "radius.chip"],
  ["--r-panel", "radius.panel"],
  ["--r-badge", "radius.badge"],
  ["--shadow", "shadow.resting"],
];

/** The brand writes a size with its weight ("22px/600") or its case ("10.5px uppercase 0.1em"). */
const BRAND_SIZES: ReadonlyArray<readonly [string, string]> = [
  ["--t-h1", "type.h1"],
  ["--t-h2", "type.h2"],
  ["--t-body", "type.body"],
  ["--t-sm", "type.small"],
  ["--t-cap", "type.caption"],
  ["--t-kpi", "type.kpi"],
];

/** The ramp's roles in DESIGN.md, and the token each one is. */
const RAMP: Readonly<Record<string, string>> = {
  h1: "--t-h1",
  h2: "--t-h2",
  body: "--t-body",
  small: "--t-sm",
  caption: "--t-cap",
  kpi: "--t-kpi",
};

describe("design tokens", () => {
  const design = readFileSync(DESIGN_MD, "utf8");
  const colours = frontmatterBlock(design, "colors");
  const radii = frontmatterBlock(design, "rounded");
  const sizes = fontSizes(design);
  const app = rootTokens(APP_CSS);
  const brand: unknown = JSON.parse(readFileSync(BRAND_JSON, "utf8"));

  it("parsed all three files, so the comparisons below mean something", () => {
    // NON-VACUITY: an empty map equals an empty map.
    expect(colours.size).toBeGreaterThan(25);
    expect(colours.get("accent")).toBe("#2747c9");
    expect(radii.get("r-badge")).toBe("4px");
    expect(sizes.get("body")).toBe("13.5px");
    expect(app.get("--accent")).toBe("#2747c9");
    expect(brandValue(brand, "color.accent.base")).toBe("#2747c9");
  });

  it("paints every colour DESIGN.md names, with DESIGN.md's value", () => {
    const drifted = [...colours]
      .filter(([name, value]) => same(app.get(`--${name}`) ?? "(missing)") !== same(value))
      .map(([name, value]) => `--${name}: DESIGN.md=${value} index.css=${app.get(`--${name}`) ?? "(missing)"}`);
    expect(drifted, drifted.join(" | ")).toEqual([]);
  });

  it("invents no colour DESIGN.md does not name", () => {
    const invented = [...app]
      .filter(([, value]) => /^#|^rgb|^hsl/i.test(value))
      .filter(([name]) => !colours.has(name.slice(2)))
      .map(([name, value]) => `${name}: ${value}`);
    expect(invented, `colours not in DESIGN.md: ${invented.join(", ")}`).toEqual([]);
  });

  it("declares the radius ladder DESIGN.md names", () => {
    expect(radii.size).toBe(4);
    for (const [name, value] of radii) expect(app.get(`--${name}`), `--${name}`).toBe(value);
  });

  it.each(Object.entries(RAMP))("declares the %s rung DESIGN.md names (%s)", (role, token) => {
    expect(app.get(token)).toBe(sizes.get(role));
  });

  it("keeps the two type stacks: Inter first, and the code stack DESIGN.md names", () => {
    // Kept from the v3 test, which asserted `--sans` starts with Inter: the
    // brand changed no face, and `fonts.test.ts` checks the @font-face, not
    // the stack a rule reads.
    expect(app.get("--sans")?.startsWith("Inter,")).toBe(true);
    expect(same(app.get("--mono") ?? "(missing)")).toBe(same(codeFamily(design)));
  });

  it.each(BRAND_MAP)("carries the brand's value in %s (%s)", (token, path) => {
    expect(same(app.get(token) ?? "(missing)")).toBe(same(brandValue(brand, path)));
  });

  it.each(BRAND_SIZES)("carries the brand's size in %s (%s)", (token, path) => {
    const size = brandValue(brand, path).split(/[/ ]/)[0];
    expect(app.get(token)).toBe(size);
  });

  it("puts every colour of the brand somewhere in the palette", () => {
    const values = new Set([...app.values()].map(same));
    const missing = brandColours((brand as { color: unknown }).color).filter((colour) => !values.has(same(colour)));
    expect(missing, `brand colours the console never paints: ${missing.join(", ")}`).toEqual([]);
  });

  it("keeps the four retired tones as aliases of the brand's, never as colours (DESIGN.md v4, D2)", () => {
    const css = readFileSync(APP_CSS, "utf8");
    const retired: ReadonlyArray<readonly [string, string]> = [
      ["--ink-2", "--ink"],
      ["--line-2", "--line"],
      ["--sev-run", "--accent"],
      ["--sev-run-s", "--accent-s"],
    ];
    for (const [alias, target] of retired) {
      expect(app.has(alias), `${alias} is back in the first :root`).toBe(false);
      expect(css).toMatch(new RegExp(`${alias}: var\\(${target}\\);`));
    }
    // D3: the rail's alert count is white on the critical badge now.
    expect(css).not.toContain("--rail-hot");
  });

  it("commits to the light colour scheme rather than claiming dark support", () => {
    const css = readFileSync(APP_CSS, "utf8");
    expect(css).toContain("color-scheme: light;");
    expect(css).not.toContain("color-scheme: light dark");
  });

  it("fetches no font or stylesheet from a third party", () => {
    const css = readFileSync(APP_CSS, "utf8");
    const external = css.match(/@import\s+url\(|fonts\.googleapis|fonts\.gstatic|https?:\/\//g);
    expect(external, `index.css reaches off-origin: ${external?.join(", ")}`).toBeNull();
  });
});
```

`frontend/src/test/second-carrier.test.tsx` (nuevo; decisión D2 y la confirmación del operador del 2026-11-24):

```tsx
/**
 * "RUNNING" AND "INFORMATION" SHARE THE COBALT (DESIGN.md v4, D2; the
 * operator confirmed the merge on 2026-11-24), so wherever the two are drawn
 * without a badge's icon, something other than the hue must tell them apart
 * (The Second Carrier Rule; WCAG 1.4.1). The four places are the ones the
 * pre-flight review named (I-3): a chart's series, the heat strip (IPAM's
 * address grid: DHCP is "info", an assigned address "run"), the donut and the
 * timeline bar.
 */
import { render } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { Donut, Timeline } from "../shell/viz";

const SRC = resolve(import.meta.dirname, "..");
const read = (path: string) => readFileSync(resolve(SRC, path), "utf8");

/** Every `--x: value;` of every `:root` in index.css, so an alias resolves to its paint. */
function tokens(): Map<string, string> {
  const out = new Map<string, string>();
  for (const block of read("index.css").matchAll(/:root\s*\{([^}]*)\}/g)) {
    for (const line of (block[1] as string).split("\n")) {
      const match = /^\s*(--[a-z0-9-]+):\s*(.+?);\s*$/.exec(line);
      if (match) out.set(match[1] as string, match[2] as string);
    }
  }
  return out;
}

/** `var(--sev-run)` -> `#2747c9`, following aliases. */
function paint(value: string, table: Map<string, string>): string {
  let at = value.trim();
  for (let hops = 0; hops < 5; hops += 1) {
    const ref = /^var\((--[a-z0-9-]+)\)$/.exec(at);
    if (!ref) return at.toLowerCase();
    at = (table.get(ref[1] as string) ?? "(missing)").trim();
  }
  throw new Error(`alias loop at ${value}`);
}

/** The declarations of one rule (`selector {`), without the trailing `;`. */
function rule(css: string, selector: string): string[] {
  const at = css.indexOf(`${selector} {`);
  if (at === -1) throw new Error(`no rule ${selector}`);
  return css
    .slice(css.indexOf("{", at) + 1, css.indexOf("}", at))
    .split(";")
    .map((declaration) => declaration.trim())
    .filter(Boolean);
}

describe("running and information, drawn without a badge (DESIGN.md v4, D2)", () => {
  const table = tokens();

  it("resolve to the same cobalt, which is why a second carrier is owed at all", () => {
    expect(paint("var(--sev-run)", table)).toBe(paint("var(--accent)", table));
  });

  it("gives every chart series its own paint, never the same one twice", () => {
    const inks =
      /const SERIES_INK = \[([^\]]*)\]/
        .exec(read("components/chart.tsx"))?.[1]
        ?.split(",")
        .map((ink) => ink.trim().replace(/^"|"$/g, "")) ?? [];
    expect(inks.length).toBeGreaterThanOrEqual(4);
    const paints = inks.map((ink) => paint(ink, table));
    expect(new Set(paints).size, paints.join(", ")).toBe(paints.length);
  });

  it("rings the information cell of a heat strip, so a DHCP address is not an assigned one by colour alone", () => {
    const css = read("shell/viz.css");
    const info = rule(css, ".heat .h-info");
    const run = rule(css, ".heat .h-run");
    const ground = (declarations: string[]) =>
      paint(declarations.find((d) => d.startsWith("background:"))?.slice("background:".length) ?? "", table);
    expect(ground(info)).toBe(ground(run));
    expect(info.some((d) => /^box-shadow:\s*inset\b/.test(d))).toBe(true);
    expect(run.some((d) => d.startsWith("box-shadow:"))).toBe(false);
  });

  it("puts the kind's icon beside a donut's label, and the two kinds' icons differ", () => {
    const icon = (kind: "run" | "info") =>
      render(<Donut pct={40} kind={kind} label="Backups" />).container.querySelector(".don-k svg")?.innerHTML;
    const running = icon("run");
    const information = icon("info");
    expect(running).toBeTruthy();
    expect(information).toBeTruthy();
    expect(running).not.toBe(information);
  });

  it("keeps the icon every timeline row already carries, for the same reason", () => {
    const { container } = render(
      <Timeline
        ticks={["00:00", "12:00"]}
        runs={[
          { at: "a", kind: "run", label: "Patch", start: 0, width: 50 },
          { at: "b", kind: "info", label: "Scan", start: 50, width: 50 },
        ]}
      />,
    );
    const icons = [...container.querySelectorAll(".tl-k svg")].map((svg) => svg.innerHTML);
    expect(icons).toHaveLength(2);
    expect(icons[0]).not.toBe(icons[1]);
  });
});
```

- [ ] **Step 3: Ejecutar las pruebas y verlas fallar**

Run: `cd frontend && npx vitest run src/test/tokens.test.ts src/test/second-carrier.test.tsx`
Expected: FAIL. En `tokens.test.ts`, entre otros: `parsed all three files…` (`colours.get("accent")` es `undefined`: el frontmatter de hoy usa otras claves) y `carries the brand's value in --bg` (`#f5f6f9` frente a `#f3f5f8`). En `second-carrier.test.tsx`: `resolve to the same cobalt` (hoy `--sev-run` es `#0f5f95`, no el acento), `gives every chart series its own paint` no falla todavía (hoy `--sev-run` y `--accent` son dos tonos) y vuelve a fallar en cuanto el Step 5 los une si el Step 6 no cambia `SERIES_INK`; `rings the information cell` y `puts the kind's icon beside a donut's label` fallan.

- [ ] **Step 4: Escribir `DESIGN.md` v4**

Reemplazar el contenido completo de `DESIGN.md`:

```markdown
---
name: Novieri RMM
description: One console for a managed fleet, a night-blue rail beside a cool, exact workspace.
colors:
  bg: "#f3f5f8"
  surface: "#ffffff"
  surface-2: "#f7f9fc"
  ink: "#111a2b"
  muted: "#536077"
  line: "#e1e6ee"
  line-control: "#7b8599"
  rail: "#101b30"
  rail-2: "#1b2945"
  rail-ink: "#a9b6cc"
  rail-dim: "#8592ab"
  rail-mark: "#8fa6ff"
  accent: "#2747c9"
  accent-2: "#203cac"
  accent-3: "#1a3190"
  accent-s: "#e9edfc"
  accent-ink: "#ffffff"
  sev-crit: "#b8172c"
  sev-crit-s: "#fcebed"
  sev-warn: "#8d5a00"
  sev-warn-s: "#fcf1dc"
  sev-ok: "#17703f"
  sev-ok-s: "#e5f4ea"
  sev-mut: "#586274"
  sev-mut-s: "#edf0f4"
  av-1: "#5b3fb0"
  av-2: "#0f5f95"
  av-3: "#17703f"
  av-4: "#9a6100"
  av-5: "#b03a5b"
  av-6: "#0f7a80"
  scrim: "rgba(16,27,48,.38)"
  hatch-ink: "rgba(255,255,255,.55)"
typography:
  h1:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: 600
    letterSpacing: "-0.015em"
  h2:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 13.5px
    fontWeight: 400
    lineHeight: "1.5"
  small:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: 400
  caption:
    fontFamily: Inter
    fontSize: 10.5px
    fontWeight: 500
    letterSpacing: "0.1em"
  kpi:
    fontFamily: Inter
    fontSize: 26px
    fontWeight: 600
    letterSpacing: "-0.03em"
  code:
    fontFamily: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace
    fontSize: 12px
    fontWeight: 400
rounded:
  r-badge: 4px
  r-ctl: 6px
  r-panel: 10px
  r-pill: 999px
spacing:
  rail: 232px
  rail-collapsed: 76px
  topbar: 60px
  content: 22px 24px 40px
  content-max: 1440px
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  button-primary-hover:
    backgroundColor: "{colors.accent-2}"
  button-primary-pressed:
    backgroundColor: "{colors.accent-3}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  input-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 36px
  chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.r-ctl}"
    height: 32px
  chip-selected:
    backgroundColor: "{colors.accent-s}"
    textColor: "{colors.accent}"
  badge:
    rounded: "{rounded.r-badge}"
    height: 24px
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.r-panel}"
  nav-item-active:
    backgroundColor: "{colors.rail-2}"
    textColor: "{colors.accent-ink}"
    rounded: "{rounded.r-ctl}"
  save-bar:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
---

# Design System: Novieri RMM

> **Version 4, "Novieri" (2026-11-23).** The tokens in the frontmatter are
> normative and named after the CSS custom properties that carry them
> (`accent` is `--accent`). `frontend/src/index.css`'s first `:root` is their
> transcription and `frontend/src/test/tokens.test.ts` fails on any
> difference, in either direction, and on any brand value from
> `docs/design/marca/novieri-tokens.json` that does not reach the console
> exactly. A rule the brand forces to change is changed here first, with its
> reason, and then in the tokens and the tests. The v3 prototype
> (`docs/design/itms-prototype-v3.html`, 2026-09-07) is history.

## Overview

**Creative North Star: "The Survey Plan"**

The brand the operator chose is "Cordillera": the sobriety of a topographic
plan. A night-blue rail runs down the left of every screen and carries the
legend, the places you can go, in the order a technician walks them. Everything
to its right is the plan itself: a cool white workspace, exact and dense, where
lines are thin and colour appears only where it means something. The symbol
is an N drawn as two peaks that also read as a monitoring pulse, and the
wordmark is built from straight strokes and arcs, flat ends, no serifs.

The organisation is borrowed on purpose. The rail, the per-client pages, the
filtered lists, the tabbed records and the "New" menu follow Atera, the
category's leader, so a technician who has used one console knows where things
are in this one. What is not borrowed is the look, and what this system
competes on is consistency, which is where the reference is weakest: one way to
save, every view addressable by URL, a unique tab title on every page, and an
empty state that says its real cause.

Density is a feature. This is a console for people who manage hundreds of
endpoints and keep it open all day; rows, chips and badges are packed onto the
page, and what keeps that from becoming noise is that colour is rationed.

**Key Characteristics:**
- A night-blue rail (232px, 76px folded) against a cool white workspace.
- One cobalt accent, which is the only colour that is not a severity or a grey.
- Five severities, each with a wash and an icon; information shares the accent.
- Badges at 4px, chips and controls at 6px, panels at 10px: nothing is a pill
  except the switch and the progress bar.
- Every ratio computed in CI and every page measured in a browser.

## Colors

Cool and exact: a night-blue rail, a white working surface on a pale blue-grey
ground, and one cobalt accent.

### Primary
- **Cobalt** (`#2747c9`, `--accent`): the primary button, links, the focus ring,
  the active tab's underline, the selected chip, the checked checkbox, the info
  badge, the symbol. 7.42:1 on white, 6.79:1 on the page.
- **Cobalt deep** (`#203cac`, `--accent-2`): a primary button on hover.
  **Cobalt pressed** (`#1a3190`, `--accent-3`): pressed.
- **Cobalt wash** (`#e9edfc`, `--accent-s`): the selected chip, the info badge.
  The accent on it is 6.35:1.

### Rail
- **Night blue** (`#101b30`, `--rail`) and **Night blue lifted** (`#1b2945`,
  `--rail-2`): the rail's ground, and the ground an item lifts to on hover and
  when it is the current page.
- **Rail text** (`#a9b6cc`, `--rail-ink`): items, 8.39:1 and 7.07:1 lifted.
- **Rail dim** (`#8592ab`, `--rail-dim`): counts, 5.48:1 and 4.62:1 lifted.
- **Rail mark** (`#8fa6ff`, `--rail-mark`): the current item's icon and dot,
  6.25:1 on the lifted ground.

### Neutral
- **Ink** (`#111a2b`, `--ink`): primary text, 17.41:1 on white.
- **Muted** (`#536077`, `--muted`): secondary text, captions, placeholders,
  breadcrumbs; 6.35:1 on white, 5.81:1 on the page, at least 5.44:1 on every
  wash.
- **Page** (`#f3f5f8`, `--bg`), **Surface** (`#ffffff`, `--surface`),
  **Surface hover** (`#f7f9fc`, `--surface-2`).
- **Line** (`#e1e6ee`, `--line`): decorative separation only, 1.25:1.
- **Control line** (`#7b8599`, `--line-control`): the boundary of anything you
  can operate, 3.71:1 on white and 3.40:1 on the page.

### Severities

| Kind | Icon | Ink | Wash | Ink on wash | Ink on white |
| --- | --- | --- | --- | --- | --- |
| crit | circle-x | `#b8172c` | `#fcebed` | 5.71:1 | 6.57:1 |
| warn | alert-triangle | `#8d5a00` | `#fcf1dc` | 5.22:1 | 5.84:1 |
| ok | circle-check | `#17703f` | `#e5f4ea` | 5.39:1 | 6.13:1 |
| mut (unknown) | minus | `#586274` | `#edf0f4` | 5.38:1 | 6.15:1 |
| info | info-circle | the accent | the accent wash | 6.35:1 | 7.42:1 |
| run | progress | the accent | the accent wash | 6.35:1 | 7.42:1 |

"Running" is not a sixth colour: it is information's hue with its own icon,
because the brand has one blue (the operator confirmed the merge on
2026-11-24). `--sev-run` is an alias of `--accent` for the code that still
names it. Where the two are drawn without a badge, the difference is carried
by something else, and `frontend/src/test/second-carrier.test.tsx` holds it:
a chart's second series is the unknown grey, not a second cobalt; a heat cell
for information is a ring (a cobalt core in a band of the surface), running a
solid cell; a donut's label carries its kind's icon; a timeline row carries
its kind's icon already.

The avatar palette (`--av-1..6`) tells people apart; it is not a status, and
white initials clear 5.10:1 on every fill.

### Named Rules

**The One Accent Rule.** Cobalt is the only colour on the page that does not
mean a severity. A control that navigates, reveals or commits is cobalt;
nothing else is.

**The Rationed Hue Rule.** If a hue could be swapped for grey without losing
meaning, it is grey.

**The Second Carrier Rule.** Colour is never the only carrier: every badge
carries its icon, the current rail item its lifted ground and white text.

**The Two Grounds Rule.** Cobalt measures 2.32:1 on the rail and is never drawn
there; the rail has its own mark, `--rail-mark`.

**The Absent Zero Rule.** Unmeasured is drawn as absent: `n/a`, no bar, a
hollow pip, a chip with no count. Never as `0`.

## Typography

**Inter, self-hosted, at 400, 500 and 600.** Nothing is fetched from a CDN:
the platform does not phone home and many deployments have no egress.

### Hierarchy
- **H1** (`--t-h1`, 22px, 600, -0.015em): the page title, one per page.
- **H2** (`--t-h2`, 15px, 600): a panel or section heading.
- **Body** (`--t-body`, 13.5px, 400, 1.5): everything read, the rail's items,
  buttons, fields, cells.
- **Small** (`--t-sm`, 12px): sub-lines, badges, counts, breadcrumbs, code.
- **Caption** (`--t-cap`, 10.5px, 500, uppercase by CSS, 0.1em): table headers,
  tile labels, fact labels. The only uppercase text, never typed.
- **KPI** (`--t-kpi`, 26px, 600, -0.03em): the figure on a tile.

Tailwind's `text-xs..2xl` resolve to Small, Body, Body, H2, H1 and KPI.

### Named Rules

**The Floor Is The Ramp's Smallest Step.** Nothing renders below 10.5px.

**The Label Leads Rule.** A tile's caption sits above its figure.

## Layout

A 232px rail (76px folded, icons only), a 60px top bar and a fluid workspace
padded `22px 24px 40px`, capped at 1440px; where the Assistant's round button
is drawn the workspace keeps 88px at its foot instead of 40px, so the last
line of a page scrolls clear of it. Breakpoints: **1300px** (tile grids
relax), **1024px** (the rail becomes a drawer, out of the tab order while
shut), **640px** (the client selector moves into the drawer, tiles go full
width). The breadcrumb is the first line of the content. Touch targets reach
44px on coarse pointers.

## Elevation & Depth

Almost flat. Depth is a white surface on the page, a 1px line around it and one
soft shadow, `0 1px 2px rgba(16,27,48,.05), 0 6px 20px rgba(16,27,48,.06)`,
tinted by the rail's own blue. There is no elevation scale: a menu, a dialog
and a panel wear the same shadow, and a dialog is set apart by the scrim.

**The Flat-Until-Touched Rule.** A hover changes the ground or the border; a
border-only hover steps at least 2:1.

## Shapes

The ladder shrinks toward the thing you read: **panels 10px** (`--r-panel`),
**controls and chips 6px** (`--r-ctl`), **badges 4px** (`--r-badge`). The
switch's track and the progress bar keep `--r-pill`, because their silhouette
is how they are recognised; nothing else is round. Avatars and pips are 50%;
heat cells 2px and 3px. Borders are always 1px, told apart by colour: a line
closes a box, the control line says it can be operated.

## Components

### Buttons
- 36px high, 6px radius, 13.5px/500, `0 13px`.
- **Primary:** cobalt fill, white text; deep on hover, pressed on press.
- **Secondary:** white, control line, ink text.
- **Ghost:** no edge, cobalt text. **Danger:** critical edge and text.
- **Focus:** 2px cobalt outline at a 2px offset, which is what keeps the ring
  off the primary button's own fill.
- **One primary per page body.** In the chrome, the "New" menu is the one
  primary control (it replaced "Add a computer" on 2026-11-23).

### Chips (filters)
- 32px high, 6px radius, control line, ink text at 13.5px/500, an optional
  count at 12px in the muted ink. **Selected:** cobalt wash, cobalt edge and
  text, and the count in cobalt. A filter chip is a toggle (`aria-pressed`) and
  its state lives in the URL.

### Badges (status)
- 24px high, 4px radius, 12px/500, the kind's wash and ink, the kind's icon
  first. Six kinds: crit, warn, ok, mut, info, run.

### Inputs / Fields
- 36px, 6px radius, white, control line; focus moves the border to cobalt.
  Help text sits outside the label (`aria-describedby`). Checkboxes and radios
  are native, tinted by `accent-color`.
- **Switch:** a pill track (control line off, cobalt on) and a white knob.

### Panels
- White, 1px line, 10px radius, the shadow. A head with an H2, a muted line
  and actions; a body at `16px 18px`, or bare around a list.

### Lists
- Page head (title, one sentence, the primary action), then one bare panel:
  search, filter chips with counts, the client chip when every client is
  shown, "Showing x of y" at the end of the toolbar; the bulk bar only where
  bulk actions exist; the table; the pager. An empty list says whether nothing
  exists yet or nothing matches the filters.

### Records (detail)
- Back, the record's name, its status badge, its context (client and site),
  actions on the right; summary tiles; one row of tabs that change the URL;
  for a device, information on the left and metrics and activity on the right.

### Forms
- Full page, back and title; collapsible sections; one sticky bar, "Unsaved
  changes · Discard · Save", for the whole console. Leaving with unsaved
  changes asks first.

### Dialogs and drawers
- Centred, 10px radius, the shadow, the scrim; focus enters, is trapped and
  returns; Escape closes.

### Navigation
- **Rail, in Atera's order, with no group captions:** Dashboard, Tickets,
  Clients, Devices, Alerts, Patching, Software, Backup, Remote sessions, Assets
  (Licenses, Subscriptions, IP addresses), IT documentation, Reports, More
  (Changes, Continuity tests, IT budget, only when on for the chosen client).
  Anchored at the bottom: Assistant, and Admin, which swaps the rail for an
  accordion of its sections with a back button and a search.
- **The Assistant is also a round button at the bottom right of every page**
  (Atera's AI button, `docs/design/referencias-atera/atera 1.png`; the
  operator, 2026-11-24), never in the top bar. See "Assistant button".
- Items at Body in rail text with an outline icon; **current**: lifted ground,
  white text, the icon and a 7px dot in rail mark. Counts in rail dim; the
  alert count is a critical badge with white text.
- Folded: icons only. Below 1024px: a drawer.

### Top bar
- 60px, white, a line below: the "New" menu (only real destinations, each
  shown with its permission), the search (`/`, wider than before: up to
  640px), the client selector, the alert glyph, the platform glyph and the
  account. No bell: there is no inbox behind one. No Assistant icon: the
  Assistant is anchored in the rail and is the round button below.

### Assistant button
- A 48px cobalt circle with the white sparkles glyph, fixed 24px from the
  bottom right of the viewport (16px on a phone), the one shadow; a link to
  `/assistant` named "Ask the Assistant", shown only with `ai.use` and not on
  the Assistant's own page. 48px clears the 44px target on every pointer.
- **It never covers the save bar.** Atera's walkthrough files exactly that
  defect ("el botón flotante de IA tapa el botón de guardar"), so while the
  button is drawn the save bar keeps 72px clear on its right, wherever the
  bar is: the two cannot share the corner at any width.

### Brand
- **Wordmark:** "NOVIERI", built from strokes and arcs (`docs/design/marca/
  novieri-wordmark.svg`), white on the rail, ink on light grounds.
- **Symbol:** the zigzag N; on the rail and as the favicon it sits on a 7px
  cobalt tile with a white stroke (`novieri-favicon.svg`, read at 16 and 32px).
- **Lockup:** wordmark, a hairline and "RMM" in the muted ink, for the sign-in
  screens, the portal and emails (`novieri-lockup.svg`).
- **Name:** "Novieri" in the console's bar; "Page · Novieri RMM" in the tab.

### Icons
Tabler, outline, stroke 1.75, from the sprite; never a hand-drawn path except
the brand's own symbol and wordmark.

## Do's and Don'ts

### Do:
- **Do** change a token here first, then in `index.css`, then in the tests.
- **Do** compute every new pair with `frontend/src/test/contrast.test.ts` and
  add it to the table its floor belongs to (4.5:1 text, 3:1 boundary).
- **Do** build every list, record and form from the kit
  (`frontend/src/shell/listKit.tsx`, `detailKit.tsx`, `formKit.tsx`).
- **Do** keep filters, tabs and create dialogs in the URL.
- **Do** run `node scripts/harmony.mjs` on the routes you touched, in both
  languages.

### Don't:
- **Don't** draw cobalt on the rail, or a second accent anywhere.
- **Don't** make a chip or a badge a pill.
- **Don't** write a font size, radius or colour as a literal.
- **Don't** type uppercase copy or an em dash into UI text.
- **Don't** ship a dead control: no bell, no "New" entry without a real
  destination.
- **Don't** draw a second save button where the save bar belongs.
- **Don't** let a floating control cover the save bar or a page's last line.
- **Don't** widen a guard to make a colour pass.
```

- [ ] **Step 5: Transcribir los tokens a `index.css`**

En `frontend/src/index.css`, reemplazar el comentario de cabecera (líneas 4 a 51) por este y el primer `:root` (líneas 52 a 103) por el bloque siguiente:

```css
/* Novieri RMM design tokens.
 *
 * TRANSCRIBED, NOT CHOSEN. Every value in the FIRST `:root` below is copied
 * from the frontmatter of `DESIGN.md` v4 ("Novieri", 2026-11-23), which names
 * each token after this property, and the brand subset of it from
 * `docs/design/marca/novieri-tokens.json` (direction A, "Cordillera", chosen
 * by the operator on 2026-11-23). `src/test/tokens.test.ts` reads all three
 * files and fails on any difference, so a deliberate change starts in
 * DESIGN.md. The ratios live in DESIGN.md and are computed by
 * `src/test/contrast.test.ts`; none is quoted here, because a quoted number
 * rots.
 *
 * THE SHAPE. Five type sizes and the KPI figure; four radii (badge, control,
 * panel, and the pill kept for the switch and the progress bar); one palette:
 * a light page (`--bg`, `--surface`, `--surface-2`), two inks (`--ink`,
 * `--muted`), one decorative line (`--line`) and one control boundary
 * (`--line-control`), a night-blue rail with its two grounds, two text tones
 * and its own mark, ONE accent with its hover, pressed and wash, and four
 * severity pairs (information shares the accent). The harmony gate
 * (`scripts/harmony.mjs`) reads this file at runtime as its allowlist.
 *
 * `--line-control` IS THE BOUNDARY THAT IDENTIFIES A CONTROL (WCAG 2.2 SC
 * 1.4.11, 3:1 against every surface a control can land on); `--line` is
 * decoration only, and `contrast.test.ts` asserts it never draws a control.
 *
 * LIGHT ONLY, DELIBERATELY: no `prefers-color-scheme`, no `[data-theme]`
 * (2026-08-16 UI port spec §3.1; `no-dark-variants.test.ts`).
 *
 * INTER IS SELF-HOSTED (`./fonts.css`): nothing is fetched off-origin
 * (`docs/product.md` §5).
 */
:root {
  --t-h1: 22px;
  --t-h2: 15px;
  --t-body: 13.5px;
  --t-sm: 12px;
  --t-cap: 10.5px;
  --t-kpi: 26px;
  --r-badge: 4px;
  --r-ctl: 6px;
  --r-panel: 10px;
  --r-pill: 999px;
  --bg: #f3f5f8;
  --surface: #ffffff;
  --surface-2: #f7f9fc;
  --ink: #111a2b;
  --muted: #536077;
  --line: #e1e6ee;
  --line-control: #7b8599;
  --rail: #101b30;
  --rail-2: #1b2945;
  --rail-ink: #a9b6cc;
  --rail-dim: #8592ab;
  --rail-mark: #8fa6ff;
  --accent: #2747c9;
  --accent-2: #203cac;
  --accent-3: #1a3190;
  --accent-s: #e9edfc;
  --accent-ink: #ffffff;
  --sev-crit: #b8172c;
  --sev-crit-s: #fcebed;
  --sev-warn: #8d5a00;
  --sev-warn-s: #fcf1dc;
  --sev-ok: #17703f;
  --sev-ok-s: #e5f4ea;
  --sev-mut: #586274;
  --sev-mut-s: #edf0f4;
  --av-1: #5b3fb0;
  --av-2: #0f5f95;
  --av-3: #17703f;
  --av-4: #9a6100;
  --av-5: #b03a5b;
  --av-6: #0f7a80;
  --scrim: rgba(16,27,48,.38);
  --hatch-ink: rgba(255,255,255,.55);
  --shadow: 0 1px 2px rgba(16,27,48,.05),0 6px 20px rgba(16,27,48,.06);
  --mono: ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --sans: Inter,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
  color-scheme: light;
  font-family: var(--sans);
}
```

En el bloque de alias (el segundo `:root`), añadir estas cuatro líneas al principio, con su comentario:

```css
  /* RETIRED BY THE BRAND (DESIGN.md v4, decision D2): Novieri has two text
     tones, one line and information in the accent's hue. The four names stay
     so the ~70 rules and modules that read them keep resolving. */
  --ink-2: var(--ink);
  --line-2: var(--line);
  --sev-run: var(--accent);
  --sev-run-s: var(--accent-s);
```

En `@theme inline`: borrar la línea `--color-rail-hot: var(--rail-hot);`; añadir después de `--color-rail-dim: var(--rail-dim);` la línea `--color-rail-mark: var(--rail-mark);`; y en el bloque de radios, antes de `--radius-sm`, la línea `--radius-xs: var(--r-badge);`.

- [ ] **Step 6: Las reglas que leían lo que cambia**

`frontend/src/shell/shell.css`, la regla `.sidebar .ct.hot` (línea 148) y su comentario (139-140):

```css
/* The rail's alert count (DESIGN.md v4, "Navigation"; decision D3): white on
 * the critical badge. It was `--rail-hot` text on the bare rail. */
.sidebar .ct.hot {
  color: var(--accent-ink);
  background: var(--sev-crit);
  border-radius: var(--r-badge);
  padding: 2px 6px;
  font-family: var(--sans);
  font-weight: 600;
}
```

`frontend/src/shell/shell.css`, en `.counter-dot` (líneas 350-366): `background: var(--accent);` → `background: var(--sev-crit);` y `border-radius: var(--r-pill);` → `border-radius: var(--r-badge);`.

`frontend/src/shell/components.css`: en `.pill` (línea 402) `border-radius: var(--r-pill);` → `border-radius: var(--r-badge);`; en `.chip` (línea 649) `border-radius: var(--r-pill);` → `border-radius: var(--r-ctl);`.

**El segundo portador de «en curso» frente a «información»** (D2; lo fija `second-carrier.test.tsx`):

`frontend/src/components/chart.tsx`, la línea 112 y su comentario (109-111):

```tsx
/* One accent and the severity inks as series inks, each also with a distinct
 * hatch, so a fill is never the only thing telling two bars apart. Series 2
 * WAS `--sev-run`, which is the accent itself since DESIGN.md v4 (D2): two
 * cobalt bars told apart by their hatch alone. It is the unknown grey now
 * (6.15:1 on white), the Rationed Hue Rule's own answer. */
const SERIES_INK = ["var(--accent)", "var(--sev-mut)", "var(--sev-warn)", "var(--sev-ok)"];
```

`frontend/src/shell/viz.css`, la regla `.heat .h-info` (línea 114) pasa a:

```css
/* Information shares running's cobalt (DESIGN.md v4, D2), so its cell is a
 * ring: a cobalt core inside a band of the surface. IPAM's address grid paints
 * a DHCP address "info" and an assigned one "run". */
.heat .h-info { background: var(--accent); opacity: 1; box-shadow: inset 0 0 0 2px var(--surface);
}
```

y la regla `.don-k` (línea 142) pasa a `.don-k { display: inline-flex; align-items: center; gap: 5px; font-weight: 600; margin-top: 4px;` (con su `}` en la línea siguiente, como las demás).

`frontend/src/shell/viz.tsx`, en `Donut`, la línea `<div className="don-k">{label}</div>` pasa a:

```tsx
      {/* THE KIND'S ICON BESIDE THE LABEL (D2): running and information are
          one cobalt now, and a donut has no badge to carry the difference. */}
      <div className="don-k">
        <Icon name={KIND_ICON[shown]} size={13} />
        {label}
      </div>
```

Las barras de la línea de tiempo no cambian: cada fila ya dibuja el icono de su tipo (`viz.tsx:345`), y la prueba lo fija.

- [ ] **Step 7: Las puertas de contraste y de radios**

`frontend/src/test/contrast.test.ts`:

1. En `TEXT_ON_BACKGROUND`, añadir al final:
   ```ts
     // DESIGN.md v4: the alert count, white on the critical badge (the rail's
     // `.ct.hot` and the top bar's `.counter-dot`), and the primary button's
     // label on its hover and pressed grounds.
     ["--accent-ink", "--sev-crit", ".sidebar .ct.hot, .counter-dot"],
     ["--accent-ink", "--accent-2", ".btn.primary:hover"],
     ["--accent-ink", "--accent-3", ".btn.primary:active"],
   ```
   y cambiar los textos de las filas de `--ink-2` y `--sev-run` para que digan que son alias (`"…, now --ink (DESIGN.md v4, D2)"`, `"…, information's hue with the progress icon (D2)"`); los pares no cambian.
2. En `TEXT_ON_RAIL`, sustituir las dos filas de `--rail-hot` por:
   ```ts
     ["--rail-mark", "--rail-2", ".nav-item.active svg and .nav-dot: the current item's mark"],
     ["--rail-mark", "--rail", "the same mark on a folder heading that holds the current page"],
   ```
3. En «keeps the hairlines and the rail tones off the light-surface text list», después de `expect(contrast(colour("--accent"), colour("--rail-2"))).toBeLessThan(4.5);` añadir:
   ```ts
     // DESIGN.md v4, "The Two Grounds Rule": cobalt is not even a mark on the
     // rail (2.32:1 on --rail, 1.95:1 lifted); --rail-mark is.
     expect(contrast(colour("--accent"), colour("--rail"))).toBeLessThan(3);
     expect(contrast(colour("--accent"), colour("--rail-2"))).toBeLessThan(3);
   ```
4. En `NON_TEXT`, después de la fila de `--accent-2`, añadir `["--accent-3", "a pressed primary button's ground and edge"],`, y después de la fila de `--ink-2`, añadir `["--ink", "the same edge: --ink-2 resolves to --ink since DESIGN.md v4 (D2)"],`. Sin esta segunda fila, «names every scanned boundary in the table above» falla con `components.css .btn:hover border-color -> --ink-2 #111a2b`: la tabla compara el token al que resuelve el alias (comprobado en una copia de trabajo el 2026-11-23).

`frontend/src/test/radius.test.ts`: en `LADDER` añadir `"--r-badge": "4px",` como primera entrada; en `SCALE` añadir `"--radius-xs": "--r-badge",` como primera entrada; en el comentario de cabecera, sustituir «a radius ladder of exactly three rungs -- control 6px, panel 10px, pill 999px» por «a radius ladder of four rungs (DESIGN.md v4) -- badge 4px, control 6px, panel 10px, and the pill kept for the switch and the progress bar».

- [ ] **Step 8: Ejecutar las puertas**

Run: `cd frontend && npx vitest run src/test/tokens.test.ts src/test/contrast.test.ts src/test/radius.test.ts src/test/no-dark-variants.test.ts src/test/jsx-tokens.test.ts src/test/base-styles.test.ts src/test/kit-review-fixes.test.tsx`
Expected: PASS en todas. Si `contrast.test.ts` informa un par por debajo del mínimo, **no** se baja el mínimo: se mira qué regla lo pinta y se corrige la regla (Global Constraints).

- [ ] **Step 9: Regenerar el sidecar `.impeccable/design.json`**

`reference/document.md` §4b: el sidecar se regenera siempre que se regenera `DESIGN.md`. Reemplazar el contenido completo de `.impeccable/design.json`:

```json
{
  "schemaVersion": 2,
  "generatedAt": "2026-11-23T12:00:00Z",
  "title": "Design System: Novieri RMM",
  "extensions": {
    "colorMeta": {
      "bg": { "role": "neutral", "displayName": "Page", "canonical": "#f3f5f8", "purpose": "The ground behind every panel." },
      "surface": { "role": "neutral", "displayName": "Surface", "canonical": "#ffffff", "purpose": "Panels, rows, fields, the top bar." },
      "surface-2": { "role": "neutral", "displayName": "Surface hover", "canonical": "#f7f9fc", "purpose": "A hovered row." },
      "ink": { "role": "neutral", "displayName": "Ink", "canonical": "#111a2b", "purpose": "Primary text, 17.41:1 on white." },
      "muted": { "role": "neutral", "displayName": "Muted ink", "canonical": "#536077", "purpose": "Secondary text, captions, placeholders; 6.35:1 on white." },
      "line": { "role": "neutral", "displayName": "Line", "canonical": "#e1e6ee", "purpose": "Decorative separation only." },
      "line-control": { "role": "neutral", "displayName": "Control line", "canonical": "#7b8599", "purpose": "The boundary of anything operable, 3.71:1 on white." },
      "rail": { "role": "tertiary", "displayName": "Night blue", "canonical": "#101b30", "purpose": "The rail's ground." },
      "rail-2": { "role": "tertiary", "displayName": "Night blue lifted", "canonical": "#1b2945", "purpose": "A rail item on hover, and the current one." },
      "rail-ink": { "role": "neutral", "displayName": "Rail text", "canonical": "#a9b6cc", "purpose": "Rail items, 8.39:1." },
      "rail-dim": { "role": "neutral", "displayName": "Rail dim", "canonical": "#8592ab", "purpose": "Rail counts, 5.48:1." },
      "rail-mark": { "role": "primary", "displayName": "Rail mark", "canonical": "#8fa6ff", "purpose": "The current item's icon and dot, 6.25:1 lifted." },
      "accent": { "role": "primary", "displayName": "Cobalt", "canonical": "#2747c9", "purpose": "The one accent: primary action, links, focus, selected chip, info." },
      "accent-2": { "role": "primary", "displayName": "Cobalt deep", "canonical": "#203cac", "purpose": "Primary button on hover." },
      "accent-3": { "role": "primary", "displayName": "Cobalt pressed", "canonical": "#1a3190", "purpose": "Primary button pressed." },
      "accent-s": { "role": "primary", "displayName": "Cobalt wash", "canonical": "#e9edfc", "purpose": "Selected chip, info badge." },
      "accent-ink": { "role": "primary", "displayName": "On cobalt", "canonical": "#ffffff", "purpose": "Text on a cobalt or critical fill." },
      "sev-crit": { "role": "status", "displayName": "Critical", "canonical": "#b8172c", "purpose": "Critical ink; the alert count's badge." },
      "sev-crit-s": { "role": "status", "displayName": "Critical wash", "canonical": "#fcebed", "purpose": "Critical badge and notice ground." },
      "sev-warn": { "role": "status", "displayName": "Warning", "canonical": "#8d5a00", "purpose": "Warning ink." },
      "sev-warn-s": { "role": "status", "displayName": "Warning wash", "canonical": "#fcf1dc", "purpose": "Warning badge and notice ground." },
      "sev-ok": { "role": "status", "displayName": "OK", "canonical": "#17703f", "purpose": "OK ink." },
      "sev-ok-s": { "role": "status", "displayName": "OK wash", "canonical": "#e5f4ea", "purpose": "OK badge ground." },
      "sev-mut": { "role": "status", "displayName": "Unknown", "canonical": "#586274", "purpose": "Unknown or not measured." },
      "sev-mut-s": { "role": "status", "displayName": "Unknown wash", "canonical": "#edf0f4", "purpose": "Unknown badge ground." },
      "av-1": { "role": "neutral", "displayName": "Avatar 1", "canonical": "#5b3fb0", "purpose": "Telling people apart; never a status." },
      "av-2": { "role": "neutral", "displayName": "Avatar 2", "canonical": "#0f5f95", "purpose": "Telling people apart; never a status." },
      "av-3": { "role": "neutral", "displayName": "Avatar 3", "canonical": "#17703f", "purpose": "Telling people apart; never a status." },
      "av-4": { "role": "neutral", "displayName": "Avatar 4", "canonical": "#9a6100", "purpose": "Telling people apart; never a status." },
      "av-5": { "role": "neutral", "displayName": "Avatar 5", "canonical": "#b03a5b", "purpose": "Telling people apart; never a status." },
      "av-6": { "role": "neutral", "displayName": "Avatar 6", "canonical": "#0f7a80", "purpose": "Telling people apart; never a status." },
      "scrim": { "role": "neutral", "displayName": "Scrim", "canonical": "rgba(16,27,48,.38)", "purpose": "Behind a dialog or the drawer." },
      "hatch-ink": { "role": "neutral", "displayName": "Hatch", "canonical": "rgba(255,255,255,.55)", "purpose": "The hatch over a heat cell that was not measured." }
    },
    "typographyMeta": {
      "h1": { "displayName": "Page title", "purpose": "One per page." },
      "h2": { "displayName": "Panel title", "purpose": "A panel or section heading." },
      "body": { "displayName": "Body", "purpose": "Everything read: rows, cells, fields, rail items." },
      "small": { "displayName": "Small", "purpose": "Sub-lines, badges, counts, code." },
      "caption": { "displayName": "Caption", "purpose": "Table headers and tile labels, uppercase by CSS." },
      "kpi": { "displayName": "KPI", "purpose": "A tile's figure." }
    },
    "shadows": [
      { "name": "resting", "value": "0 1px 2px rgba(16,27,48,.05), 0 6px 20px rgba(16,27,48,.06)", "purpose": "Every panel, tile, menu and dialog. The only shadow." }
    ],
    "motion": [
      { "name": "press", "value": "transform: translateY(1px) on :active", "purpose": "Buttons. Nothing animates at rest." }
    ],
    "breakpoints": [
      { "name": "tiles", "value": "1300px" },
      { "name": "rail-drawer", "value": "1024px" },
      { "name": "handset", "value": "640px" }
    ]
  },
  "components": [
    {
      "name": "Primary Button",
      "kind": "button",
      "refersTo": "button-primary",
      "description": "Cobalt fill, white label; deep on hover, pressed on press; the ring sits 2px outside the fill.",
      "html": "<button class=\"ds-btn ds-btn-primary\">Guardar</button>",
      "css": ".ds-btn{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 13px;font:500 13.5px/1 Inter,system-ui,sans-serif;border-radius:var(--r-ctl);border:1px solid var(--line-control);background:var(--surface);color:var(--ink);cursor:pointer;white-space:nowrap}.ds-btn-primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}.ds-btn-primary:hover{background:var(--accent-2);border-color:var(--accent-2)}.ds-btn-primary:active{background:var(--accent-3);border-color:var(--accent-3);transform:translateY(1px)}.ds-btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
    },
    {
      "name": "Secondary Button",
      "kind": "button",
      "refersTo": "button-secondary",
      "description": "White, control line, ink label.",
      "html": "<button class=\"ds-btn\">Exportar</button>",
      "css": ".ds-btn{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 13px;font:500 13.5px/1 Inter,system-ui,sans-serif;border-radius:var(--r-ctl);border:1px solid var(--line-control);background:var(--surface);color:var(--ink);cursor:pointer}.ds-btn:hover{background:var(--surface-2)}.ds-btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
    },
    {
      "name": "Filter Chip",
      "kind": "chip",
      "refersTo": "chip",
      "description": "A toggle whose state lives in the URL; selected is the cobalt wash.",
      "html": "<span class=\"ds-chips\"><button class=\"ds-chip\" aria-pressed=\"false\">Servidores<span class=\"ds-n\">12</span></button><button class=\"ds-chip on\" aria-pressed=\"true\">Todos<span class=\"ds-n\">248</span></button></span>",
      "css": ".ds-chips{display:flex;gap:8px}.ds-chip{display:inline-flex;align-items:center;gap:7px;height:32px;padding:0 11px;border-radius:var(--r-ctl);border:1px solid var(--line-control);background:var(--surface);color:var(--ink);font:500 13.5px/1 Inter,system-ui,sans-serif}.ds-n{font:500 12px/1 Inter,system-ui,sans-serif;color:var(--muted);font-variant-numeric:tabular-nums}.ds-chip.on{background:var(--accent-s);border-color:var(--accent);color:var(--accent)}.ds-chip.on .ds-n{color:var(--accent)}.ds-chip:focus-visible{outline:2px solid var(--accent);outline-offset:2px}"
    },
    {
      "name": "Status Badge",
      "kind": "custom",
      "refersTo": "badge",
      "description": "4px badge with the kind's icon first; colour is never the only carrier.",
      "html": "<span class=\"ds-badge crit\"><svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.75\"><circle cx=\"12\" cy=\"12\" r=\"9\"/><path d=\"M10 10l4 4m0-4l-4 4\"/></svg>Crítico</span>",
      "css": ".ds-badge{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 9px 0 7px;border-radius:var(--r-badge);font:500 12px/1 Inter,system-ui,sans-serif}.ds-badge.crit{background:var(--sev-crit-s);color:var(--sev-crit)}"
    },
    {
      "name": "Field",
      "kind": "input",
      "refersTo": "input-field",
      "description": "36px, control line, cobalt border on focus.",
      "html": "<input class=\"ds-input\" placeholder=\"Buscar equipos\">",
      "css": ".ds-input{height:36px;padding:0 10px;border:1px solid var(--line-control);border-radius:var(--r-ctl);background:var(--surface);color:var(--ink);font:400 13.5px/1.5 Inter,system-ui,sans-serif}.ds-input::placeholder{color:var(--muted)}.ds-input:focus{outline:none;border-color:var(--accent)}"
    },
    {
      "name": "Rail Item",
      "kind": "nav",
      "refersTo": "nav-item-active",
      "description": "Rail text at rest; the current item lifts, turns white and wears the rail mark.",
      "html": "<nav class=\"ds-rail\"><a class=\"ds-item\">Tickets</a><a class=\"ds-item on\" aria-current=\"page\">Equipos<i class=\"ds-dot\"></i></a></nav>",
      "css": ".ds-rail{background:var(--rail);padding:12px 0;width:232px}.ds-item{display:flex;align-items:center;gap:11px;height:38px;margin:0 10px;padding:0 12px;border-radius:var(--r-ctl);color:var(--rail-ink);font:400 13.5px/1 Inter,system-ui,sans-serif;text-decoration:none}.ds-item:hover{background:var(--rail-2);color:#fff}.ds-item.on{background:var(--rail-2);color:#fff;font-weight:500}.ds-dot{margin-left:auto;width:7px;height:7px;border-radius:50%;background:var(--rail-mark)}"
    },
    {
      "name": "Save Bar",
      "kind": "custom",
      "refersTo": "save-bar",
      "description": "The console's one way to save: sticky at the bottom while something changed.",
      "html": "<div class=\"ds-save\" role=\"region\" aria-label=\"Cambios sin guardar\"><span>Cambios sin guardar</span><button class=\"ds-btn\">Descartar</button><button class=\"ds-btn ds-btn-primary\">Guardar</button></div>",
      "css": ".ds-save{display:flex;align-items:center;gap:10px;padding:10px 16px;background:var(--surface);border:1px solid var(--line);border-radius:var(--r-panel);box-shadow:var(--shadow);font:500 13.5px/1.5 Inter,system-ui,sans-serif;color:var(--ink)}.ds-save span{margin-right:auto}.ds-btn{height:36px;padding:0 13px;border-radius:var(--r-ctl);border:1px solid var(--line-control);background:var(--surface);color:var(--ink)}.ds-btn-primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}"
    }
  ],
  "narrative": {
    "northStar": "The Survey Plan",
    "overview": "The sobriety of a topographic plan: a night-blue rail carries the legend in the order a technician walks it, and a cool white workspace carries the plan, exact and dense, with colour only where it means something. The organisation follows Atera so a technician knows where things are; the look is Novieri's, and the system competes on consistency: one way to save, every view addressable, a unique tab title, empty states that say their cause.",
    "keyCharacteristics": [
      "A night-blue rail (232px, 76px folded) against a cool white workspace.",
      "One cobalt accent; five severities with washes and icons; information shares the accent.",
      "Badges 4px, chips and controls 6px, panels 10px; only the switch and progress bar are pills.",
      "Every ratio computed in CI, every page measured in a browser in two languages."
    ],
    "rules": [
      { "name": "The One Accent Rule", "body": "Cobalt is the only colour that does not mean a severity.", "section": "colors" },
      { "name": "The Two Grounds Rule", "body": "Cobalt is never drawn on the rail; the rail has its own mark.", "section": "colors" },
      { "name": "The Second Carrier Rule", "body": "Every badge carries its icon; colour is never the only carrier.", "section": "colors" },
      { "name": "The Absent Zero Rule", "body": "Unmeasured is drawn as absent, never as 0.", "section": "colors" },
      { "name": "The Floor Is The Ramp's Smallest Step", "body": "Nothing renders below 10.5px.", "section": "typography" },
      { "name": "The Flat-Until-Touched Rule", "body": "Surfaces rest flat; a hover changes the ground or steps the border 2:1.", "section": "elevation" }
    ],
    "dos": [
      "Do change a token in DESIGN.md first, then index.css, then the tests.",
      "Do build every list, record and form from the kit.",
      "Do keep filters, tabs and create dialogs in the URL."
    ],
    "donts": [
      "Don't draw cobalt on the rail or add a second accent.",
      "Don't make a chip or a badge a pill.",
      "Don't ship a dead control or a second save button."
    ]
  }
}
```

- [ ] **Step 10: `docs/product.md` con el nombre elegido**

En `docs/product.md`: el título pasa a `# Product — what Novieri RMM is, and what it does`; la línea 30 («ITMS is a self-hosted IT management platform…») pasa a «Novieri RMM is a self-hosted IT management platform…» (son tres las líneas con «ITMS» al 2026-11-23, la 1, la 30 y la 240: `grep -n ITMS docs/product.md`); la sección §9 empieza así en lugar del párrafo «`ITMS` IS A PLACEHOLDER…» (líneas 240 a 247, hasta «…the size of that job then.»):

```markdown
**The product is Novieri RMM** (operator, 2026-11-23, decision A3 of the
console redesign): "Novieri" in the console's bar, "Page · Novieri RMM" in the
browser tab, "Novieri RMM" in emails and on managed computers. `ITMS` was a
placeholder from 2026-08-21 until then; the identity is direction A,
"Cordillera" (`docs/design/marca/`), and `DESIGN.md` v4 is its system.
`frontend/src/test/no-itms.test.ts` keeps the old name out of the console.
```

y la fila de `tokens.test.ts` en la tabla de §11 dice: `| frontend/src/test/tokens.test.ts | A palette that has drifted from DESIGN.md v4 or from the brand (docs/design/marca/novieri-tokens.json). A deliberate change starts in DESIGN.md. |`.

- [ ] **Step 11: Comprobar el contexto de impeccable**

Run (desde la raíz): `.claude/skills/impeccable/scripts/impeccable context`
Expected: la salida ya no contiene `NO_PRODUCT_MD` ni `PRODUCT_INIT_REQUIRED`, e incluye `# DESIGN.md` con `name: Novieri RMM`. Si añade `CONTEXT_STALE` por el sidecar, se informa y no se repara aparte (SKILL.md, «Never repair drift as a side effect»): este paso ya lo regeneró.

- [ ] **Step 12: La puerta de armonía falla cuando una página no se dibuja** (revisión previa, C1)

Hoy `harmony.mjs` recoge los errores de consola pero no los cuenta como fallo, y no exige el armazón: una página que el servidor de desarrollo no pudo servir no dibuja nada, un marco vacío no tiene ningún valor fuera de los tokens, y la pasada imprime `ok`. Reproducido el 2026-11-24 (`p3-fix-report.md`): con dos worktrees que compartían `node_modules/.vite`, `/licenses @ 1440x900` no dibujó la barra, con 262 módulos que nunca cargaron, y la pasada terminó en `ok` con código 0. Medido el mismo día sobre `main`: una pasada completa (117 rutas, 4 viewports) no tiene **ningún** error de consola, y las únicas diez rutas sin `.topbar` son las de acceso sin sesión; por eso las dos condiciones nuevas no ponen en rojo nada de hoy.

1. Prueba primero. `frontend/src/test/harmony-client.test.ts`: la importación de `../../scripts/harmony.mjs` pasa a `import { VIEWPORTS, chromeProblems, failedLoad, markDrawn, missingShell, servedError, viewportLabel } from "../../scripts/harmony.mjs";`, la de `../../scripts/routes.mjs` a `import { CHOSEN_CLIENT, FRAMELESS, PARAMS, ROUTES } from "../../scripts/routes.mjs";`, y al final del archivo:

```ts
/**
 * WHAT FAILS A RUN EVEN WHEN EVERY MEASUREMENT PASSES (redesign plan,
 * pre-flight C1, reproduced 2026-11-24): two Vite servers sharing one
 * `node_modules/.vite` served a page that never loaded, the page drew no
 * shell, and an empty frame measured clean, so harmony printed "ok".
 */
describe("what fails a harmony run even when every measurement passes (pre-flight C1)", () => {
  it("refuses a 5xx the dev server answered for a page file, and leaves the absent backend alone", () => {
    expect(servedError(504, "http://127.0.0.1:5173/node_modules/.vite/deps/react.js?v=1")).toBe(
      "the dev server answered 504 for /node_modules/.vite/deps/react.js",
    );
    expect(servedError(500, "http://127.0.0.1:5173/src/main.tsx")).toBe("the dev server answered 500 for /src/main.tsx");
    expect(servedError(502, "http://127.0.0.1:5173/api/v1/me")).toBeNull();
    expect(servedError(404, "http://127.0.0.1:5173/src/x.tsx")).toBeNull();
    expect(servedError(200, "http://127.0.0.1:5173/src/x.tsx")).toBeNull();
  });

  it("refuses a page file that never loaded, and leaves the absent backend alone", () => {
    expect(failedLoad("http://127.0.0.1:5173/src/lib/format.ts", "net::ERR_NETWORK_CHANGED")).toBe(
      "/src/lib/format.ts never loaded (net::ERR_NETWORK_CHANGED)",
    );
    expect(failedLoad("http://127.0.0.1:5173/api/v1/ws", "net::ERR_CONNECTION_REFUSED")).toBeNull();
  });

  it("refuses a console route that drew no top bar, and only a console route", () => {
    expect(missingShell("/devices", { toggle: null, profileRight: null, innerWidth: 1440, topbar: false })).toBe(true);
    expect(missingShell("/settings?tab=users", null)).toBe(true);
    expect(missingShell("/devices", { toggle: null, profileRight: 1420, innerWidth: 1440, topbar: true })).toBe(false);
    expect(missingShell("/sign-in", { toggle: null, profileRight: null, innerWidth: 1440, topbar: false })).toBe(false);
  });

  it("calls frameless only the session-less routes it walks (measured: the ten with no .topbar)", () => {
    expect(FRAMELESS.size).toBe(10);
    for (const route of FRAMELESS) expect(ROUTES).toContain(route);
    const sessionless = ["/forgot-password", "/reset-password", "/invite", "/recover", "/portal/sign-in"];
    expect([...FRAMELESS].filter((route) => !route.startsWith("/sign-in") && !sessionless.includes(route))).toEqual([]);
  });
});
```

Run: `cd frontend && npx vitest run src/test/harmony-client.test.ts`
Expected: FAIL (`failedLoad`, `missingShell`, `servedError` y `FRAMELESS` no existen).

2. `frontend/scripts/routes.mjs`, después de `export const OPEN_DISCLOSURES = new Set(["/settings?tab=users"]);`:

```js
/**
 * THE ROUTES THAT DRAW NO CONSOLE SHELL: the pages reached without a session
 * (own-auth Task 11), which have the sign-in frame instead. Every other route
 * draws the top bar, and harmony fails one that does not (`missingShell`,
 * redesign plan pre-flight C1): a page the dev server could not serve draws
 * nothing, and an empty frame measures clean. Measured 2026-11-24: these ten
 * are exactly the routes of a full walk with no `.topbar`.
 */
export const FRAMELESS = new Set([
  "/sign-in",
  "/sign-in/factor",
  "/sign-in/enroll",
  "/sign-in/recovery-codes",
  "/sign-in/microsoft",
  "/forgot-password",
  "/reset-password",
  "/invite",
  "/recover",
  "/portal/sign-in",
]);
```

y al final de `frontend/scripts/routes.d.mts`:

```ts
/** Routes with no console shell (no `.topbar`); every other route must draw one. */
export declare const FRAMELESS: Set<string>;
```

3. `frontend/scripts/harmony.mjs`, seis cambios:

   a. La importación de `./routes.mjs` pasa a `import { CHOSEN_CLIENT, FRAMELESS, OPEN_DISCLOSURES, ROUTES, patternOf } from "./routes.mjs";`.

   b. En el comentario de cabecera, las cinco líneas 39 a 43 (de ` * EXIT 1 on any stray value, any horizontal overflow, a top bar` a ` * miss. Without that flag a miss is`) pasan a:
   ```js
    * EXIT 1 on any stray value, any horizontal overflow, a top bar
    * `chromeProblems` refuses, any serious or critical axe violation, any route
    * that fails to load (a client viewport whose selector does not show the
    * client chosen counts as one, and so does a console route that draws no
    * shell: `missingShell`), any 5xx the dev server answers for one of the
    * page's own files and any page file that never loads (`servedError`,
    * `failedLoad`) -- and, with `--strict-fixtures`, any fixture miss.
    * Without that flag a miss is
   ```
   (las dos líneas que siguen, ` * reported and tolerated, which is the Phase A state: the gate runs and` y ` * names what each module still owes.`, no cambian).

   c. Justo antes del comentario `/** * Whether the viewport draws the client mark in the drawer toggle` (la función `markDrawn`):
   ```js
   /**
    * WHAT A RUN REFUSES EVEN WHEN EVERY MEASUREMENT PASSES (redesign plan,
    * pre-flight C1, reproduced 2026-11-24). Two harmony runs whose Vite servers
    * shared one `node_modules/.vite` rewrote it under each other; a page never
    * loaded, drew no shell, measured clean, and the run printed "ok". An empty
    * frame has no stray value, no overflow and nothing for axe to object to, so
    * these are what fail it:
    *
    * - `servedError`: the dev server answered 5xx for a file the page asked
    *   for ("504 (Outdated Optimize Dep)" is how a shared cache shows).
    * - `failedLoad`: a file the page asked for got no answer at all.
    *   `/api/` is left out of both: the fixtures replace the backend
    *   (`?__fixtures`), nothing listens behind Vite's proxy, and a page that
    *   reached past its fixtures is already `__fixtureMisses`' to name.
    * - `missingShell`: a console route that drew no top bar. The routes with
    *   no console shell of their own are `FRAMELESS` (`./routes.mjs`).
    */
   function pathOf(url) {
     try {
       return new URL(url).pathname;
     } catch {
       return url;
     }
   }

   export function servedError(status, url) {
     if (status < 500) return null;
     const path = pathOf(url);
     return path.startsWith("/api/") ? null : `the dev server answered ${status} for ${path}`;
   }

   export function failedLoad(url, errorText) {
     const path = pathOf(url);
     return path.startsWith("/api/") ? null : `${path} never loaded (${errorText})`;
   }

   export function missingShell(route, probe) {
     return !FRAMELESS.has(route.split("?")[0]) && !(probe && probe.topbar);
   }
   ```

   d. En `walkApp`, el objeto `result` gana, después de `chromeProblems: [],`, la línea `served: [],` con el comentario `// What the dev server failed to serve this page (`servedError`, `failedLoad`).`; y después del `page.on("console", …)`:
   ```js
         page.on("response", (response) => {
           const served = servedError(response.status(), response.url());
           if (served && !result.served.includes(served)) result.served.push(served);
         });
         page.on("requestfailed", (request) => {
           const failed = failedLoad(request.url(), request.failure()?.errorText ?? "failed");
           if (failed && !result.served.includes(failed)) result.served.push(failed);
         });
   ```

   e. En el mismo `try`, justo después de `result.chrome = await page.evaluate(CHROME_IN_PAGE);`:
   ```js
           if (missingShell(route, result.chrome)) throw new Error("drew no shell: a console route with no top bar");
   ```

   f. En `main`, dentro del bucle que llena `failures`, después de la línea de `--strict-fixtures`: `for (const served of r.served ?? []) failures.push(`${where}: ${served}`);`; y en `printAppTable`, después de `if (r.misses.length) lines.push(…)`: `for (const served of r.served) lines.push(`served: ${served}`);`.

   Y al final de `frontend/scripts/harmony.d.mts`:
   ```ts
   /** `"the dev server answered 504 for /node_modules/.vite/deps/x.js"` for a 5xx on a page file; null otherwise and for `/api/`. */
   export declare function servedError(status: number, url: string): string | null;
   /** `"/src/x.tsx never loaded (net::ERR_ABORTED)"` for a page file that got no answer; null for `/api/`. */
   export declare function failedLoad(url: string, errorText: string): string | null;
   /** A console route (not in `FRAMELESS`) whose probe found no top bar. */
   export declare function missingShell(route: string, probe: ChromeProbe | null): boolean;
   ```

4. Run: `cd frontend && npx vitest run src/test/harmony-client.test.ts src/test/routes-covered.test.ts && npm run typecheck`
   Expected: PASS (30 pruebas en los dos archivos al 2026-11-24) y `tsc` sin salida.

Run: `node scripts/harmony.mjs --strict-fixtures --lang en --no-shots`
Expected: `ok …`, como antes del cambio: ninguna ruta de hoy deja de dibujar el armazón ni recibe un 5xx. En la copia de trabajo del 2026-11-24, 467 de las 468 cargas pasaron las condiciones nuevas a la primera; la otra (`/backup/policies @ 390x844-client`, con otras pasadas de Chromium en marcha en la misma máquina) perdió tres módulos con `net::ERR_NETWORK_CHANGED` y no dibujó el armazón: la puerta la rechazó, que es justo lo que hoy no hace. Si pasa aquí, se repite la pasada sin nada más corriendo; nunca se da por buena.

- [ ] **Step 13: Suite completa y navegador**

Run: `cd frontend && npx vitest run && npm run typecheck && npm run build`
Expected: todo en verde (366 archivos de prueba o más: los de hoy y `second-carrier.test.tsx`).

Run: `node scripts/harmony.mjs --strict-fixtures --lang en --routes /devices,/health,/settings,/clients` y lo mismo con `--lang es`
Expected: `ok    every route on the tokens, …` en las dos. Las capturas de `/devices` a 1440x900 ya muestran el riel `#101b30` y el acento cobalto.

Run: `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs`
Expected: termina con código 0.

- [ ] **Step 14: Commit**

```bash
git add PRODUCT.md DESIGN.md .impeccable/design.json docs/product.md frontend/src/index.css frontend/src/shell/shell.css frontend/src/shell/components.css frontend/src/shell/viz.css frontend/src/shell/viz.tsx frontend/src/components/chart.tsx frontend/scripts/harmony.mjs frontend/scripts/harmony.d.mts frontend/scripts/routes.mjs frontend/scripts/routes.d.mts frontend/src/test/tokens.test.ts frontend/src/test/contrast.test.ts frontend/src/test/radius.test.ts frontend/src/test/second-carrier.test.tsx frontend/src/test/harmony-client.test.ts
git commit -m "feat(ui): Novieri brand tokens, DESIGN.md v4, impeccable product context; harmony fails an undrawn page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación:** `tokens.test.ts` compara las tres fuentes y pasa; `second-carrier.test.tsx` fija el segundo portador de «en curso» en los cuatro sitios sin etiqueta; `impeccable context` carga `PRODUCT.md`; harmony falla una ruta de la consola sin armazón y un 5xx del servidor de desarrollo, y sigue en verde en las cuatro rutas de muestra en los dos idiomas; ninguna prueba se debilitó.

---

### Task 2: Nombre «Novieri RMM», logotipo, símbolo, favicon, título de pestaña y guarda de «ITMS»

Diseño §3.1 (logotipo, símbolo, favicon), decisión A3 (nombre) y §5 (las pruebas del título y de «ITMS»).

**Files:**
- Create: `frontend/public/favicon.svg` (copia exacta de `docs/design/marca/novieri-favicon.svg`)
- Create: `frontend/src/shell/brand.tsx`, `frontend/src/shell/brand.css`
- Create: `frontend/src/shell/title.tsx`
- Create: `frontend/src/test/no-itms.test.ts`, `frontend/src/shell/title.test.tsx`, `frontend/src/shell/brand.test.tsx`
- Modify: `frontend/index.html:6-10`
- Modify: `frontend/src/shell/Sidebar.tsx:56-63` (la marca), `frontend/src/shell/AppShell.tsx:323-337` (título) y `:619-631` (raíz de la miga)
- Modify: `frontend/src/shell/nav.ts:290,336` (título de reserva)
- Modify: `frontend/src/i18n/{en,es}/{people,settings,auth,change,agentReleases}.ts` (las cadenas visibles con «ITMS»), `frontend/src/i18n/test-utils.tsx:16`
- Modify: `frontend/src/modules/auth/fixtures.ts:28,32,60`, `frontend/src/modules/settings/fixtures.ts:55`, `frontend/src/modules/agent-releases/fixtures.ts:207`, `frontend/src/lib/api.ts:1472,4344`
- Modify: `frontend/src/preview/PreviewPage.tsx:774,792-794,1618`, `frontend/scripts/a11y-browser.mjs:491` y `frontend/src/test/preview.test.ts:169` (la sonda XSS)
- Modify (los nombres del agente, diseño del proyecto 4 §1.1 y Apéndice F de su plan): `frontend/src/modules/agent-releases/publish.ts:6,57,63`, `frontend/src/modules/agent-releases/fixtures.ts:104-128`, `frontend/src/modules/deploy/fixtures.ts:47`, y las ocho pruebas que los fijan: `publish.test.ts`, `PublishReleaseDialog.test.tsx`, `agent-releases.es.test.tsx`, `ReleaseChannels.test.tsx`, `ManagedDeploymentDialog.test.tsx`, `AgentReleasesPage.test.tsx`, `MsiDownload.test.tsx` y `frontend/src/lib/api.test.ts`
- Modify (anfitriones de los datos de prueba): `frontend/src/modules/settings/fixtures.ts:54,342,347,527`, `frontend/src/modules/email-intake/fixtures.ts:182`
- Modify (lo que la guarda rompe, nombrado de antemano): `frontend/src/i18n/coverage.test.ts:115`, `frontend/src/modules/audit/auditVocabulary.coverage.test.ts:38-43`, `frontend/src/shell/AppShell.test.tsx:140-146,727-742`, `frontend/src/modules/settings/settings.es.test.tsx:315,349`
- Modify: cada archivo que la guarda nombre (Step 6), comentarios y pruebas incluidos

**Interfaces:**
- Produces (`shell/brand.tsx`): `PRODUCT_NAME = "Novieri RMM"`, `BRAND_NAME = "Novieri"`, `NovieriSymbol({ size?: number; tile?: boolean; label?: string })`, `NovieriWordmark({ height?: number; label?: string; className?: string })`, `NovieriLockup({ height?: number })`.
- Produces (`shell/title.tsx`): `documentTitle(page?: string, record?: string): string`, `RecordTitleProvider` (un `Context.Provider` de `(name: string | undefined) => void`), `useRecordTitle(name: string | undefined): void`, `useDocumentTitle(page: string | undefined): void`.
- Las oleadas usan `useRecordTitle` en cada ficha, y la O2 y la O8 usan `useDocumentTitle` y `NovieriLockup` en el portal y en las pantallas de acceso.

- [ ] **Step 1: Escribir las pruebas (fallan)**

`frontend/src/shell/title.test.tsx`:

```tsx
import { render } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { documentTitle, RecordTitleProvider, useDocumentTitle, useRecordTitle } from "./title";

describe("documentTitle (design §5: 'Página · Novieri RMM')", () => {
  it("names the page, then the product", () => {
    expect(documentTitle("Devices")).toBe("Devices · Novieri RMM");
  });

  it("names a record before its page", () => {
    expect(documentTitle("Devices", "srv-app02")).toBe("srv-app02 · Devices · Novieri RMM");
  });

  it("is the product alone when there is nothing else to say, and never says it twice", () => {
    expect(documentTitle()).toBe("Novieri RMM");
    expect(documentTitle("  ")).toBe("Novieri RMM");
    expect(documentTitle("Novieri RMM")).toBe("Novieri RMM");
  });
});

function Shell({ page, children }: { page: string; children: React.ReactNode }) {
  const [record, setRecord] = useState<string | undefined>();
  document.title = documentTitle(page, record);
  return <RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>;
}

function Record({ name }: { name: string }) {
  useRecordTitle(name);
  return null;
}

describe("useRecordTitle", () => {
  it("puts the record's name in front while the record is open, and takes it away after", () => {
    const view = render(
      <Shell page="Devices">
        <Record name="srv-app02" />
      </Shell>,
    );
    expect(document.title).toBe("srv-app02 · Devices · Novieri RMM");
    view.rerender(<Shell page="Devices">{null}</Shell>);
    expect(document.title).toBe("Devices · Novieri RMM");
  });

  it("does nothing outside the shell", () => {
    document.title = "before";
    render(<Record name="srv-app02" />);
    expect(document.title).toBe("before");
  });
});

describe("useDocumentTitle", () => {
  function Frame({ page }: { page: string }) {
    useDocumentTitle(page);
    return null;
  }

  it("names a page that has no shell, and gives the old title back", () => {
    document.title = "before";
    const view = render(<Frame page="Sign in" />);
    expect(document.title).toBe("Sign in · Novieri RMM");
    view.unmount();
    expect(document.title).toBe("before");
  });
});
```

`frontend/src/shell/brand.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { BRAND_NAME, NovieriLockup, NovieriSymbol, NovieriWordmark, PRODUCT_NAME } from "./brand";

const MARCA = resolve(import.meta.dirname, "../../../docs/design/marca");

describe("the brand", () => {
  it("is called what the operator named it (decision A3)", () => {
    expect(PRODUCT_NAME).toBe("Novieri RMM");
    expect(BRAND_NAME).toBe("Novieri");
  });

  it("draws the operator's own symbol, path for path", () => {
    const source = readFileSync(resolve(MARCA, "novieri-symbol.svg"), "utf8");
    const { container } = render(<NovieriSymbol />);
    const drawn = container.querySelector("path")?.getAttribute("d");
    expect(source).toContain(`d="${drawn}"`);
  });

  it("draws the operator's own wordmark, path for path", () => {
    const source = readFileSync(resolve(MARCA, "novieri-wordmark.svg"), "utf8");
    const { container } = render(<NovieriWordmark />);
    const paths = [...container.querySelectorAll("path")].map((path) => path.getAttribute("d"));
    expect(paths).toHaveLength(7);
    for (const d of paths) expect(source).toContain(`d="${d}"`);
  });

  it("names itself to assistive technology once, and the symbol is decorative unless labelled", () => {
    render(<NovieriWordmark />);
    expect(screen.getByRole("img", { name: "Novieri" })).toBeInTheDocument();
    const { container } = render(<NovieriSymbol tile />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("puts the product's full name on the lockup", () => {
    render(<NovieriLockup />);
    expect(screen.getByRole("img", { name: "Novieri RMM" })).toBeInTheDocument();
  });

  it("ships the favicon the operator approved, and the page links it", () => {
    const approved = readFileSync(resolve(MARCA, "novieri-favicon.svg"), "utf8");
    const shipped = readFileSync(resolve(import.meta.dirname, "../../public/favicon.svg"), "utf8");
    expect(shipped).toBe(approved);
    const html = readFileSync(resolve(import.meta.dirname, "../../index.html"), "utf8");
    expect(html).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />');
    expect(html).toContain("<title>Novieri RMM</title>");
  });
});
```

`frontend/src/test/no-itms.test.ts`:

```ts
/**
 * "ITMS" WAS A PLACEHOLDER; THE PRODUCT IS NOVIERI RMM (operator, 2026-11-23,
 * decision A3 of the console redesign). This keeps the old name out of the
 * console by reading its source, the technique of
 * `backend/tests/test_no_vendor_names_backend.py` and `lib/noVendorNames.test.ts`.
 *
 * WIDER THAN THOSE TWO, ON PURPOSE. They read only what a user can see,
 * because the vendor they ban is a real dependency whose name is accurate in
 * code. "ITMS" names nothing real any more, so every line of `src/`,
 * `scripts/` and `index.html` is read, comments and tests included. A line
 * may still carry the letters for one of two reasons, and only these:
 *   - a wire contract with the server, which project 4 keeps (its design
 *     §1.2): the `X-ITMS-*` request headers (`X-ITMS-Request` and
 *     `X-ITMS-Background` today, and any other P4 keeps by that prefix) and
 *     the recording format's magic `ITMSRSP1`;
 *   - dated history IN A COMMENT: the line is a comment (`//`, `*`, `/*`,
 *     `{/*`, `<!--`) and carries a `YYYY-MM-DD` date, so it says what was true
 *     then, and rewriting it would falsify the record. A date does not excuse
 *     a string a person can read.
 * Lower-case `itms.` storage keys are identifiers nobody sees and stay
 * (project 4's rule for internal identifiers); this guard is case-sensitive.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const FRONTEND = resolve(import.meta.dirname, "../..");
const ROOTS = [resolve(FRONTEND, "src"), resolve(FRONTEND, "scripts"), resolve(FRONTEND, "index.html")];
const SELF = resolve(import.meta.dirname, "no-itms.test.ts");
const WIRE = /X-ITMS-[A-Za-z-]+|ITMSRSP1/g;
const DATED = /\b20\d\d-\d\d-\d\d\b/;
const COMMENT = /^\s*(\/\/|\*|\/\*|\{\/\*|<!--)/;

function files(path: string): string[] {
  if (statSync(path).isDirectory()) return readdirSync(path).flatMap((name) => files(join(path, name)));
  return /\.(tsx?|css|html|mjs|json)$/.test(path) ? [path] : [];
}

/** The 1-based numbers of the lines that name ITMS for a reason this guard refuses. */
export function offences(source: string): number[] {
  return source.split("\n").flatMap((line, at) => {
    const rest = line.replace(WIRE, "");
    if (!rest.includes("ITMS")) return [];
    if (COMMENT.test(line) && DATED.test(line)) return [];
    return [at + 1];
  });
}

describe("the old product name", () => {
  it("is caught where it appears, so a clean result means something", () => {
    expect(offences('<span className="brand-name">ITMS</span>')).toEqual([1]);
    expect(offences("document.title = `${title} · ITMS`;")).toEqual([1]);
    expect(offences('export const CSRF_HEADER = "X-ITMS-Request";')).toEqual([]);
    expect(offences('const MAGIC = "ITMSRSP1";')).toEqual([]);
    expect(offences("// ITMS was the placeholder name until 2026-11-23")).toEqual([]);
    expect(offences('headers.set("X-ITMS-Request", "1"); // the ITMS header')).toEqual([1]);
    expect(offences('const AGENT = "X-ITMS-Agent-Version";')).toEqual([]);
    expect(offences(" * the ITMS tab title, as it read on 2026-09-08")).toEqual([]);
    expect(offences('title: "ITMS (2026-11-23)",')).toEqual([1]);
    expect(offences("ok\nstill ITMS\nok")).toEqual([2]);
  });

  it("reads a real tree", () => {
    const all = ROOTS.flatMap(files);
    expect(all.length).toBeGreaterThan(500);
    expect(all).toContain(resolve(FRONTEND, "index.html"));
  });

  it("appears nowhere in the console's source, tests, scripts or page", () => {
    const found = ROOTS.flatMap(files)
      .filter((path) => path !== SELF)
      .flatMap((path) => offences(readFileSync(path, "utf8")).map((line) => `${relative(FRONTEND, path)}:${line}`));
    expect(found, found.join("\n")).toEqual([]);
  });
});
```

- [ ] **Step 2: Ejecutarlas y verlas fallar**

Run: `cd frontend && npx vitest run src/shell/title.test.tsx src/shell/brand.test.tsx src/test/no-itms.test.ts`
Expected: FAIL. `title.test.tsx` y `brand.test.tsx` no compilan (`./title` y `./brand` no existen); `no-itms.test.ts` pasa sus dos primeros casos y en el tercero lista 94 líneas (medido con esta versión de la guarda en una copia de trabajo el 2026-11-24: las 92 de `grep -rn ITMS frontend/src | grep -v "X-ITMS\|ITMSRSP1"` más dos cadenas fechadas que ya no se excusan por la fecha).

- [ ] **Step 3: `brand.tsx`, `brand.css`, `title.tsx` y el favicon**

Run: `cp docs/design/marca/novieri-favicon.svg frontend/public/favicon.svg` (crea `frontend/public/`; Vite copia `public/` a la raíz de `dist/`, y `deploy/nginx.conf` ya sirve cualquier archivo de la raíz con `try_files $uri`).

`frontend/src/shell/brand.tsx`:

```tsx
/**
 * THE BRAND (DESIGN.md v4, "Brand"), drawn from the operator's own SVGs in
 * `docs/design/marca/` (direction A, "Cordillera", 2026-11-23).
 *
 * INLINE, NOT `<img>`: the wordmark takes its colour from the ground it sits
 * on (`currentColor`: white on the rail, ink on a light frame), and an inline
 * SVG is measured by the harmony gate and the a11y pass like any other paint.
 * The paths are the files' own, and `brand.test.tsx` compares them.
 */
import "./brand.css";

/** The product's name (decision A3): in the tab title, the sign-in frame, the portal. */
export const PRODUCT_NAME = "Novieri RMM";
/** What the console's own bar says (decision A3). */
export const BRAND_NAME = "Novieri";

const SYMBOL_PATH = "M4.73 25L8.37 25L14.67 7L11.03 7ZM11.03 7L14.67 7L20.97 25L17.33 25ZM17.33 25L20.97 25L27.27 7L23.63 7Z";

/** The wordmark's seven letters, each at its offset in the 112.9-wide view box. */
const WORDMARK: readonly { x: number; d: string; evenOdd?: boolean }[] = [
  { x: 0, d: "M0 0h2.7v20h-2.7ZM13.7 0h2.7v20h-2.7ZM0 0L3.233 0L16.4 20L13.167 20Z" },
  {
    x: 20.4,
    d: "M0 10a10.35 10.35 0 1 0 20.7 0a10.35 10.35 0 1 0 -20.7 0ZM2.889 10a7.461 7.461 0 1 1 14.922 0a7.461 7.461 0 1 1 -14.922 0Z",
    evenOdd: true,
  },
  { x: 44.5, d: "M0 0L2.818 0L10.209 20L7.391 20ZM14.782 0L17.6 0L10.209 20L7.391 20Z" },
  { x: 66.6, d: "M0 0h2.7v20h-2.7Z" },
  { x: 74.2, d: "M0 0h2.7v20h-2.7ZM0 0h12.4v2.7h-12.4ZM0 8.65h11.1v2.7h-11.1ZM0 17.3h12.4v2.7h-12.4Z" },
  { x: 91, d: "M0 0h2.7v20h-2.7ZM0 0H7.5A5.6 5.6 0 0 1 7.5 11.2H0V8.5H7.5A2.9 2.9 0 0 0 7.5 2.7H0ZM5.1 8.5L8.122 8.5L13.9 20L10.878 20Z" },
  { x: 110, d: "M0 0h2.7v20h-2.7Z" },
];

const R_PATH = WORDMARK[5]?.d ?? "";
const M_PATH = "M0 0h2.7v20h-2.7ZM16.9 0h2.7v20h-2.7ZM0 0L2.952 0L11.276 15.5L8.324 15.5ZM16.648 0L19.6 0L11.276 15.5L8.324 15.5Z";

function Letters() {
  return (
    <>
      {WORDMARK.map((letter) => (
        <path
          key={letter.x}
          className="nv-letter"
          transform={`translate(${letter.x} 0)`}
          d={letter.d}
          fillRule={letter.evenOdd ? "evenodd" : undefined}
        />
      ))}
    </>
  );
}

/**
 * The zigzag N. `tile`: on its cobalt tile with a white stroke, as the favicon
 * and the rail draw it. Decorative unless `label` is given, because it always
 * sits beside the wordmark that already names the product.
 */
export function NovieriSymbol({ size = 28, tile = false, label }: { size?: number; tile?: boolean; label?: string }) {
  return (
    <svg
      className={tile ? "nv-symbol tile" : "nv-symbol"}
      viewBox="0 0 32 32"
      width={size}
      height={size}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      {tile ? <rect className="nv-tile" width="32" height="32" rx="7" /> : null}
      <path className="nv-stroke" d={SYMBOL_PATH} />
    </svg>
  );
}

/** "NOVIERI", in the colour of the text around it. */
export function NovieriWordmark({ height = 16, label = BRAND_NAME, className }: { height?: number; label?: string; className?: string }) {
  return (
    <svg
      className={className ? `nv-wordmark ${className}` : "nv-wordmark"}
      viewBox="-0.1 -0.6 112.9 21.2"
      width={(height * 112.9) / 21.2}
      height={height}
      role="img"
      aria-label={label}
      focusable="false"
    >
      <Letters />
    </svg>
  );
}

/** "NOVIERI | RMM": the sign-in frame, the portal, and wherever the full name is the subject. */
export function NovieriLockup({ height = 22 }: { height?: number }) {
  return (
    <svg
      className="nv-lockup"
      viewBox="-0.1 -0.6 156.18 21.2"
      width={(height * 156.18) / 21.2}
      height={height}
      role="img"
      aria-label={PRODUCT_NAME}
      focusable="false"
    >
      <Letters />
      <rect className="nv-rule" x="117.9" y="0" width="0.9" height="20" />
      <g className="nv-descriptor" transform="translate(124 9.6) scale(0.52)">
        <path d={R_PATH} />
        <path transform="translate(18.1 0)" d={M_PATH} />
        <path transform="translate(41.9 0)" d={M_PATH} />
      </g>
    </svg>
  );
}
```

`frontend/src/shell/brand.css`:

```css
/* The brand's paint (DESIGN.md v4, "Brand"). Tokens only: the harmony gate
 * reads `fill` on every SVG. */
.nv-symbol, .nv-wordmark, .nv-lockup { display: block; flex: 0 0 auto; }
.nv-symbol .nv-stroke { fill: var(--accent); }
.nv-symbol.tile .nv-tile { fill: var(--accent); }
.nv-symbol.tile .nv-stroke { fill: var(--accent-ink); }
.nv-wordmark .nv-letter, .nv-lockup .nv-letter { fill: currentColor; }
.nv-lockup .nv-rule, .nv-lockup .nv-descriptor path { fill: var(--muted); }
```

`frontend/src/shell/title.tsx`:

```tsx
/**
 * THE BROWSER TAB SAYS WHERE YOU ARE (design §5; WCAG 2.4.2): "<Page> ·
 * Novieri RMM", and on a record "<Record> · <Page> · Novieri RMM". Atera's own
 * walkthrough files "the same title on every page" as a defect, and a person
 * with a dozen consoles open reads the tab before anything else.
 *
 * The shell owns the page half (its title is the rail entry's own label, so
 * it cannot drift from the rail and the crumb); a record page adds its name
 * through `useRecordTitle`. A frame with no shell -- sign-in, the portal, the
 * consent page -- names itself with `useDocumentTitle`.
 */
import { createContext, useContext, useEffect } from "react";

import { PRODUCT_NAME } from "./brand";

export function documentTitle(page?: string, record?: string): string {
  const parts = [record, page]
    .map((part) => part?.trim() ?? "")
    .filter((part) => part !== "" && part !== PRODUCT_NAME);
  return [...parts, PRODUCT_NAME].join(" · ");
}

const RecordTitle = createContext<((name: string | undefined) => void) | null>(null);

/** Given by `AppShell`, which holds the record's name beside the page's title. */
export const RecordTitleProvider = RecordTitle.Provider;

/** A record page names itself in the tab while it is open. Outside the shell, nothing. */
export function useRecordTitle(name: string | undefined): void {
  const set = useContext(RecordTitle);
  useEffect(() => {
    if (!set) return undefined;
    set(name);
    return () => set(undefined);
  }, [set, name]);
}

/** A frame with no shell names its page, and gives the previous title back when it goes. */
export function useDocumentTitle(page: string | undefined): void {
  useEffect(() => {
    const previous = document.title;
    document.title = documentTitle(page);
    return () => {
      document.title = previous;
    };
  }, [page]);
}
```

- [ ] **Step 4: `index.html`, la barra lateral, el armazón y la miga**

`frontend/index.html`, líneas 6 a 10:

```html
    <title>Novieri RMM</title>
    <!-- The brand's favicon (docs/design/marca/novieri-favicon.svg), read at 16
       ~ and 32px. It also ends the browser's automatic /favicon.ico request,
       ~ which is what the old `data:,` link was there for. -->
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
```

`frontend/src/shell/Sidebar.tsx`, el bloque `.brand` (líneas 56 a 63) pasa a:

```tsx
      <div className="brand">
        {/* The brand (DESIGN.md v4): the N on its cobalt tile and the white
            wordmark. The wordmark folds away with the rail (`.brand-name`). */}
        <NovieriSymbol tile size={28} />
        <NovieriWordmark className="brand-name" height={14} />
```

con `import { NovieriSymbol, NovieriWordmark } from "./brand";`. En `shell.css`, `.brand-mark` deja de usarse: borrar esa regla (la que pinta `background: var(--accent)` sobre un cuadro de 28 px), y en la regla `.brand` cambiar `color: #fff;` por `color: var(--accent-ink);` (el logotipo toma ese color por `currentColor`).

`frontend/src/shell/AppShell.tsx`:
- importar `{ BRAND_NAME }` de `./brand` y `{ documentTitle, RecordTitleProvider }` de `./title`;
- en el cuerpo, junto a los demás estados: `const [record, setRecord] = useState<string | undefined>();`
- el efecto del título (líneas 330-336) pasa a:
  ```tsx
  useEffect(() => {
    const previous = document.title;
    document.title = documentTitle(title, record);
    return () => {
      document.title = previous;
    };
  }, [title, record]);
  ```
  y el comentario que lo precede dice «THE TAB SAID THE SAME THING ON EVERY ROUTE …» en lugar de «THE TAB SAID "ITMS" …»;
- en la miga (líneas 619 a 631), las dos apariciones de `ITMS` como texto del enlace raíz pasan a `{BRAND_NAME}`;
- `{children}` dentro de `<main>` pasa a `<RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>`.

`frontend/src/shell/nav.ts`: línea 290, el comentario dice `what goes before "· Novieri RMM" in the tab title`; línea 336, `title: "ITMS"` pasa a `title: PRODUCT_NAME`, con `import { PRODUCT_NAME } from "./brand";` (`documentTitle` no lo repite).

- [ ] **Step 5: Los textos visibles, los datos de prueba y la sonda**

Exactamente estos cambios de texto (inglés y español, en los dos catálogos):

| Archivo | Clave | Cambio |
|---|---|---|
| `i18n/en/people.ts:179` / `es/people.ts:175` | `stillSees` | «ITMS still sees…» → «Novieri RMM still sees…» / «Novieri RMM sigue detectando…» |
| `i18n/en/settings.ts:309` / `es/settings.ts:299` | el `body` de borrar usuario | «Their ITMS record…» → «Their Novieri RMM record…» / «Su registro de Novieri RMM…» |
| `i18n/en/auth.ts:190,195` / `es/auth.ts:187,193` | `intro`, el cuerpo del consentimiento | «use ITMS as you», «act as you in ITMS» → «use Novieri RMM as you», «act as you in Novieri RMM» / «Pide usar Novieri RMM en tu nombre», «actuar como tú en Novieri RMM» |
| `i18n/en/change.ts:46` / `es/change.ts:41` | `tabTitle` | «{title} · Change · ITMS» → «{title} · Change · Novieri RMM» / «{title} · Cambio · Novieri RMM» (la O6 lo cambia a `useRecordTitle`) |
| `i18n/en/agentReleases.ts:197` / `es/agentReleases.ts:198` | `linkHelp` | «ITMS keeps only a fingerprint» → «Novieri RMM keeps only a fingerprint» / «Novieri RMM solo guarda una huella» |
| `i18n/test-utils.tsx:16` | `SAME_IN_BOTH` | `"ITMS"` → `"Novieri RMM"`, y añadir `"Novieri"` |

Datos que imitan al servidor (el proyecto 4 cambia el servidor a `Novieri RMM`, su diseño §1.1):
- `modules/auth/fixtures.ts:28`: `otpauth://totp/Novieri%20RMM:helgar.palmieri%40toeshee.example?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=Novieri%20RMM`; `:32` y `:60`: `rp: { id: "localhost", name: "Novieri RMM" }`.
- `modules/settings/fixtures.ts:55`: `from_display_name: "Novieri RMM"`.
- `modules/agent-releases/fixtures.ts:207`: `` filename: `NovieriRMM-Agent-console.toeshee.example-${body.arch}.msi` ``.
- `lib/api.ts:1472` (comentario): `` `NovieriRMM-Agent-<host>[@<port>]-<arch>.msi` ``; `:4344`: `` ?? `NovieriRMM-Agent-${body.arch}.msi` ``. (El nombre que manda el servidor es del proyecto 4; Apéndice E, X2.)

El comentario de `lib/clientChoice.ts:11-12` («project 4's rename of the brand renames it with `itms.sidebar.collapsed`») deja de ser cierto: el diseño del proyecto 4 (§1.2) no renombra identificadores internos y este plan conserva las claves `itms.*` (decisión D15). Pasa a: «Kept in `localStorage["itms.client"]` as the id or `all`. The key keeps its old prefix on purpose: it is an identifier nobody sees (project 4's rule, its design §1.2), and the harmony gate and the a11y pass write it by that name.»

La sonda XSS de `/__preview`, su lectura en el script del navegador y la guarda que comprueba que no llega al paquete de producción: `window.__ITMS_XSS_EXECUTED` → `window.__XSS_PROBE_EXECUTED` en `preview/PreviewPage.tsx:774` (comentario), `:792`, `:793`, `:794`, `:1618` (comentario), `scripts/a11y-browser.mjs:491` y `src/test/preview.test.ts:169` (la cadena `"__ITMS_XSS_EXECUTED"` de su lista pasa a `"__XSS_PROBE_EXECUTED"`; sin este cambio esa guarda buscaría un nombre que ya nadie escribe y no afirmaría nada). Después: `grep -rn "XSS_EXECUTED\|XSS_PROBE" frontend/src frontend/scripts` da siete líneas, todas con `__XSS_PROBE_EXECUTED`.

**Los nombres del agente** (diseño del proyecto 4 §1.1, «Binario y servicio del agente … y los artefactos `its-agent-<os>-<arch>` → `novieri-agent`»; resolución del coordinador del 2026-11-24: P3 hace las ediciones de la consola y las concilia con el Apéndice F del plan del proyecto 4 cuando exista). La consola lee el nombre del binario para saber su plataforma al publicar una versión (`PublishReleaseDialog.tsx:129,249`): si solo cambiara el agente, cada publicación tras la PR conjunta pediría elegir a mano las seis plataformas, y la suite seguiría en verde porque sus pruebas dan a la consola nombres `its-agent-*`.
- `modules/agent-releases/publish.ts`, en `inferPlatform` (línea 63): `if (parts.length !== 4 || parts[0] !== "its" || parts[1] !== "agent") return null;` → `if (parts.length !== 4 || parts[0] !== "novieri" || parts[1] !== "agent") return null;`. En los dos comentarios, la línea 6 dice `` carrying six binaries named `novieri-agent-<os>-<arch>[.exe]` `` y la 57 `` `novieri-agent-<os>-<arch>` with both halves from the known sets ``. El nombre viejo **no** se sigue aceptando: no hay flota (proyecto 4, B1 y P7), y aceptar dos nombres sería un segundo contrato que nadie usa.
- `modules/agent-releases/fixtures.ts:104-128`: cada `its-agent` pasa a `novieri-agent` (la URL del artefacto, el binario de las órdenes, `install_command`, `enrol_command`, `verify_command`); `ITS_ENROLL_TOKEN` no cambia (el prefijo `ITS_` se queda, P4 §1.2).
- `modules/deploy/fixtures.ts:47`: `name: "toeshee-agent"` → `name: "novieri-agent"` (es el paquete `.deb` del agente, que el proyecto 4 llama `novieri-agent`; ninguna prueba fija ese nombre, solo el `id` `pkg-agent-deb`, que no cambia).
- Las seis pruebas del módulo que nombran binarios: `sed -i 's/its-agent/novieri-agent/g' frontend/src/modules/agent-releases/{publish.test.ts,PublishReleaseDialog.test.tsx,agent-releases.es.test.tsx,ReleaseChannels.test.tsx,ManagedDeploymentDialog.test.tsx,AgentReleasesPage.test.tsx}` (no toca `ITS_AGENT_ENROLL_TOKEN`, que va en mayúsculas). Después, `grep -rn "its-agent" frontend/src` no imprime nada.
- Las tres pruebas que fijan el nombre del MSI que manda el servidor (`MsiDownload.test.tsx:91,97,98,130,134,137`, `lib/api.test.ts:200,220`, `agent-releases.es.test.tsx:194`): `sed -i 's/ITMS-Agent/NovieriRMM-Agent/g' frontend/src/modules/agent-releases/{MsiDownload.test.tsx,agent-releases.es.test.tsx} frontend/src/lib/api.test.ts`. El anfitrión en minúsculas (`itms.example.com`) es un dato de esas pruebas y se queda: la guarda solo mira «ITMS» en mayúsculas. Después, `grep -rn "ITMS-Agent" frontend/src` no imprime nada. Con esto quedan las ocho pruebas y los dos `fixtures.ts` que el Apéndice F del plan del proyecto 4 (F.1 a F.3) nombra.
- En `publish.test.ts`, junto a los casos de `inferPlatform` (línea 49), uno nuevo:
  ```ts
  it("reads the agent's new name and only that (project 4 design §1.1: no fleet keeps the old one)", () => {
    expect(inferPlatform("novieri-agent-windows-amd64.exe")).toEqual({ os: "windows", arch: "amd64" });
    expect(inferPlatform("its-agent-windows-amd64.exe")).toBeNull();
  });
  ```

**Los anfitriones de los datos de prueba**, que la puerta de armonía fotografía y el operador revisa (el proyecto 4 deja `rmm.novieri.com`, B2): `itms.toeshee.example` → `rmm.novieri.example` en `modules/settings/fixtures.ts:342,347,527` y `modules/email-intake/fixtures.ts:182`; y `modules/settings/fixtures.ts:54`, `from_address: "itms@toeshee.example"` → `from_address: "rmm@novieri.example"`. Ninguna prueba fija esos valores (`grep -rn "itms.toeshee.example\|itms@toeshee.example" frontend/src` solo los encuentra en los dos `fixtures.ts`).

**La variable de entorno de una guarda**: `modules/audit/auditVocabulary.coverage.test.ts:38-43`, `ITMS_AUDIT_BACKEND_APP` → `ITS_AUDIT_BACKEND_APP` en el comentario y en las dos lecturas de `process.env`: una variable que pone un desarrollador lleva el prefijo `ITS_` de todas (proyecto 4, §1.2). Nada más la pone (`grep -rn AUDIT_BACKEND_APP` solo da ese archivo).

- [ ] **Step 6: Barrer lo que la guarda señale**

Run: `cd frontend && npx vitest run src/test/no-itms.test.ts`
La prueba imprime cada `archivo:línea`. Para cada una:
- un comentario que usa «ITMS» como nombre del producto: «Novieri RMM» (o «the console» si se habla de la interfaz);
- un comentario que cuenta historia: se deja y se añade la fecha del hecho si no la tiene (`(2026-09-23)`), nunca una fecha inventada: se toma de `git log -S ITMS -- <archivo>` o del comentario vecino. La guarda excusa la fecha **solo en una línea que empieza como comentario** (`//`, `*`, `/*`, `{/*`, `<!--`): si «ITMS» cae en la continuación de un comentario de JSX sin marcador, esa línea se reescribe para que lo lleve (o se reparte para que la palabra caiga en la línea fechada);
- una prueba que esperaba «ITMS» en lo que pinta la consola: espera el texto nuevo (`"Devices & Inventory · Novieri RMM"` en `AppShell.test.tsx:146` y `:732`, el título de esa prueba pasa a `"names the page in the browser tab, not just the product"`; la miga en `App.crumb.test.tsx`, `App.shell.test.tsx` y `AppShell.test.tsx:742` espera el enlace `"Novieri"`; `nav.test.ts` espera `title: "Novieri RMM"` en «draws a bare crumb for a route in no model at all»);
- una prueba que fijaba un dato del servidor (`rp.name`, emisor TOTP, nombre del MSI): el dato nuevo del Step 5;
- una variable de entorno de desarrollo: el prefijo `ITS_` (Step 5, `ITS_AUDIT_BACKEND_APP`).

Tres de esas roturas se conocen de antemano y se hacen así, no por descubrimiento:
- `i18n/coverage.test.ts:115`: la entrada `ITMS:` de `ALLOWED_LITERALS` **se borra**, no se cambia por «Novieri»: la consola ya no escribe el nombre como literal en JSX (la barra dibuja el logotipo y la miga lee `BRAND_NAME`, un identificador), y una entrada que nada usa hace fallar `coverage.test.ts:381` («ALLOWED_LITERALS entries nothing uses: ['ITMS']», medido en una copia de trabajo).
- `shell/AppShell.test.tsx:140-141`: el comentario dice `// The brand block: the wordmark, which names itself to assistive technology.` y `expect(screen.getByText("ITMS", { selector: ".brand-name" })).toBeInTheDocument();` pasa a `expect(screen.getByRole("img", { name: "Novieri" })).toBeInTheDocument();`: el logotipo es un SVG sin nodo de texto, así que «espera el texto nuevo» fallaría.
- `modules/settings/settings.es.test.tsx:315` y `:349`: en las listas de `expectNoEnglish`, `"ITMS"` pasa a `"Novieri RMM"` (el remitente de los datos del Step 5); `"itms"` y `"itms@toeshee.com"` se quedan, porque son los datos propios de esa prueba.

Expected al final: PASS.

- [ ] **Step 7: Ejecutar las pruebas del paso 1 y la suite**

Run: `cd frontend && npx vitest run src/shell/title.test.tsx src/shell/brand.test.tsx src/test/no-itms.test.ts src/shell src/App.crumb.test.tsx src/App.shell.test.tsx src/i18n`
Expected: PASS.

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: todo en verde. `ls dist/favicon.svg` existe.

Run: `node scripts/harmony.mjs --strict-fixtures --lang es --routes /devices,/devices/dev-1,/sign-in` y con `--lang en`
Expected: `ok …`. La captura `es/1440x900/devices.png` muestra la N sobre su placa cobalto y «NOVIERI» en blanco en el riel.

- [ ] **Step 8: Commit**

```bash
git add frontend/public/favicon.svg frontend/index.html frontend/src frontend/scripts/a11y-browser.mjs
git commit -m "feat(ui): Novieri RMM name, logo, favicon and tab titles; guard against ITMS

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación:** el riel muestra el símbolo y «NOVIERI»; la pestaña dice `Dispositivos · Novieri RMM` en español en `/devices` (`Equipos · Novieri RMM` desde la Task 4, que cambia la etiqueta); el favicon es la N cobalto; `no-itms.test.ts` está en verde y sus únicas excepciones son las cabeceras `X-ITMS-*`, `ITMSRSP1` y los comentarios fechados; la consola reconoce `novieri-agent-<os>-<arch>` al publicar una versión y `grep -rn its-agent frontend/src` no imprime nada; la guarda de `preview.test.ts` busca `__XSS_PROBE_EXECUTED`.

---

### Task 3: El kit de patrones (lista, ficha, formulario) y lo que necesita debajo

Diseño §2 completo. Esta tarea **no cambia ninguna página** salvo `/__preview` (`src/preview/PreviewPage.tsx`, una de las 86), donde se enseñan las piezas nuevas para que `a11y-browser.mjs` las mida en píxeles. **Restilo de lo que ya existe:** el selector de cliente, las tablas, los avisos, los diálogos y los cajones (diseño §1.2 y §2, «Ya existentes, que se restilizan») cambian **solo por los tokens** de la Task 1 y por la geometría de `.btn`, `.chip` y `.pill` del Step 7; ninguna de sus reglas propias cambia, y un revisor no debe buscar otra cosa.

**Files:**
- Create: `frontend/src/shell/listKit.tsx`, `frontend/src/shell/detailKit.tsx`, `frontend/src/shell/formKit.tsx`, `frontend/src/shell/kit.css`
- Create: `frontend/src/test/renderPage.tsx`, `frontend/src/test/narrowing.ts`
- Create: `frontend/src/shell/RouteError.tsx`
- Create: `frontend/src/i18n/identical-ok.o1.ts` a `frontend/src/i18n/identical-ok.o8.ts` (Step 3b)
- Create: `frontend/src/shell/listKit.test.tsx`, `frontend/src/shell/detailKit.test.tsx`, `frontend/src/shell/formKit.test.tsx`, `frontend/src/lib/api.forClient.test.ts`, `frontend/src/App.router.test.tsx`
- Modify: `frontend/src/i18n/identical-ok.ts` (Step 3b: las entradas de cada oleada salen a su archivo), `frontend/src/i18n/catalogue.identical.test.ts` (Step 3b)
- Modify: `frontend/src/shell/components.tsx` (reexportar el kit; `Tabs` acepta `label`), `frontend/src/shell/components.css` (`.btn`, `.chip`, `.pill`: la geometría de la marca), `frontend/src/components/form.tsx:24` y `frontend/src/components/form.test.tsx:24` (dos comentarios, Step 7)
- Modify: `frontend/src/lib/api.ts` (`ApiClient.forClient`, junto al constructor, línea ~1822)
- Modify: `frontend/src/fixtures/client.ts:121-129` (el proxy responde `forClient`)
- Modify: `frontend/src/App.tsx:723-1631` (router de datos)
- Modify: `frontend/src/i18n/en/shell.ts`, `frontend/src/i18n/es/shell.ts` (bloque `kit`)
- Modify: `frontend/src/preview/PreviewPage.tsx` (tres secciones al final: «Kit v4», «AuthCard embedded» y «Portal frame»)

**Interfaces:**
- Consumes: `useApi` (`lib/ApiContext`), `useClientScope` (`lib/clientScope`), `isClientId` (`lib/clientChoice`), `Btn`, `FilterBar`, `ResultCount`, `Tabs`, `Panel` (`shell/components`), `Pill`, `type Kind` (`shell/viz`), `ConfirmDialog` (`components/confirm`), `describeSubmitError` (`components/form`).
- Produces (`shell/listKit.tsx`):
  - `useUrlFilters<K extends string>(keys: readonly K[]): UrlFilters<K>` con `values: Record<K, string>` (`""` si falta), `active: boolean`, `set(key: K, value: string): void` (`""` borra; siempre borra `offset`; la búsqueda `q` sustituye la entrada del historial, cualquier otra clave añade una, así que Atrás deshace un chip), `clear(): void` (añade una entrada);
  - `FilterChips({ label, options, value, onChange })`, `options: readonly { value: string; label: string; count?: number }[]` (el valor `""` es «todos»);
  - `ShowingCount({ shown: number; total: number | null })`;
  - `BulkBar({ count: number; onClear(): void; children })` (nada si `count === 0`);
  - `ListEmpty({ filtered: boolean; onClear(): void; nothingYet: string; action?: ReactNode; icon?: string })`;
  - `ClientFilter({ value: string; onChange(value: string): void })` (nada si arriba hay un cliente elegido o la persona tiene menos de dos; quita de la URL, con `onChange("")`, un valor que no es un cliente de la persona en cuanto `/me` lo sabe);
  - `useNarrowing(clientId: string): { client: string | null; ready: boolean }` (`client`: el identificador que de verdad se manda, o `null` si se lee con la elección de arriba; `ready`: `false` mientras `/me` no ha dicho si ese cliente es de la persona: no se lee nada antes);
  - `useApiFor(clientId: string): ApiClient` (la API estrechada a `useNarrowing(clientId).client`, o la de siempre);
  - `useNewParam(kind: string): { open: boolean; start(): void; close(): void }`;
  - `ListToolbar({ search?, chips?, end? })`.
- Produces (`shell/detailKit.tsx`): `SectionNav({ label: string; items: readonly { to: string; label: string; end?: boolean }[] })` (las páginas de un módulo como una fila de enlaces: sustituye a los ocho `*Nav.tsx` de módulo que hoy dibujan cada uno la suya), `DetailHead({ back: { to: string; label: string }; title: string; status?: { kind: Kind; label: string }; context?: ReactNode; actions?: ReactNode })`, `useUrlTab<K extends string>(keys: readonly K[], fallback: K, param?: string): [K, (key: K) => void]`, `UrlTabs<K extends string>({ items: readonly { key: K; label: string; count?: number }[]; active: K; onSelect(key: K): void; label: string; children })`, `DetailGrid({ main: ReactNode; aside: ReactNode })`, `ClientContext({ clientId: string | null; siteName?: string | null })`.
- Produces (`shell/formKit.tsx`): `FormPage({ back; title; description?; children })`, `FormSection({ title: string; icon?: string; defaultOpen?: boolean; children })`, `SaveBar({ dirty: boolean; saving: boolean; error?: unknown; onDiscard(): void; onSave?(): void; submit?: boolean; saved?: boolean; savedMessage?: string; problems?: readonly Problem[]; blockedReason?: string })`, `useUnsavedGuard(dirty: boolean, options?: { search?: boolean }): { guard: ReactElement | null; release(): void }`, `UnsavedGuard({ dirty: boolean; search?: boolean })`, `useDraft<T>(saved: T | undefined): { draft: T | undefined; setDraft(next: T): void; dirty: boolean; discard(): void }`.
- Produces (`lib/api.ts`): `ApiClient.forClient(clientId: string): ApiClient`.
- Produces (`App.tsx`): `appRoutes(): RouteObject[]` (exportada) y `App` sobre `createBrowserRouter` + `RouterProvider`.
- Produces (`test/renderPage.tsx`): `renderPage(path: string, element: ReactElement, pattern?: string, options?: { page?: MessageKey }): RenderResult & { router; queryClient }` y `TitleShell({ page: MessageKey; children })`, la mitad del título de pestaña que pone `AppShell`, para una prueba que monta una ficha sin el armazón.
- Produces (`test/narrowing.ts`): `recordingNarrowing<T extends object>(api: T): { api: T; narrowedTo: string[] }`, un doble de la API que anota el cliente de cada `forClient` y devuelve la misma API.
- Produces (`shell/RouteError.tsx`): `RouteError()`, la pantalla de error del router de datos, traducida.
- Todo lo de `listKit`, `detailKit` y `formKit` se reexporta desde `shell/components.tsx`: las páginas importan de un solo sitio.

**Decisiones de esta tarea** (Apéndice A):
- **D6. El chip «Cliente» y la ficha 360 leen con `X-Client-Id`, no con un `?client_id=` nuevo en el servidor.** `ApiClient.forClient(id)` devuelve un cliente de la API que manda ese cliente en cada petición. El servidor ya responde con la intersección del alcance de la sesión y ese cliente, y la base de datos lo impone (invariante 5a). Así ninguna lista necesita un parámetro nuevo en el backend del proyecto 4, y el filtro es exactamente tan estricto como el selector. Cuando el selector de arriba ya eligió un cliente, `useApiFor` lo respeta y el chip no se dibuja: la elección de arriba manda.
- **D7. El router pasa a ser de datos** (`createBrowserRouter`). `useBlocker`, lo único que permite «avisar antes de salir» dentro de la consola, solo funciona con un router de datos. Las rutas son las mismas `<Route>` de hoy dentro de una ruta de diseño que dibuja `SetupGate`; `routes-covered.test.ts` las sigue leyendo igual. Fuera de un router de datos (las pruebas que montan con `MemoryRouter`), `UnsavedGuard` solo protege la recarga y el cierre (`beforeunload`).
- **D8. `?new=<tipo>` abre el diálogo de alta de una lista.** Así cada entrada del menú «Nuevo» es una URL real y un alta se puede enlazar. Cerrar el diálogo quita el parámetro.
- **D16. Salir de una página con cambios es cambiar de ruta, y un guardado que lleva a otro sitio no pregunta** (revisión previa, I-1, reproducido con react-router 7.18.1). `useBlocker` registra su condición en un efecto, así que un manejador que limpia `dirty` y navega en el mismo paso es juzgado con la condición vieja: el alta del portal (`NewTicketPage.tsx:120-140`) se quedaba en `/portal/new` con «¿Salir sin guardar?» abierto. Por eso la guarda compara solo la ruta salvo que la página pida también la query (`search`), y `useUnsavedGuard` da un `release()` que se lee en el acto.
- **D17. El chip «Cliente» solo estrecha con un cliente de la persona, y espera a saberlo** (revisión previa, I-5). Con un identificador ajeno en la URL el servidor responde cero filas (`client_scope.py:30-36`), pero el `<select>` no tiene esa opción y enseñaría «Todos los clientes» sobre una lista vacía; y antes de que `/me` conteste, leer con la API de la consola mostraría las filas de todos bajo el chip de uno. `useNarrowing` decide las dos cosas y `ClientFilter` quita el valor ajeno.
- **D18. El router de datos tiene pantalla de error propia** (resolución del coordinador del 2026-11-24). Sin `errorElement`, una página que lanza un error enseña la de React Router, en inglés y con estilos en línea (`Unexpected Application Error!`); con `RouteError` en la ruta de diseño se ve una frase traducida con la forma de `QueryState` y un enlace al inicio.

- [ ] **Step 1: Escribir las pruebas del kit (fallan)**

`frontend/src/test/renderPage.tsx`:

```tsx
/**
 * A page under a DATA router (design §2, "Formulario": `UnsavedGuard` needs
 * one to block a navigation), with a query client that does not retry, so a
 * test can read `router.state.location` after a click. The API is injected the
 * way the rest of the suite does it: `vi.mock` of `lib/ApiContext`.
 *
 * `options.page` also gives the page the shell's half of the tab title
 * (`TitleShell`): `useRecordTitle` does nothing outside `AppShell`, so a record
 * page's "names the record in the browser tab" test needs it (pre-flight W-I3).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { useEffect, useState, type ReactElement, type ReactNode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { documentTitle, RecordTitleProvider } from "../shell/title";

/**
 * What `AppShell` does for the tab title, and nothing else: the page half is
 * the rail entry's label (a catalogue key, so it follows a language switch the
 * way the shell's does), and a record page puts its name in front.
 */
export function TitleShell({ page, children }: { page: MessageKey; children: ReactNode }) {
  const t = useT();
  const [record, setRecord] = useState<string | undefined>();
  const title = t(page);
  useEffect(() => {
    document.title = documentTitle(title, record);
  }, [title, record]);
  return <RecordTitleProvider value={setRecord}>{children}</RecordTitleProvider>;
}

export function renderPage(
  path: string,
  element: ReactElement,
  pattern = "*",
  options: { page?: MessageKey } = {},
): RenderResult & { router: ReturnType<typeof createMemoryRouter>; queryClient: QueryClient } {
  const shown = options.page ? <TitleShell page={options.page}>{element}</TitleShell> : element;
  const routes =
    pattern === "*" ? [{ path: "*", element: shown }] : [{ path: pattern, element: shown }, { path: "*", element: <p>elsewhere</p> }];
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router, queryClient };
}
```

`frontend/src/test/narrowing.ts`:

```ts
/**
 * AN API DOUBLE THAT RECORDS WHICH CLIENT EACH READ WAS NARROWED TO (decision
 * D6): every `forClient(id)` is noted and answers with this same double, so a
 * page test can assert that a list behind the Client chip read through
 * `forClient(<the chip's client>)`. Anything else is the wrapped API's own.
 */
export function recordingNarrowing<T extends object>(api: T): { api: T; narrowedTo: string[] } {
  const narrowedTo: string[] = [];
  const wrapped: T = new Proxy(api, {
    get(target, prop, receiver) {
      if (prop === "forClient") {
        return (clientId: string) => {
          narrowedTo.push(clientId);
          return wrapped;
        };
      }
      return Reflect.get(target, prop, receiver);
    },
  });
  return { api: wrapped, narrowedTo };
}
```

`frontend/src/shell/listKit.test.tsx`:

```tsx
import { useQuery } from "@tanstack/react-query";
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MeClient } from "../lib/api";
import { renderPage } from "../test/renderPage";
import {
  BulkBar,
  ClientFilter,
  FilterChips,
  ListEmpty,
  ShowingCount,
  useApiFor,
  useNarrowing,
  useNewParam,
  useUrlFilters,
} from "./listKit";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const BOGOTA = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
/** A well-formed client id that is not one of this person's (a stale or edited link). */
const FOREIGN = "9d1f0c2a-4b3e-4c5d-8e6f-7a8b9c0d1e2f";

const scope = vi.hoisted(() => ({ selected: null as string | null, clients: [] as unknown[], ready: true }));
vi.mock("../lib/clientScope", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/clientScope")>()),
  useClientScope: () => ({ selected: scope.selected, clients: scope.clients, select: vi.fn(), ready: scope.ready }),
}));
const api = vi.hoisted(() => ({ forClient: vi.fn((id: string) => ({ narrowedTo: id })) }));
vi.mock("../lib/ApiContext", () => ({ useApi: () => api }));

function client(id: string, name: string): MeClient {
  return { id, name, kind: "customer", enabled_modules: [], permissions: [] } as unknown as MeClient;
}

beforeEach(() => {
  scope.selected = null;
  scope.ready = true;
  scope.clients = [client(ANDES, "Andes Logistics"), client(BOGOTA, "Bogotá Retail")];
  api.forClient.mockClear();
});

function Fleet() {
  const filters = useUrlFilters(["state", "q"] as const);
  const online = filters.values.state === "online";
  return (
    <>
      <input aria-label="Search" value={filters.values.q} onChange={(event) => filters.set("q", event.target.value)} />
      <FilterChips
        label="State"
        options={[
          { value: "", label: "All", count: 12 },
          { value: "online", label: "Online", count: 9 },
          { value: "offline", label: "Offline" },
        ]}
        value={filters.values.state}
        onChange={(value) => filters.set("state", value)}
      />
      <ShowingCount shown={online ? 9 : 12} total={12} />
      {filters.values.state === "offline" ? (
        <ListEmpty filtered={filters.active} onClear={filters.clear} nothingYet="No computers yet." />
      ) : null}
    </>
  );
}

describe("useUrlFilters and FilterChips (design §2: filters live in the URL)", () => {
  it("reads the filter from the URL, so a filtered view is a link", () => {
    renderPage("/devices?state=online", <Fleet />);
    expect(screen.getByRole("button", { name: /Online/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /All/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("Showing 9 of 12")).toBeInTheDocument();
  });

  it("writes a chip into the URL, goes back to the first page, and drops the default", async () => {
    const { router } = renderPage("/devices?offset=50", <Fleet />);
    await userEvent.click(screen.getByRole("button", { name: /Online/ }));
    expect(router.state.location.search).toBe("?state=online");
    await userEvent.click(screen.getByRole("button", { name: /All/ }));
    expect(router.state.location.search).toBe("");
  });

  it("draws a count only where the caller has one, never a made-up zero", () => {
    renderPage("/devices", <Fleet />);
    expect(screen.getByRole("button", { name: /All\s*12/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Offline" })).toBeInTheDocument();
  });

  it("lets Back undo a chip, while typing a search adds no history entry of its own", async () => {
    const { router } = renderPage("/devices", <Fleet />);
    await userEvent.click(screen.getByRole("button", { name: /Online/ }));
    await userEvent.type(screen.getByRole("textbox", { name: "Search" }), "srv");
    expect(router.state.location.search).toBe("?state=online&q=srv");
    await act(() => router.navigate(-1));
    expect(router.state.location.search).toBe("");
  });
});

describe("ShowingCount", () => {
  it("never claims a total the server did not give", () => {
    renderPage("/x", <ShowingCount shown={50} total={null} />);
    expect(screen.getByText("Showing 50")).toBeInTheDocument();
  });
});

describe("ListEmpty (design §2: the empty state says the real cause)", () => {
  it("says nothing matches the filters, and clearing them goes back to the whole list", async () => {
    const { router } = renderPage("/devices?state=offline&q=srv", <Fleet />);
    expect(screen.getByText("Nothing matches these filters.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(router.state.location.search).toBe("");
  });

  it("says nothing exists yet, and offers the page's own action", () => {
    renderPage("/x", <ListEmpty filtered={false} onClear={vi.fn()} nothingYet="No computers yet." action={<a href="/devices/onboard">Add a computer</a>} />);
    expect(screen.getByText("No computers yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add a computer" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });
});

describe("BulkBar (design §2: only where bulk actions exist, and only with a selection)", () => {
  it("is not drawn until something is selected", () => {
    renderPage("/x", <BulkBar count={0} onClear={vi.fn()}><button type="button">Approve</button></BulkBar>);
    expect(screen.queryByRole("region", { name: "Bulk actions" })).toBeNull();
  });

  it("counts the selection and clears it", async () => {
    const clear = vi.fn();
    renderPage("/x", <BulkBar count={3} onClear={clear}><button type="button">Approve</button></BulkBar>);
    expect(screen.getByRole("region", { name: "Bulk actions" })).toHaveTextContent("3 selected");
    await userEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(clear).toHaveBeenCalledTimes(1);
  });
});

describe("ClientFilter (design §1.3: a Client chip while every client is shown)", () => {
  it("offers every client of the person, drawn as a chip", () => {
    renderPage("/x", <ClientFilter value="" onChange={vi.fn()} />);
    const select = screen.getByRole("combobox", { name: "Client" });
    expect([...(select as HTMLSelectElement).options].map((option) => option.textContent)).toEqual([
      "All clients",
      "Andes Logistics",
      "Bogotá Retail",
    ]);
    // Design §1.3 says a "Client" chip: a select in the chip's shape, pinned
    // so a refactor cannot drop the shape (spec pre-flight M-11).
    expect(select.closest(".chip")).toHaveClass("chip-select");
  });

  it("drops a client the person does not have from the URL, instead of drawing All clients over an empty list", () => {
    const onChange = vi.fn();
    renderPage("/x", <ClientFilter value={FOREIGN} onChange={onChange} />);
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("is not drawn when the top bar has chosen a client, because that choice already decides", () => {
    scope.selected = ANDES;
    renderPage("/x", <ClientFilter value="" onChange={vi.fn()} />);
    expect(screen.queryByRole("combobox", { name: "Client" })).toBeNull();
  });
});

function Probe({ id }: { id: string }) {
  const narrowed = useApiFor(id) as unknown as { narrowedTo?: string };
  return <p>{narrowed.narrowedTo ?? "console"}</p>;
}

describe("useApiFor (decision D6)", () => {
  it("narrows to the chip's client with X-Client-Id", () => {
    renderPage("/x", <Probe id={ANDES} />);
    expect(screen.getByText(ANDES)).toBeInTheDocument();
  });

  it("leaves the console's own client alone when the chip is empty, or the top bar has chosen", () => {
    renderPage("/x", <Probe id="" />);
    expect(screen.getByText("console")).toBeInTheDocument();
    scope.selected = BOGOTA;
    renderPage("/y", <Probe id={ANDES} />);
    expect(screen.getAllByText("console")).toHaveLength(2);
  });

  it("never sends a value that is not a client id", () => {
    renderPage("/x", <Probe id="not-a-uuid" />);
    expect(screen.getByText("console")).toBeInTheDocument();
    expect(api.forClient).not.toHaveBeenCalled();
  });

  it("never sends a client the person does not have (decision D17)", () => {
    renderPage("/x", <Probe id={FOREIGN} />);
    expect(screen.getByText("console")).toBeInTheDocument();
    expect(api.forClient).not.toHaveBeenCalled();
  });
});

function Decided({ id }: { id: string }) {
  const narrowing = useNarrowing(id);
  return <p>{narrowing.ready ? (narrowing.client ?? "console") : "waiting"}</p>;
}

/** A list behind the chip, keyed on the narrowed client the way Receta L keys one. */
function ChipList() {
  const filters = useUrlFilters(["client"] as const);
  const narrowing = useNarrowing(filters.values.client);
  const narrowed = useApiFor(filters.values.client) as unknown as { narrowedTo?: string };
  const list = useQuery({
    queryKey: ["probe", narrowing.client],
    queryFn: async () => narrowed.narrowedTo ?? "console",
    enabled: narrowing.ready,
  });
  return (
    <>
      <ClientFilter value={filters.values.client} onChange={(value) => filters.set("client", value)} />
      <p>read as {list.data ?? "nothing yet"}</p>
    </>
  );
}

describe("useNarrowing (decision D17)", () => {
  it("decides nothing before /me has listed the person's clients, so a list reads nothing", () => {
    scope.ready = false;
    renderPage("/x", <Decided id={ANDES} />);
    expect(screen.getByText("waiting")).toBeInTheDocument();
  });

  it("is decided at once for an empty chip, or when the top bar has chosen", () => {
    scope.ready = false;
    renderPage("/x", <Decided id="" />);
    scope.selected = BOGOTA;
    renderPage("/y", <Decided id={ANDES} />);
    expect(screen.getAllByText("console")).toHaveLength(2);
  });

  it("reads again under the new client when the chip changes, so no row of the last one stays", async () => {
    renderPage("/x", <ChipList />);
    expect(await screen.findByText("read as console")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Client" }), ANDES);
    expect(await screen.findByText(`read as ${ANDES}`)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Client" }), BOGOTA);
    expect(await screen.findByText(`read as ${BOGOTA}`)).toBeInTheDocument();
    expect(api.forClient).toHaveBeenCalledWith(ANDES);
    expect(api.forClient).toHaveBeenCalledWith(BOGOTA);
  });
});

function Creating() {
  const created = useNewParam("ticket");
  return (
    <>
      <button type="button" onClick={created.start}>New ticket</button>
      {created.open ? (
        <div role="dialog" aria-label="New ticket">
          <button type="button" onClick={created.close}>Close</button>
        </div>
      ) : null}
    </>
  );
}

describe("useNewParam (decision D8)", () => {
  it("opens a create dialog from ?new=, which is what the New menu links to", () => {
    renderPage("/service-desk?new=ticket", <Creating />);
    expect(screen.getByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
  });

  it("puts the dialog in the URL when opened here, and takes it out when closed", async () => {
    const { router } = renderPage("/service-desk?state=open", <Creating />);
    await userEvent.click(screen.getByRole("button", { name: "New ticket" }));
    expect(router.state.location.search).toBe("?state=open&new=ticket");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(router.state.location.search).toBe("?state=open");
    await act(() => router.navigate("/service-desk?new=other"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
```

`frontend/src/shell/detailKit.test.tsx`:

```tsx
import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderPage } from "../test/renderPage";
import { DetailHead, SectionNav, UrlTabs, useUrlTab } from "./detailKit";

const TABS = ["overview", "hardware", "software"] as const;
type Tab = (typeof TABS)[number];
const LABEL: Record<Tab, string> = { overview: "Overview", hardware: "Hardware", software: "Software" };

function Device() {
  const [tab, setTab] = useUrlTab<Tab>(TABS, "overview");
  return (
    <>
      <DetailHead
        back={{ to: "/devices", label: "All computers" }}
        title="srv-app02"
        status={{ kind: "ok", label: "Online" }}
        context={<span>Andes Logistics · Bogotá</span>}
        actions={<button type="button" className="btn">Connect</button>}
      />
      <UrlTabs label="Computer" items={TABS.map((key) => ({ key, label: LABEL[key] }))} active={tab} onSelect={setTab}>
        <p>{tab} panel</p>
      </UrlTabs>
    </>
  );
}

describe("DetailHead (design §2, Ficha)", () => {
  it("leads back to the list, names the record, its status and its context", () => {
    renderPage("/devices/dev-1", <Device />);
    expect(screen.getByRole("link", { name: "All computers" })).toHaveAttribute("href", "/devices");
    expect(screen.getByRole("heading", { level: 1, name: "srv-app02" })).toBeInTheDocument();
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Andes Logistics · Bogotá")).toBeInTheDocument();
  });
});

describe("UrlTabs and useUrlTab (design §2: tabs that change the URL)", () => {
  it("opens the tab the URL names", () => {
    renderPage("/devices/dev-1?tab=hardware", <Device />);
    expect(screen.getByRole("tab", { name: "Hardware" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel", { name: "Hardware" })).toHaveTextContent("hardware panel");
    expect(screen.getByRole("tablist", { name: "Computer" })).toBeInTheDocument();
  });

  it("writes a tab into the URL as a new history entry, and the default tab writes nothing", async () => {
    const { router } = renderPage("/devices/dev-1", <Device />);
    await userEvent.click(screen.getByRole("tab", { name: "Software" }));
    expect(router.state.location.search).toBe("?tab=software");
    await act(() => router.navigate(-1));
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    await userEvent.click(screen.getByRole("tab", { name: "Overview" }));
    expect(router.state.location.search).toBe("");
  });

  it("falls back to the default for a tab it does not know", () => {
    renderPage("/devices/dev-1?tab=nope", <Device />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the strip on one line, as one scrolling row (design §1.3)", () => {
    renderPage("/devices/dev-1", <Device />);
    expect(screen.getByRole("tablist")).toHaveClass("scroll");
  });
});

describe("SectionNav", () => {
  it("is a navigation of links that marks the current page, not a tablist", () => {
    renderPage(
      "/deploy/packages",
      <SectionNav
        label="Software"
        items={[
          { to: "/deploy", label: "Deployments", end: true },
          { to: "/deploy/packages", label: "Packages" },
        ]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "Software" });
    expect(nav).not.toHaveAttribute("role", "tablist");
    expect(screen.getByRole("link", { name: "Packages" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Deployments" })).not.toHaveAttribute("aria-current");
  });
});
```

`frontend/src/shell/formKit.test.tsx`:

```tsx
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { renderPage } from "../test/renderPage";
import { FormPage, FormSection, SaveBar, UnsavedGuard, useDraft, useUnsavedGuard } from "./formKit";

interface Org {
  name: string;
}

function Settings({ initial, onSave }: { initial: Org; onSave: (org: Org) => void }) {
  const [saved, setSaved] = useState<Org>(initial);
  const { draft, setDraft, dirty, discard } = useDraft(saved);
  return (
    <FormPage back={{ to: "/admin", label: "Admin" }} title="Settings">
      <UnsavedGuard dirty={dirty} />
      <FormSection title="Organization" icon="settings">
        <label>
          Name
          <input value={draft?.name ?? ""} onChange={(event) => setDraft({ name: event.target.value })} />
        </label>
      </FormSection>
      <SaveBar
        dirty={dirty}
        saving={false}
        onDiscard={discard}
        onSave={() => {
          if (draft) {
            onSave(draft);
            setSaved(draft);
          }
        }}
      />
    </FormPage>
  );
}

describe("SaveBar and useDraft (design §2: one save bar for the whole console)", () => {
  it("is not drawn until something changed, and Discard puts the page back", async () => {
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    expect(screen.getByRole("region", { name: "Unsaved changes" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("saves through the page, and goes away once the server has the change", async () => {
    const save = vi.fn();
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={save} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(save).toHaveBeenCalledWith({ name: "Novieri SAS" });
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("says the save failed in the bar, and keeps the change", () => {
    renderPage(
      "/x",
      <SaveBar dirty saving={false} error={new Error("The server refused it.")} onDiscard={vi.fn()} onSave={vi.fn()} />,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  // What the per-panel buttons it replaces did (settings' SaveBar, the
  // ticket's "Save changes"), so a page loses none of it by moving here.
  it("says it saved once the server answered, until the next change", () => {
    renderPage("/x", <SaveBar dirty={false} saving={false} saved onDiscard={vi.fn()} onSave={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
    expect(screen.queryByRole("region", { name: "Unsaved changes" })).toBeNull();
  });

  it("says why it cannot save, and does not offer to", () => {
    renderPage(
      "/x",
      <SaveBar dirty saving={false} blockedReason="A subject is required." onDiscard={vi.fn()} onSave={vi.fn()} />,
    );
    expect(screen.getByText("A subject is required.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("submits the page's form when asked, so Enter in a field still saves", async () => {
    const onSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    renderPage(
      "/x",
      <form onSubmit={onSubmit}>
        <input aria-label="Name" defaultValue="a" />
        <SaveBar dirty saving={false} submit onDiscard={vi.fn()} />
      </form>,
    );
    await userEvent.type(screen.getByLabelText("Name"), "{Enter}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});

// LEAVING IS A CLICK ON A LINK, as a person leaves: the form's own back link.
// (A bare `router.navigate()` that the blocker holds leaves a pending
// transition behind, and React reports it as an un-awaited suspension.)
describe("UnsavedGuard (design §5: a page with changes warns before it is left)", () => {
  it("asks before leaving, and staying keeps the page and the change", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/settings");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(router.state.location.pathname).toBe("/settings");
    expect(screen.getByLabelText("Name")).toHaveValue("Novieri SAS");
  });

  it("leaves when the person says so", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    await userEvent.click(screen.getByRole("button", { name: "Leave without saving" }));
    expect(router.state.location.pathname).toBe("/admin");
  });

  it("lets a clean page go without asking", async () => {
    const { router } = renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.click(screen.getByRole("link", { name: "Admin" }));
    expect(router.state.location.pathname).toBe("/admin");
  });

  it("asks the browser too, for a reload or a closed tab", async () => {
    renderPage("/settings", <Settings initial={{ name: "Novieri" }} onSave={vi.fn()} />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), " SAS");
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });
});

/** An alta that leaves for what it created, the portal's New ticket shape. */
function Creating({ releasing }: { releasing: boolean }) {
  const [title, setTitle] = useState("");
  const { guard, release } = useUnsavedGuard(title !== "");
  const navigate = useNavigate();
  return (
    <FormPage back={{ to: "/tickets", label: "Tickets" }} title="New ticket">
      {guard}
      <label>
        Title
        <input value={title} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <button
        type="button"
        onClick={() => {
          if (releasing) release();
          navigate("/tickets/t-1");
        }}
      >
        Send
      </button>
    </FormPage>
  );
}

/** A form whose links change only the query: a tab, the way Settings has them. */
function Tabbed({ search }: { search?: boolean }) {
  const [name, setName] = useState("");
  return (
    <>
      <UnsavedGuard dirty={name !== ""} search={search} />
      <label>
        Name
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <Link to="/settings?tab=email">Email</Link>
    </>
  );
}

describe("UnsavedGuard, what leaving is (decision D16)", () => {
  it("lets a save that leaves go without asking", async () => {
    const { router } = renderPage("/tickets/new", <Creating releasing />, "/tickets/new");
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(router.state.location.pathname).toBe("/tickets/t-1");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("is not vacuous: the same navigation without release() asks", async () => {
    const { router } = renderPage("/tickets/new", <Creating releasing={false} />, "/tickets/new");
    await userEvent.type(screen.getByLabelText("Title"), "Printer");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/tickets/new");
  });

  it("does not call a change of query alone leaving (a tab, a filter, ?new=)", async () => {
    const { router } = renderPage("/settings", <Tabbed />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Email" }));
    expect(router.state.location.search).toBe("?tab=email");
    expect(screen.queryByRole("dialog", { name: "Leave without saving?" })).toBeNull();
  });

  it("asks on a change of query too where the page says its query is what it edits", async () => {
    const { router } = renderPage("/settings", <Tabbed search />, "/settings");
    await userEvent.type(screen.getByLabelText("Name"), "x");
    await userEvent.click(screen.getByRole("link", { name: "Email" }));
    expect(screen.getByRole("dialog", { name: "Leave without saving?" })).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });
});

describe("FormSection", () => {
  it("folds and unfolds, and says which it is", async () => {
    renderPage("/x", <FormSection title="Mail">x</FormSection>);
    const toggle = screen.getByRole("button", { name: "Mail" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("x")).not.toBeVisible();
  });
});
```

`frontend/src/lib/api.forClient.test.ts`:

```ts
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./api";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";

afterEach(() => vi.unstubAllGlobals());

function stubFetch() {
  const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response("[]", { status: 200 })));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const headersOf = (fetchMock: ReturnType<typeof vi.fn>, call: number) =>
  new Headers((fetchMock.mock.calls[call] as [string, RequestInit])[1].headers);

describe("ApiClient.forClient (decision D6)", () => {
  it("narrows every request to the one client it was made for, whatever the top bar says", async () => {
    const fetchMock = stubFetch();
    const console = new ApiClient(vi.fn(), "/api/v1", () => null);
    await console.forClient(ANDES).listLicenses();
    expect(headersOf(fetchMock, 0).get("X-Client-Id")).toBe(ANDES);
    await console.listLicenses();
    expect(headersOf(fetchMock, 1).get("X-Client-Id")).toBeNull();
  });

  it("keeps the session's own handling of a 401", async () => {
    const onUnauthorized = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 401 })));
    await expect(new ApiClient(onUnauthorized).forClient(ANDES).listLicenses()).rejects.toThrow();
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
```

`frontend/src/App.router.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";

import { appRoutes } from "./App";

describe("the console's router (decision D7)", () => {
  it("is a data router over the same routes, under the setup gate", () => {
    const routes = appRoutes();
    expect(routes).toHaveLength(1);
    const paths = (routes[0]?.children ?? []).map((route) => route.path);
    expect(paths).toContain("/devices");
    expect(paths).toContain("/devices/:deviceId");
    expect(paths).toContain("/clients/:clientId");
    expect(paths).toContain("*");
  });
});
```

- [ ] **Step 2: Ejecutarlas y verlas fallar**

Run: `cd frontend && npx vitest run src/shell/listKit.test.tsx src/shell/detailKit.test.tsx src/shell/formKit.test.tsx src/lib/api.forClient.test.ts src/App.router.test.tsx`
Expected: FAIL: no existen `./listKit`, `./detailKit`, `./formKit`, `forClient` ni `appRoutes`.

- [ ] **Step 3: Los textos del kit**

`frontend/src/i18n/en/shell.ts`, después del bloque `ui`:

```ts
  // The pattern kit (shell/listKit.tsx, detailKit.tsx, formKit.tsx; design §2).
  kit: {
    showing: "Showing {shown} of {total}",
    showingSome: "Showing {shown}",
    nothingMatches: "Nothing matches these filters.",
    clearFilters: "Clear filters",
    client: "Client",
    allClients: "All clients",
    bulkActions: "Bulk actions",
    selected: { one: "{count} selected", other: "{count} selected" },
    clearSelection: "Clear selection",
    unsaved: "Unsaved changes",
    saving: "Saving…",
    discard: "Discard",
    save: "Save",
    saved: "Saved.",
    leaveTitle: "Leave without saving?",
    leaveBody: "The changes on this page have not been saved. If you leave, they are lost.",
    leave: "Leave without saving",
  },
  // The data router's error page (shell/RouteError.tsx; decision D18).
  routeError: {
    title: "This page could not be shown",
    body: "Something on this page failed while it was being drawn. Nothing was changed. Reload the page, or go back to the dashboard.",
    home: "Go to the dashboard",
  },
```

`frontend/src/i18n/es/shell.ts`, en el mismo sitio:

```ts
  kit: {
    showing: "Mostrando {shown} de {total}",
    showingSome: "Mostrando {shown}",
    nothingMatches: "Nada coincide con estos filtros.",
    clearFilters: "Quitar filtros",
    client: "Cliente",
    allClients: "Todos los clientes",
    bulkActions: "Acciones en lote",
    selected: { one: "{count} seleccionado", other: "{count} seleccionados" },
    clearSelection: "Quitar selección",
    unsaved: "Cambios sin guardar",
    saving: "Guardando…",
    discard: "Descartar",
    save: "Guardar",
    saved: "Guardado.",
    leaveTitle: "¿Salir sin guardar?",
    leaveBody: "Los cambios de esta página no se han guardado. Si sales, se pierden.",
    leave: "Salir sin guardar",
  },
  routeError: {
    title: "No se pudo mostrar esta página",
    body: "Algo de esta página falló mientras se dibujaba. No se cambió nada. Recarga la página o vuelve al inicio.",
    home: "Ir al inicio",
  },
```

- [ ] **Step 3b: `identical-ok.ts`, partido por oleada** (revisión previa, W-I1 y F-M10)

`src/i18n/identical-ok.ts` es un solo objeto con 154 entradas de todos los módulos, y cualquier oleada que deja idéntica una clave en los dos catálogos (VRF, VLAN, «Ticket», «Software») o que renombra una que ya está listada tiene que tocarlo: ocho oleadas añadiendo al final del mismo literal chocan. Se parte una vez, aquí, y cada oleada pasa a tener el suyo.

1. Prueba primero. En `frontend/src/i18n/catalogue.identical.test.ts`, la importación `import { IDENTICAL_OK } from "./identical-ok";` pasa a `import { IDENTICAL_OK, IDENTICAL_OK_SHARED, SHARED_NAMESPACES, WAVE_ENTRIES, WAVE_NAMESPACES } from "./identical-ok";`, y antes del caso «the audit itself finds a pasted English value, and a plural form»:

```ts
  it("keeps each wave's entries in that wave's own file, and only the shared ones in identical-ok.ts (redesign plan, shared files)", () => {
    const namespace = (path: string) => path.split(".")[0] as string;
    for (const [wave, entries] of Object.entries(WAVE_ENTRIES)) {
      const foreign = Object.keys(entries).filter((path) => !(WAVE_NAMESPACES[wave] ?? []).includes(namespace(path)));
      expect(foreign, `identical-ok.${wave}.ts lists another wave's keys: ${foreign.join(", ")}`).toEqual([]);
    }
    const unshared = Object.keys(IDENTICAL_OK_SHARED).filter((path) => !SHARED_NAMESPACES.includes(namespace(path)));
    expect(unshared, `identical-ok.ts lists a wave's keys: ${unshared.join(", ")}`).toEqual([]);
    // Every catalogue has an owner, so a new identical key always has a file to go to.
    const owned = new Set([...SHARED_NAMESPACES, ...Object.values(WAVE_NAMESPACES).flat()]);
    expect(Object.keys(en).filter((name) => !owned.has(name))).toEqual([]);
  });
```

Run: `cd frontend && npx vitest run src/i18n/catalogue.identical.test.ts`
Expected: FAIL (no existen `IDENTICAL_OK_SHARED`, `SHARED_NAMESPACES`, `WAVE_ENTRIES` ni `WAVE_NAMESPACES`).

2. Guardar este guion como `"${TMPDIR:-/tmp}/split-identical-ok.mjs"` (fuera del repositorio: se usa una vez y no se versiona; si la sesión tiene una carpeta temporal propia, ahí) y ejecutarlo desde `frontend/` con `node "${TMPDIR:-/tmp}/split-identical-ok.mjs"`:

```js
// Run once from frontend/: node <this file>. Moves each wave's IDENTICAL_OK
// entries into src/i18n/identical-ok.o<N>.ts and leaves the shared ones.
import { readFileSync, writeFileSync } from "node:fs";

const WAVES = {
  o1: ["devices", "inventory", "onboarding", "agentPush", "agentReleases"],
  o2: ["dashboard", "serviceDesk", "sla", "emailIntake", "notification", "portal"],
  o3: ["health", "patch", "deploy", "remote", "jobs", "errorMonitor", "platformStatus", "security", "audit", "auditVocabulary"],
  o4: ["backup", "bcp", "provisioning", "siteServers", "enrollment"],
  o5: ["licenses", "subscriptions", "ipam"],
  o6: ["docs", "change", "budget", "people", "credentials", "reporting"],
  o7: ["settings", "profile", "aiAdmin", "aiAutomation"],
  o8: ["auth", "setup"],
};
const TASK = { o1: 6, o2: 7, o3: 8, o4: 9, o5: 10, o6: 11, o7: 12, o8: 13 };
const FILE = "src/i18n/identical-ok.ts";
const source = readFileSync(FILE, "utf8");
const open = source.indexOf("export const IDENTICAL_OK: Record<string, string> = {");
const close = source.indexOf("\n};", open);
const header = source.slice(0, open);
const lines = source.slice(source.indexOf("\n", open) + 1, close).split("\n");
const entry = /^  "([a-zA-Z]+)\.[^"]*": ".*",$/;
const byWave = Object.fromEntries(Object.keys(WAVES).map((wave) => [wave, []]));
const shared = [];
for (const line of lines) {
  const match = entry.exec(line);
  if (!match) throw new Error(`not a one-line entry: ${line}`);
  const wave = Object.keys(WAVES).find((key) => WAVES[key].includes(match[1]));
  (wave ? byWave[wave] : shared).push(line);
}
for (const [wave, namespaces] of Object.entries(WAVES)) {
  const name = `IDENTICAL_OK_${wave.toUpperCase()}`;
  writeFileSync(
    `src/i18n/identical-ok.${wave}.ts`,
    `/**
 * IDENTICAL_OK entries of wave ${wave.toUpperCase()} (Task ${TASK[wave]}): the catalogues ${namespaces.map((n) => `\`${n}\``).join(", ")}.
 * The wave adds and removes its own entries here and nowhere else, so the eight
 * waves that run in parallel never edit one file (redesign plan, "Archivos
 * compartidos"); \`catalogue.identical.test.ts\` holds each file to its catalogues.
 */
export const ${name}: Record<string, string> = {
${byWave[wave].join("\n")}${byWave[wave].length ? "\n" : ""}};
`,
  );
}
const imports = Object.keys(WAVES).map((wave) => `import { IDENTICAL_OK_${wave.toUpperCase()} } from "./identical-ok.${wave}";`).join("\n");
writeFileSync(
  FILE,
  `${imports}

${header.trimEnd()}

/** The catalogues each wave owns (redesign plan, "Archivos compartidos"). */
export const WAVE_NAMESPACES: Readonly<Record<string, readonly string[]>> = ${JSON.stringify(WAVES, null, 2).replace(/"([a-z0-9]+)":/g, "$1:")};

/** The catalogues the base tasks own; their entries stay in this file. */
export const SHARED_NAMESPACES: readonly string[] = ["common", "components", "shell", "enums", "lib", "time", "validation", "clients"];

/** The shared entries (the base tasks'). */
export const IDENTICAL_OK_SHARED: Record<string, string> = {
${shared.join("\n")}
};

/** Each wave's file, by wave. */
export const WAVE_ENTRIES: Readonly<Record<string, Record<string, string>>> = {
${Object.keys(WAVES).map((wave) => `  ${wave}: IDENTICAL_OK_${wave.toUpperCase()},`).join("\n")}
};

export const IDENTICAL_OK: Record<string, string> = {
  ...IDENTICAL_OK_SHARED,
${Object.keys(WAVES).map((wave) => `  ...IDENTICAL_OK_${wave.toUpperCase()},`).join("\n")}
};
`,
);
console.log(`shared ${shared.length}, ${Object.entries(byWave).map(([wave, list]) => `${wave} ${list.length}`).join(", ")}`);
```

Expected: imprime `shared 28, o1 19, o2 20, o3 23, o4 17, o5 27, o6 10, o7 10, o8 0` (154 entradas, las de hoy; medido en una copia de trabajo el 2026-11-24), crea `src/i18n/identical-ok.o1.ts` a `identical-ok.o8.ts` (el de la O8 vacío) y reescribe `identical-ok.ts` con las importaciones, las 28 entradas compartidas en `IDENTICAL_OK_SHARED` y el `IDENTICAL_OK` de siempre como su unión. Si el número de una oleada no coincide, alguien añadió una entrada después del 2026-11-24: no es un fallo, siempre que la suma sea el número de entradas que tenía el archivo (`grep -c '^  "' src/i18n/identical-ok.ts` antes de ejecutarlo).

Run: `npx vitest run src/i18n && npm run typecheck`
Expected: PASS; `IDENTICAL_OK` tiene las mismas 154 claves que antes, así que `catalogue.identical.test.ts` no cambia de veredicto.

Desde aquí, **la Task 4 y la Task 5 añaden sus entradas a `IDENTICAL_OK_SHARED`** (`shell.*`, `clients.*`) y cada oleada a su `identical-ok.o<N>.ts`.

- [ ] **Step 4: `listKit.tsx`**

```tsx
/**
 * THE LIST PATTERN (design §2, "Lista"; DESIGN.md v4, "Lists"). One way every
 * list in the console filters, counts, narrows to a client, selects and says
 * why it is empty. A page composes these pieces; no page draws its own.
 *
 * EVERYTHING IS IN THE URL (design §2, and the reason Atera's walkthrough
 * gives for competing on consistency): a filtered view is a link, a reload
 * keeps it, Back undoes a chip.
 */
import { useCallback, useEffect, useMemo, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

import { useT } from "../i18n/context";
import type { ApiClient } from "../lib/api";
import { useApi } from "../lib/ApiContext";
import { isClientId } from "../lib/clientChoice";
import { useClientScope } from "../lib/clientScope";
import { Btn, FilterBar, ResultCount } from "./components";
import { Icon } from "./icons";
import "./kit.css";

/** The page cursor of the lists that page; any filter change starts again from the first page. */
const CURSOR = "offset";
/** The search box's key: typing in it replaces the history entry instead of adding one per keystroke. */
const SEARCH = "q";

export interface UrlFilters<K extends string> {
  /** Every key's value, `""` when the URL does not carry it. */
  readonly values: Readonly<Record<K, string>>;
  /** Whether any of the keys holds a value: what tells "nothing yet" from "nothing matches". */
  readonly active: boolean;
  /**
   * `""` removes the key. The search (`q`) replaces the history entry, so
   * typing is not twenty Backs; any other key adds one, so Back undoes a chip.
   */
  set(key: K, value: string): void;
  /** Removes every key: a new history entry, so Back brings the filters back. */
  clear(): void;
}

export function useUrlFilters<K extends string>(keys: readonly K[]): UrlFilters<K> {
  const [params, setParams] = useSearchParams();
  const signature = keys.join("|");
  const values = useMemo(
    // `keys` is usually a literal re-created on every render; its contents
    // (`signature`) are what decide whether the values changed.
    () => Object.fromEntries(signature.split("|").map((key) => [key, params.get(key) ?? ""])) as Record<K, string>,
    [params, signature],
  );
  const active = keys.some((key) => (params.get(key) ?? "") !== "");
  const set = useCallback(
    (key: K, value: string) =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (value === "") next.delete(key);
          else next.set(key, value);
          next.delete(CURSOR);
          return next;
        },
        { replace: key === SEARCH },
      ),
    [setParams],
  );
  const clear = useCallback(
    () =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        for (const key of signature.split("|")) next.delete(key);
        next.delete(CURSOR);
        return next;
      }),
    [setParams, signature],
  );
  return { values, active, set, clear };
}

export interface FilterChipOption {
  /** `""` is "every value": the default, which writes nothing to the URL. */
  readonly value: string;
  readonly label: string;
  /** From the server. Absent is drawn as no number at all, never as 0 (The Absent Zero Rule). */
  readonly count?: number;
}

/** One filter as a row of toggles, each with its count (DESIGN.md v4, "Chips"). */
export function FilterChips({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly FilterChipOption[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="chips" role="group" aria-label={label}>
      {options.map((option) => {
        const on = option.value === value;
        return (
          <button
            key={option.value === "" ? "(all)" : option.value}
            type="button"
            className={on ? "chip on" : "chip"}
            aria-pressed={on}
            onClick={() => onChange(option.value)}
          >
            {/* The second carrier: the selected chip says so by shape too. */}
            {on ? <Icon name="circle-check" size={13} /> : null}
            {option.label}
            {option.count === undefined ? null : <span className="n">{option.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

/** "Showing 9 of 12" -- or "Showing 50" when the server gave no total, which is never guessed. */
export function ShowingCount({ shown, total }: { shown: number; total: number | null }) {
  const t = useT();
  return (
    <ResultCount>{total === null ? t("shell.kit.showingSome", { shown }) : t("shell.kit.showing", { shown, total })}</ResultCount>
  );
}

/** The bulk bar: only with a selection, so it is never a row of dead buttons (Atera's flaw). */
export function BulkBar({ count, onClear, children }: { count: number; onClear: () => void; children: ReactNode }) {
  const t = useT();
  if (count === 0) return null;
  return (
    <div className="bulkbar" role="region" aria-label={t("shell.kit.bulkActions")}>
      <span className="bulkbar-count">{t("shell.kit.selected", { count })}</span>
      {children}
      <span className="filter-spacer" />
      <Btn label={t("shell.kit.clearSelection")} variant="ghost" small onClick={onClear} />
    </div>
  );
}

/**
 * THE EMPTY LIST SAYS ITS REAL CAUSE (design §2): nothing exists yet, with the
 * page's action; or nothing matches these filters, with the way out. Atera
 * says "try adjusting the filters" on an account with no devices at all.
 */
export function ListEmpty({
  filtered,
  onClear,
  nothingYet,
  action,
  icon = "list",
}: {
  filtered: boolean;
  onClear: () => void;
  nothingYet: string;
  action?: ReactNode;
  icon?: string;
}) {
  const t = useT();
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name={filtered ? "filter" : icon} size={26} />
      </span>
      <b>{filtered ? t("shell.kit.nothingMatches") : nothingYet}</b>
      {filtered ? <Btn label={t("shell.kit.clearFilters")} small onClick={onClear} /> : (action ?? null)}
    </div>
  );
}

/**
 * THE CLIENT CHIP (design §1.3): a list of one client's things shown with
 * every client gets a "Client" filter. Not drawn when the top bar has chosen a
 * client -- that choice already decides what this session sees -- nor for a
 * person with fewer than two clients, where it could change nothing.
 */
export function ClientFilter({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const t = useT();
  const { selected, clients, ready } = useClientScope();
  // A CLIENT THE PERSON DOES NOT HAVE (a stale or edited link) leaves the URL
  // once /me has listed theirs, rather than being drawn as "All clients" over
  // the empty list the server answers for it (decision D17).
  const foreign = ready && value !== "" && !clients.some((client) => client.id === value);
  useEffect(() => {
    if (foreign) onChange("");
  }, [foreign, onChange]);
  if (selected !== null || clients.length < 2) return null;
  return (
    <span className={value === "" ? "chip chip-select" : "chip chip-select on"}>
      <span aria-hidden="true">{t("shell.kit.client")}</span>
      <select aria-label={t("shell.kit.client")} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">{t("shell.kit.allClients")}</option>
        {clients.map((client) => (
          <option key={client.id} value={client.id}>
            {client.name}
          </option>
        ))}
      </select>
    </span>
  );
}

/**
 * WHAT THE CHIP NARROWS TO, AND WHETHER THAT IS DECIDED (decisions D6, D17).
 * The top bar's choice wins when there is one, and an empty chip is the
 * console's own choice: both decided at once. Otherwise the chip's client,
 * only once `/me` has said it is one of the person's: until then `ready` is
 * false and a list reads nothing, so every client's rows never sit under one
 * client's chip. A value that is not one of theirs is never sent (the server
 * answers a malformed header with 400, and a foreign client with zero rows).
 */
export function useNarrowing(clientId: string): { client: string | null; ready: boolean } {
  const { selected, clients, ready } = useClientScope();
  if (clientId === "" || selected !== null) return { client: null, ready: true };
  if (!ready) return { client: null, ready: false };
  const mine = isClientId(clientId) && clients.some((client) => client.id === clientId);
  return { client: mine ? clientId : null, ready: true };
}

/**
 * THE API, NARROWED TO THE CHIP'S CLIENT (decision D6). `X-Client-Id` narrows
 * and never widens; the database enforces it (invariant 5a). For READS: a
 * write keeps `useApi()`, so the chip never changes where a write goes.
 */
export function useApiFor(clientId: string): ApiClient {
  const api = useApi();
  const { client } = useNarrowing(clientId);
  return useMemo(() => (client === null ? api : api.forClient(client)), [api, client]);
}

/** `?new=<kind>` opens the page's create dialog (decision D8), so the New menu can link to it. */
export function useNewParam(kind: string): { open: boolean; start: () => void; close: () => void } {
  const [params, setParams] = useSearchParams();
  const open = params.get("new") === kind;
  const start = useCallback(
    () =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        next.set("new", kind);
        return next;
      }),
    [setParams, kind],
  );
  const close = useCallback(
    () =>
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.delete("new");
          return next;
        },
        { replace: true },
      ),
    [setParams],
  );
  return { open, start, close };
}

/** The head of a list, flush inside its bare panel: search, chips, then the count at the end. */
export function ListToolbar({ search, chips, end }: { search?: ReactNode; chips?: ReactNode; end?: ReactNode }) {
  return (
    <FilterBar>
      {search}
      {chips ? <div className="list-chips">{chips}</div> : null}
      <span className="filter-spacer" />
      {end}
    </FilterBar>
  );
}
```

- [ ] **Step 5: `detailKit.tsx`**

```tsx
/**
 * THE RECORD PATTERN (design §2, "Ficha"; Atera's device page, capture "atera
 * 8"): a head with the record's status, context and actions; summary tiles;
 * one row of tabs that change the URL, so a tab reloads and links -- which
 * Atera's do not; and, where a record has both, information on the left and
 * metrics and activity on the right.
 */
import { useCallback, useId, type ReactNode } from "react";
import { Link, NavLink, useSearchParams } from "react-router-dom";

import { useClientScope } from "../lib/clientScope";
import { Tabs } from "./components";
import { Icon } from "./icons";
import { Pill, type Kind } from "./viz";
import "./kit.css";

export function DetailHead({
  back,
  title,
  status,
  context,
  actions,
}: {
  back: { to: string; label: string };
  title: string;
  status?: { kind: Kind; label: string };
  context?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="ph detail-head">
      <Link className="back-link" to={back.to}>
        <Icon name="chevron-left" size={14} />
        {back.label}
      </Link>
      <div className="ph-row">
        <div>
          <h1>{title}</h1>
          {status || context ? (
            <div className="detail-meta">
              {status ? <Pill kind={status.kind}>{status.label}</Pill> : null}
              {context ? <span className="detail-context">{context}</span> : null}
            </div>
          ) : null}
        </div>
        {actions ? <div className="ph-act">{actions}</div> : null}
      </div>
    </section>
  );
}

/**
 * The tab a record shows, from `?tab=` (or `param`). The default tab writes
 * nothing; an unknown one falls back to it. A change is a NEW history entry,
 * so Back undoes a tab change instead of leaving the record.
 */
export function useUrlTab<K extends string>(keys: readonly K[], fallback: K, param = "tab"): [K, (key: K) => void] {
  const [params, setParams] = useSearchParams();
  const requested = params.get(param) ?? "";
  const active = (keys as readonly string[]).includes(requested) ? (requested as K) : fallback;
  const select = useCallback(
    (key: K) =>
      setParams((current) => {
        const next = new URLSearchParams(current);
        if (key === fallback) next.delete(param);
        else next.set(param, key);
        return next;
      }),
    [setParams, fallback, param],
  );
  return [active, select];
}

/** One row of tabs (it scrolls; it never wraps into two, Atera's flaw) and the panel they switch. */
export function UrlTabs<K extends string>({
  items,
  active,
  onSelect,
  label,
  children,
}: {
  items: readonly { key: K; label: string; count?: number }[];
  active: K;
  onSelect: (key: K) => void;
  label: string;
  children: ReactNode;
}) {
  const panelId = useId();
  const current = items.find((item) => item.key === active);
  return (
    <>
      <Tabs items={items} active={active} onSelect={(key) => onSelect(key as K)} panelId={panelId} label={label} scroll />
      <div id={panelId} role="tabpanel" aria-label={current?.label} tabIndex={-1} className="tab-panel">
        {children}
      </div>
    </>
  );
}

/** Information on the left, metrics and activity on the right; one column below 1024px. */
export function DetailGrid({ main, aside }: { main: ReactNode; aside: ReactNode }) {
  return (
    <div className="detail-grid">
      <div className="detail-main">{main}</div>
      <div className="detail-aside">{aside}</div>
    </div>
  );
}

/**
 * A MODULE'S OWN PAGES AS ONE ROW OF LINKS (deploy, backup, IPAM, licenses,
 * provisioning, push install, the service desk, the portal). Eight modules
 * drew their own copy of this strip, each a `role="tablist"` of links -- a
 * tablist promises arrow keys and panels that links do not have. This is a
 * plain navigation: `NavLink` marks the current page with `aria-current`.
 */
export function SectionNav({
  label,
  items,
}: {
  label: string;
  items: readonly { to: string; label: string; end?: boolean }[];
}) {
  return (
    <nav aria-label={label} className="tabs scroll section-nav">
      {items.map((item) => (
        <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

/** A record's client (linked to its 360 page) and site, for `DetailHead`'s context. */
export function ClientContext({ clientId, siteName }: { clientId: string | null; siteName?: string | null }) {
  const { clients } = useClientScope();
  const client = clientId === null ? undefined : clients.find((item) => item.id === clientId);
  if (!client && !siteName) return null;
  return (
    <>
      {client ? <Link to={`/clients/${client.id}`}>{client.name}</Link> : null}
      {client && siteName ? " · " : null}
      {siteName ?? null}
    </>
  );
}
```

En `shell/components.tsx`, `Tabs` gana la prop opcional `label?: string`, que pasa a `aria-label` del `div role="tablist"` (una línea: `aria-label={label}`), y al final del archivo:

```tsx
export * from "./listKit";
export * from "./detailKit";
export * from "./formKit";
```

(Las importaciones son circulares en apariencia: `listKit` importa `Btn` de `components`, y `components` reexporta `listKit`. Es seguro porque ninguna de las dos usa la otra al evaluarse, solo dentro de funciones; `npm run build` lo comprueba.)

- [ ] **Step 6: `formKit.tsx`**

```tsx
/**
 * THE FORM PATTERN (design §2, "Formulario"): a full page with back and title,
 * collapsible sections, and ONE sticky bar -- "Unsaved changes · Discard ·
 * Save" -- for the whole console. Atera's walkthrough counts six ways to save;
 * this console has one. A page with changes asks before it is left.
 */
import { useCallback, useContext, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from "react";
import { Link, UNSAFE_DataRouterContext, useBeforeUnload, useBlocker } from "react-router-dom";

import { ConfirmDialog } from "../components/confirm";
import { describeSubmitError, type Problem } from "../components/form";
import { useT } from "../i18n/context";
import { Btn } from "./components";
import { Icon } from "./icons";
import { KIND_ICON } from "./viz";
import "./kit.css";

export function FormPage({
  back,
  title,
  description,
  children,
}: {
  back: { to: string; label: string };
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <div className="form-page">
      <section className="ph">
        <Link className="back-link" to={back.to}>
          <Icon name="chevron-left" size={14} />
          {back.label}
        </Link>
        <div className="ph-row">
          <div>
            <h1>{title}</h1>
            {description ? <p className="sub">{description}</p> : null}
          </div>
        </div>
      </section>
      {children}
    </div>
  );
}

export function FormSection({
  title,
  icon,
  defaultOpen = true,
  children,
}: {
  title: string;
  icon?: string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();
  return (
    <section className="panel form-section">
      <h2 className="form-section-h">
        <button
          type="button"
          className="form-section-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((value) => !value)}
        >
          {icon ? <Icon name={icon} size={16} /> : null}
          <span>{title}</span>
          <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
        </button>
      </h2>
      <div id={bodyId} className="panel-b" hidden={!open}>
        {children}
      </div>
    </section>
  );
}

/**
 * The one save bar. Drawn only while there is something to say: a change, a
 * save in flight, a failure, or "Saved." once the server answered.
 *
 * IT DOES WHAT THE BUTTONS IT REPLACES DID (settings' `SaveBar`, the ticket's
 * "Save changes"), so no page loses a behaviour by moving here: the reason it
 * cannot save (`blockedReason`: Rule 1, disabled with the reason shown), the
 * form's own `problems`, the confirmation after a 2xx (`saved`, never an
 * optimistic one), and Enter in a field (`submit`: the bar sits inside the
 * page's `<Form>` and its Save is that form's submit button).
 */
export function SaveBar({
  dirty,
  saving,
  error,
  onDiscard,
  onSave,
  submit = false,
  saved = false,
  savedMessage,
  problems = [],
  blockedReason,
}: {
  dirty: boolean;
  saving: boolean;
  error?: unknown;
  onDiscard: () => void;
  onSave?: () => void;
  submit?: boolean;
  saved?: boolean;
  savedMessage?: string;
  problems?: readonly Problem[];
  blockedReason?: string;
}) {
  const t = useT();
  if (!dirty && !saving && !error) {
    return saved ? (
      <div className="savebar saved" role="status">
        <Icon name={KIND_ICON.ok} size={15} />
        <span className="savebar-what">{savedMessage ?? t("shell.kit.saved")}</span>
      </div>
    ) : null;
  }
  const failure = error
    ? describeSubmitError(error)
    : problems.length > 0
      ? `${problems.map((problem) => problem.message).join(". ")}.`
      : null;
  return (
    <div className="savebar" role="region" aria-label={t("shell.kit.unsaved")}>
      <span className="savebar-what">{saving ? t("shell.kit.saving") : t("shell.kit.unsaved")}</span>
      {failure ? (
        <span className="savebar-error" role="alert">
          {failure}
        </span>
      ) : null}
      {blockedReason ? <span className="savebar-blocked">{blockedReason}</span> : null}
      <Btn label={t("shell.kit.discard")} onClick={onDiscard} disabled={saving} />
      <Btn
        label={saving ? t("shell.kit.saving") : t("shell.kit.save")}
        variant="primary"
        type={submit ? "submit" : "button"}
        onClick={submit ? undefined : onSave}
        disabled={saving || !dirty || blockedReason !== undefined}
      />
    </div>
  );
}

/**
 * What the form shows, against what the server has. A new answer from the
 * server replaces the draft only while nothing was being edited: a refetch
 * must not wipe what someone is typing. Compared by value, so a refetch that
 * returns the same record is not a change.
 */
export function useDraft<T>(saved: T | undefined): {
  draft: T | undefined;
  setDraft: (next: T) => void;
  dirty: boolean;
  discard: () => void;
} {
  const savedKey = JSON.stringify(saved ?? null);
  const [draft, setDraftState] = useState<T | undefined>(saved);
  const [baseKey, setBaseKey] = useState(savedKey);
  const draftKey = JSON.stringify(draft ?? null);
  if (savedKey !== baseKey) {
    setBaseKey(savedKey);
    if (draftKey === baseKey) setDraftState(saved);
  }
  const setDraft = useCallback((next: T) => setDraftState(next), []);
  const discard = useCallback(() => setDraftState(saved), [saved]);
  return { draft, setDraft, dirty: saved !== undefined && draftKey !== savedKey, discard };
}

/**
 * A PAGE WITH CHANGES ASKS BEFORE IT IS LEFT (design §5). Inside the console's
 * data router (decision D7) a navigation is blocked and the person chooses;
 * a reload or a closed tab gets the browser's own question. Under a router
 * that cannot block -- a test mounted with `MemoryRouter` -- only the browser
 * half applies.
 *
 * LEAVING IS A CHANGE OF PATH (decision D16). A change of query alone -- a
 * tab, a filter, `?new=`, the record a save just selected -- keeps the page,
 * so it does not ask, unless the page says its query IS what it edits
 * (`search`: Settings, where a tab is another form).
 *
 * A SAVE THAT LEAVES, LEAVES. `useBlocker` reads its condition from an
 * effect, so a handler that saves and navigates in one step is judged by the
 * render before the save, with the change still in it (pre-flight I-1,
 * reproduced with react-router 7.18.1). `release()` is read at once and lets
 * the next navigation through; a new change arms the guard again.
 */
export function useUnsavedGuard(
  dirty: boolean,
  options: { search?: boolean } = {},
): { guard: ReactElement | null; release: () => void } {
  const released = useRef(false);
  useEffect(() => {
    if (!dirty) released.current = false;
  }, [dirty]);
  const release = useCallback(() => {
    released.current = true;
  }, []);
  useBeforeUnload(
    useCallback(
      (event: BeforeUnloadEvent) => {
        if (!dirty || released.current) return;
        event.preventDefault();
        event.returnValue = "";
      },
      [dirty],
    ),
  );
  const dataRouter = useContext(UNSAFE_DataRouterContext);
  const guard = dataRouter ? <LeaveQuestion dirty={dirty} search={options.search ?? false} released={released} /> : null;
  return { guard, release };
}

/** The guard for a page that does not leave when it saves. */
export function UnsavedGuard({ dirty, search = false }: { dirty: boolean; search?: boolean }) {
  return useUnsavedGuard(dirty, { search }).guard;
}

function LeaveQuestion({
  dirty,
  search,
  released,
}: {
  dirty: boolean;
  search: boolean;
  released: { readonly current: boolean };
}) {
  const t = useT();
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty &&
      !released.current &&
      (currentLocation.pathname !== nextLocation.pathname || (search && currentLocation.search !== nextLocation.search)),
  );
  return (
    <ConfirmDialog
      open={blocker.state === "blocked"}
      title={t("shell.kit.leaveTitle")}
      subject=""
      body={t("shell.kit.leaveBody")}
      confirmLabel={t("shell.kit.leave")}
      destructive
      onCancel={() => blocker.reset?.()}
      onConfirm={() => blocker.proceed?.()}
    />
  );
}
```

(`ConfirmDialog` nombra su botón de cancelar «Cancel» / «Cancelar»; la prueba lo busca por ese nombre.)

- [ ] **Step 7: `kit.css` y la geometría de la marca en las piezas que ya existen**

`frontend/src/shell/kit.css`:

```css
/* The pattern kit (DESIGN.md v4: "Lists", "Records (detail)", "Forms").
 * Tokens only: the harmony gate reads every value here at runtime. */
.list-chips { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.chip-select { padding: 0 4px 0 11px; gap: 6px; }
.chip-select select { min-height: 30px; border: 0; background: transparent; color: inherit; font: inherit; cursor: pointer; }
.chip-select:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }

.bulkbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 16px; background: var(--accent-s); border-bottom: 1px solid var(--line); }
.bulkbar-count { font-weight: 600; color: var(--accent); }

.back-link { display: inline-flex; align-items: center; gap: 4px; margin-bottom: 6px; font-size: var(--t-sm); color: var(--muted); text-decoration: none; }
.back-link:hover { color: var(--accent); }
.back-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--r-ctl); }

.detail-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 6px; }
.detail-context { font-size: var(--t-sm); color: var(--muted); }
.detail-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); align-items: start; gap: 16px; }
.detail-main, .detail-aside { display: grid; gap: 16px; min-width: 0; }
.tab-panel:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
@media (max-width: 1024px) { .detail-grid { grid-template-columns: minmax(0, 1fr); } }

.form-page { max-width: 1100px; }
.form-section { margin-bottom: 16px; }
.form-section-h { margin: 0; }
.form-section-toggle { display: flex; align-items: center; gap: 8px; width: 100%; padding: 14px 18px; border: 0; background: transparent; color: var(--ink); font: 600 var(--t-h2)/1.3 var(--sans); text-align: left; cursor: pointer; }
.form-section-toggle .chev { margin-left: auto; transition: transform .15s ease; }
.form-section-toggle .chev.open { transform: rotate(180deg); }
.form-section-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.savebar { position: sticky; bottom: 12px; z-index: 20; display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; padding: 10px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-panel); box-shadow: var(--shadow); }
.savebar-what { margin-right: auto; font-weight: 500; color: var(--ink); }
.savebar-error { order: 3; flex-basis: 100%; font-size: var(--t-sm); color: var(--sev-crit); }
.savebar-blocked { order: 3; flex-basis: 100%; font-size: var(--t-sm); color: var(--muted); }
.savebar.saved { gap: 8px; }
.savebar.saved .ic { color: var(--sev-ok); }

@media (prefers-reduced-motion: reduce) { .form-section-toggle .chev { transition: none; } }
```

`frontend/src/shell/components.css` (DESIGN.md v4, «Buttons», «Chips», «Badges»; la maqueta de la dirección A mide 36 / 30 / 32 / 24 px):
- en `.btn` añadir `min-height: 36px;`; en `.btn.small` añadir `min-height: 30px;`;
- añadir, después de `.btn.primary:hover, .btn.pri:hover`, la regla `.btn.primary:active, .btn.pri:active { background: var(--accent-3); border-color: var(--accent-3); }`;
- en `.chip`: `font-size: var(--t-body); font-weight: 500; color: var(--ink); min-height: 32px; padding: 0 11px; gap: 7px;` (sustituye `font-size: var(--t-sm)`, `color: var(--ink-2)` y `padding: 6px 11px`) y añadir `.chip .n { font-size: var(--t-sm); color: var(--muted); font-variant-numeric: tabular-nums; }` y `.chip.on .n { color: var(--accent); }`;
- en `.pill`: `min-height: 24px; padding: 0 9px 0 7px;` (sustituye `padding: 3px 9px 3px 7px`).

Dos comentarios de `components/` (de esta tarea) citan la barra de ajustes que la O7 borra (decisión D22): `components/form.tsx:24` («`modules/settings/SaveBar.tsx` — disabled with a visible reason,») y `components/form.test.tsx:24` («`modules/settings/SaveBar.tsx`») pasan a citar `` `shell/formKit.tsx`'s `SaveBar` ``, y `form.tsx:400` y `form.test.tsx:20` («SaveBar already…») quedan como están, porque la barra del kit cumple la misma regla. Así ninguna oleada tiene que tocar `components/`.

Si `base-styles.test.ts` informa que una regla que transcribía de v3 cambió (`it.each([".btn:hover", ".btn.primary", …])`), se añade al principio del archivo:

```ts
/**
 * RULES DESIGN.md v4 CHANGED ON PURPOSE (2026-11-23, the Novieri brand). The
 * transcription tests below compare against v3 unless a selector is listed
 * here, in which case they compare against these declarations, which are the
 * brand's. Each entry names the reason.
 */
const V4_OVERRIDES: Readonly<Record<string, readonly string[]>> = {};
const expectedFor = (design: string, selector: string): string[] =>
  [...(V4_OVERRIDES[selector] ?? declarations(design, selector))];
```

y cada `expect(ported).toEqual(declarations(design, selector))` del archivo pasa a `expect(ported).toEqual(expectedFor(design, selector))`. La regla nueva va en `V4_OVERRIDES` con sus declaraciones exactas, como `declarations()` las devuelve. Con los cambios de arriba no hace falta ninguna (ninguno toca un selector transcrito); el mapa queda vacío y listo para la Task 4.

- [ ] **Step 8: `forClient`, el proxy de fixtures y el router de datos**

`frontend/src/lib/api.ts`, justo después del constructor de `ApiClient`:

```ts
  /**
   * THIS CLIENT, NARROWED TO ONE CLIENT for every request it makes
   * (`X-Client-Id`), whatever the top bar says: a list's "Client" chip and the
   * client 360 page (redesign design §1.3, §2; decision D6). It narrows and
   * never widens -- the server answers with the session's scope intersected
   * with this id, and RLS enforces it (invariant 5a). Global routes still go
   * without the header (`requestHeaders`).
   */
  forClient(clientId: string): ApiClient {
    return new ApiClient(this.onUnauthorized, this.base, () => clientId);
  }
```

`frontend/src/fixtures/client.ts`, en el `get` del `Proxy` (líneas 121-125):

```ts
    get(_target, prop) {
      if (typeof prop !== "string" || prop === "then") return undefined;
      // A narrowed client answers from the same fixtures: they are not
      // per-client, and the harmony gate measures the page, not the scope.
      if (prop === "forClient") return () => proxy;
      return methodFor(prop);
    },
```

con `const proxy: ApiClient = new Proxy({} as ApiClient, { … });` y `return proxy;` en lugar de `return new Proxy(…)` (el tipo explícito es lo que deja al `get` devolverse a sí mismo; comprobado con `npm run typecheck` en una copia de trabajo).

`frontend/src/App.tsx`:
- importar `createBrowserRouter`, `createRoutesFromElements`, `Outlet`, `RouterProvider` y `type RouteObject` de `react-router-dom`, y quitar `BrowserRouter` y `Routes` de la importación;
- antes de `export default function App()`, crear la función de rutas con esta forma:
  ```tsx
  /**
   * THE CONSOLE'S ROUTES, AS A DATA ROUTER (redesign decision D7): the same
   * `<Route>` elements as before, under one layout route that draws the setup
   * gate. A data router is what lets a page with unsaved changes block a
   * navigation (`shell/formKit.tsx`, `useBlocker`). `routes-covered.test.ts`
   * still reads every `path=` below.
   */
  export function appRoutes(): RouteObject[] {
    return createRoutesFromElements(
      <Route
        errorElement={<RouteError />}
        element={
          <SetupGate>
            <Outlet />
          </SetupGate>
        }
      >
      </Route>,
    );
  }
  ```
  con `import { RouteError } from "./shell/RouteError";`, y **mover**, sin editarlas, las líneas que hoy están entre `<Routes>` y `</Routes>` en `App` (de `{/* DEV ONLY. Vite substitutes…` en la 735 a la `/>` de la ruta `path="*"` en la 1623: el bloque `import.meta.env.DEV`, las 115 `<Route>` y sus comentarios) a dentro de esa `<Route element={…}>`, entre su `>` y su `</Route>`. Comprobación: `grep -c 'path="' frontend/src/App.tsx` da **el mismo número** antes y después (la ruta de diseño no tiene `path`). No se compara `grep -c "<Route"`: el nuevo `<RouterProvider` y la palabra `<Route>` del comentario de arriba también cuentan (medido: 116 antes, 118 después, sin que falte ninguna ruta).
- `App` pasa a:
  ```tsx
  export default function App() {
    // Built once per mount, from the URL the page was opened at. Per mount,
    // not a module singleton, on purpose: `App.portalMeCensus.test.tsx` mounts
    // `<App/>` at a new URL in each case. Under `StrictMode` (`main.tsx`) the
    // development build mounts twice and builds two routers; the discarded
    // one keeps its `popstate` listener until the page unloads. Accepted: it
    // is development only, and production mounts once.
    const [router] = useState(() => createBrowserRouter(appRoutes()));
    return (
      <QueryClientProvider client={queryClient}>
        <ApiProvider>
          <LanguageBridge>
            <RouterProvider router={router} />
          </LanguageBridge>
        </ApiProvider>
      </QueryClientProvider>
    );
  }
  ```

`frontend/src/shell/RouteError.tsx` (decisión D18):

```tsx
/**
 * WHAT A PAGE THAT FAILED WHILE DRAWING SHOWS (redesign decision D18). The
 * data router catches a render error at the layout route and draws this in
 * place of the console, instead of React Router's own page ("Unexpected
 * Application Error!", English, inline styles). One sentence that says what
 * failed and that nothing was changed (copy voice), drawn the way `QueryState`
 * draws a failed read, and the way out.
 */
import { Link, useRouteError } from "react-router-dom";

import { useT } from "../i18n/context";
import { Icon } from "./icons";
import { KIND_ICON } from "./viz";

export function RouteError() {
  const t = useT();
  // Read so the router treats the error as handled here. Never shown: a stack
  // is nothing a person can act on, and it can carry a record's data.
  useRouteError();
  return (
    <main className="content route-error" id="main">
      <div role="alert" className="notice red">
        <Icon name={KIND_ICON.crit} size={17} />
        <div>
          <b>{t("shell.routeError.title")}</b>
          <p>{t("shell.routeError.body")}</p>
          <Link className="btn small" to="/dashboard">
            {t("shell.routeError.home")}
          </Link>
        </div>
      </div>
    </main>
  );
}
```

y en `frontend/src/App.router.test.tsx`, con `import { render, screen } from "@testing-library/react";`, `import { createMemoryRouter, RouterProvider } from "react-router-dom";`, `vi` en la importación de `vitest` e `import { RouteError } from "./shell/RouteError";`, dos casos más dentro del `describe`:

```tsx
  it("puts a translated error page on the layout route (decision D18)", () => {
    expect(appRoutes()[0]?.errorElement).toBeDefined();
  });

  it("says what failed in the console's words when a page throws, never React Router's page", () => {
    function Broken(): never {
      throw new Error("boom");
    }
    const router = createMemoryRouter([{ path: "/", element: <Broken />, errorElement: <RouteError /> }]);
    // React reports the caught error through console.error; expected here.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(<RouterProvider router={router} />);
      expect(screen.getByRole("alert")).toHaveTextContent("This page could not be shown");
      expect(screen.getByRole("link", { name: "Go to the dashboard" })).toHaveAttribute("href", "/dashboard");
      expect(screen.queryByText(/Unexpected Application Error/)).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });
```

- [ ] **Step 9: Ejecutar las pruebas del kit**

Run: `cd frontend && npx vitest run src/shell/listKit.test.tsx src/shell/detailKit.test.tsx src/shell/formKit.test.tsx src/lib/api.forClient.test.ts src/App.router.test.tsx src/test/routes-covered.test.ts src/App.routing.test.tsx src/App.shell.test.tsx`
Expected: PASS.

- [ ] **Step 10: El kit, el marco de acceso y el del portal en `/__preview`**

`frontend/src/preview/PreviewPage.tsx` gana tres secciones al final, después de la de `SessionEnded` y antes del `ConfirmDialog`. Son de esta tarea y de ninguna oleada (revisión previa, W-I4): la O8 rehace `AuthCard` y la O2 rehace `PortalShell`, y las dos secciones los dibujan tal como estén, así que cada oleada los mide aquí sin tocar este archivo.

1. **«Kit v4»**: `<Panel title="Kit v4" …>`, que dibuja, con datos fijos y sin API: `ListToolbar` con `SearchField`, `FilterChips` de tres opciones (dos con conteo) y `ShowingCount shown={9} total={12}`; `BulkBar count={2}` con un `Btn`; `ListEmpty` en sus dos formas; `DetailHead` con `status={{ kind: "ok", label: "Online" }}` y un contexto; `UrlTabs` de tres pestañas sobre un `useUrlTab(["a", "b", "c"], "a", "kit")` (parámetro propio, para no chocar con el `tab` de la página); `DetailGrid` con dos `Panel`; `FormSection` con un `Field`; `SaveBar dirty saving={false}` y, debajo, `SaveBar dirty={false} saving={false} saved`. `ClientFilter` no se dibuja aquí: necesita `/me`, y lo mide la puerta de armonía en las listas.
2. **«AuthCard embedded»** y **«Portal frame»**, con `import { AuthCard } from "../modules/auth/AuthCard";`, `import { PortalShell } from "../modules/portal/PortalShell";`, `import { PORTAL_ME_KEY } from "../modules/portal/usePortalMe";` y `PortalMe` en la importación de tipos de `../lib/api`. Antes de `export function PreviewPage()`:
   ```tsx
   /**
    * The portal's `/portal/me`, answered, and nothing that can reach the
    * network: `PortalShell` reads it, and this pass runs with no backend.
    */
   function portalPreviewClient(): QueryClient {
     const client = new QueryClient({
       defaultOptions: {
         queries: {
           staleTime: Infinity,
           gcTime: Infinity,
           retry: false,
           refetchOnMount: false,
           refetchOnWindowFocus: false,
           refetchOnReconnect: false,
         },
       },
     });
     const contact: PortalMe = {
       user_id: "contact-preview",
       kind: "contact",
       email: "ana.gomez@andes.example",
       display_name: "Ana Gómez",
       timezone: "America/Bogota",
       permissions: ["portal.ticket.read", "portal.ticket.write"],
     };
     client.setQueryData([PORTAL_ME_KEY], contact);
     return client;
   }
   ```
   en el cuerpo, junto a `patchClient`: `const portalClient = useMemo(() => portalPreviewClient(), []);`, y las dos secciones:
   ```tsx
         {/* THE SIGN-IN FRAME (redesign plan, pre-flight W-I4): the card every
             screen without a session draws. Here so the browser pass measures
             it, and measures it again when wave 8 repaints `AuthCard`.
             `embedded`: a region named by its headline, not a second <main>,
             and it leaves the tab's title alone. */}
         <div style={{ marginTop: 24 }}>
           <AuthCard title="Sign in" intro="Use the email address and password your administrator gave you." embedded>
             <p>The sign-in form goes here.</p>
           </AuthCard>
         </div>

         {/* THE PORTAL'S FRAME (pre-flight W-I4), around one fixed line: what
             a client's contact sees around every portal page, measured here
             because the harmony walk's fixture person is not a contact. Its
             `/portal/me` is seeded (`portalPreviewClient`). It forgets the
             client stored by the top bar on render (decision 7 of its own
             design), which this pass does not set. */}
         <div style={{ marginTop: 24 }}>
           <QueryClientProvider client={portalClient}>
             <PortalShell>
               <p>A portal page draws here.</p>
             </PortalShell>
           </QueryClientProvider>
         </div>
   ```

Run: `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs`
Expected: código 0; las piezas nuevas, el marco de acceso y el del portal aparecen entre los «text runs measured» sin pares por debajo del mínimo.

- [ ] **Step 11: Suite completa y navegador**

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: verde.

Run: `node scripts/harmony.mjs --strict-fixtures --lang en` y `--lang es` (todas las rutas: el router cambió)
Expected: `ok …` en las dos.

`PreviewPage.tsx` es una de las 86 y la Task 14 busca su crítica: «Cierre de cada oleada», pasos 1, 3 y 4, sobre `frontend/src/preview/PreviewPage.tsx` (sin el paso 2: harmony no recorre `/__preview`, que es de `a11y-browser.mjs`; el subagente B de la crítica la abre con `npx vite` en `/__preview`). Su crítica mira la página como lo que es, el banco de pruebas del kit, y sus preguntas las contesta el coordinador como dice el paso 3; es la primera crítica del programa, así que esas respuestas inauguran `.impeccable/critica-respuestas.md` (del coordinador, versionado en la rama base).

- [ ] **Step 12: Commit**

```bash
git add frontend/src .impeccable/critique .impeccable/critica-respuestas.md
git commit -m "feat(ui): pattern kit for lists, records and forms; data router; client-narrowed reads

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación:** las cinco pruebas nuevas en verde, con los casos de D16 (un guardado que se va no pregunta; la query sola no es salir), D17 (el chip espera a `/me`, no manda un cliente ajeno y vuelve a leer al cambiar) y D18 (la pantalla de error traducida); `catalogue.identical.test.ts` fija a cada oleada en su `identical-ok.o<N>.ts`; las 117 rutas de `routes.mjs` se recorren igual en harmony; `/__preview` muestra y mide cada pieza, el marco de acceso y el del portal; ninguna página de módulo cambió; el selector, las tablas, los avisos, los diálogos y los cajones solo cambiaron por los tokens.

---

### Task 4: El armazón: la barra lateral de Atera, Administración en acordeón, «Nuevo» y la barra superior

Diseño §1.1 y §1.2, y la respuesta del operador del 2026-11-24 sobre el Asistente. Es la tarea que cambia la navegación a propósito: `nav.test.ts`, `Sidebar.test.tsx`, `AppShell.test.tsx`, `AreaPage.test.tsx`, `shell.es.test.tsx` y `responsive.test.ts` se corrigen aquí, cada cambio con un comentario que cita el diseño o la respuesta, y ninguna prueba se borra: un caso cuyo asunto el diseño no cambia se conserva tal cual (Step 1 los nombra).

**Files:**
- Modify: `frontend/src/shell/nav.ts:68-70` (la `group()` vieja se borra), `:131-213` (tipos y `NAV_GROUPS`), `:215-222` (`navItems`), `:296-321` (`destinations`), `:346-405` (`visibleGroups`, `searchDestinations`), `:411-416` (`PORTAL_GROUPS`)
- Modify: `frontend/src/shell/Sidebar.tsx` (archivo completo)
- Create: `frontend/src/shell/AdminPanel.tsx`, `frontend/src/shell/newMenu.ts`, `frontend/src/shell/NewMenu.tsx`
- Modify: `frontend/src/shell/AppShell.tsx` (prop `newMenu`; fuera «Añadir un equipo» y el icono del Asistente de la barra; el botón flotante del Asistente, decisión D12)
- Modify: `frontend/src/shell/AreaPage.tsx` (la portada de Administración)
- Modify: `frontend/src/shell/shell.css` (riel, panel de Administración, «Nuevo», la búsqueda más ancha, el botón flotante del Asistente y el sitio que la barra de guardado le deja)
- Modify: `frontend/src/App.tsx` (`Shell` calcula `newMenu`)
- Modify: `frontend/src/i18n/{en,es}/shell.ts`, `frontend/src/i18n/identical-ok.ts` (`IDENTICAL_OK_SHARED`)
- Modify (textos que nombran entradas del riel que cambian, revisión previa S-M-1): `frontend/src/i18n/es/{serviceDesk,remote,deploy,backup,agentReleases}.ts` y sus pruebas `modules/service-desk/service-desk.es.test.tsx:1019`, `modules/deploy/deploy.es.test.tsx:807`, `modules/backup/backup.es.test.tsx:1257`
- Modify (destinos de «Nuevo», decisión D8): `frontend/src/modules/service-desk/TicketListPage.tsx:96,258,460`, `frontend/src/modules/docs/DocsPage.tsx:379-387`, `frontend/src/modules/change/ChangesPage.tsx:64,145,275`, `frontend/src/modules/clients/ClientsPage.tsx:30,110`, `frontend/src/modules/clients/ContactsTab.tsx:63`
- Test: `frontend/src/shell/nav.test.ts`, `Sidebar.test.tsx`, `AppShell.test.tsx`, `AreaPage.test.tsx`, `shell.es.test.tsx`, `icons.test.tsx`, `frontend/src/test/responsive.test.ts`; nuevos `frontend/src/shell/newMenu.test.ts`, `frontend/src/shell/NewMenu.test.tsx`, `frontend/src/shell/AdminPanel.test.tsx`, `frontend/src/shell/newDestinations.test.tsx`

**Interfaces:**
- Consumes: `NovieriSymbol`, `NovieriWordmark` (Task 2); `SearchField` (kit); `useNewParam` (Task 3).
- Produces (`nav.ts`): `interface NavFolder { kind: "folder"; key: "assets" | "more"; label: string; icon: string; items: readonly NavItem[] }`; `type RailEntry = NavItem | NavArea | NavFolder`; `isFolder(entry): entry is NavFolder`; `navFolders(groups?: readonly NavGroup[]): NavFolder[]`; `interface NavGroup { key: "main" | "anchored" | "help"; label: string; items: readonly RailEntry[] }`. `visibleGroups`, `navItems`, `crumbFor`, `activeNavTo` y `searchDestinations` conservan su firma.
- Produces (`newMenu.ts`): `type NewMenuKind = "ticket" | "client" | "contact" | "device" | "document" | "change"`, `interface NewMenuEntry { kind; to: string; icon: string }`, `newMenuEntries(input: NewMenuInput): NewMenuEntry[]` con `NewMenuInput = { permissions; everywhere; clients: readonly MeClient[]; selected: string | null; modules: ReadonlySet<string> }`.
- Produces (`AppShell`): prop `newMenu?: readonly NewMenuEntry[]`; el botón flotante del Asistente (`.assistant-fab`), que se dibuja solo con `ai.use` y fuera de `/assistant`, y pone en `<body>` la clase `assistant-fab-on` mientras está, que es lo que aparta la `SaveBar` (Task 3) de su esquina.

**Decisiones de esta tarea** (Apéndice A):
- **D9.** «Activos» y «Más» son carpetas del riel: un botón con `aria-expanded` que despliega sus páginas en el sitio. La carpeta que contiene la página actual se abre sola. Plegado el riel, sus páginas se ven como iconos debajo del icono de la carpeta, así que nada queda a un clic de más. «Más» no se dibuja si ningún módulo opcional está activo para la elección de arriba (`visibleGroups` ya quita un grupo vacío).
- **D10.** «Administración» es un botón anclado abajo que cambia el riel por su panel (volver, búsqueda y acordeón) **sin navegar**. El panel se abre solo cuando la página actual es de Administración; «Volver al menú» lo cierra sin salir de la página. El título del panel enlaza a `/admin`, que sigue siendo la portada (la miga apunta allí).
- **D11.** «Añadir un equipo» sale de la barra superior y pasa a «Nuevo → Equipo» y a la cabecera de Equipos (diseño §1.2). «Nuevo» es ahora el único primario de la barra (DESIGN.md v4, «Buttons»).
- **D12. El Asistente, como en las capturas de Atera** (respuesta del operador del 2026-11-24, «Como queda en las capturas de atera»; comprobado en `docs/design/referencias-atera/atera 1.png` y en el recorrido, líneas 22, 82 y 87). **Sale de la barra superior** (la de Atera lleva búsqueda, «Install agent», campana, triángulo, ayuda y perfil, sin icono de IA), **queda anclado al pie del riel** con Administración, y **es además un botón redondo flotante abajo a la derecha en todas las páginas** de la consola: un enlace a `/assistant`, como el icono de hoy, con el permiso `ai.use`, que no se dibuja en la propia página del Asistente (`/assistant`; sí en sus subpáginas de ajustes, autonomía y acciones), de 48 px (más que los 44 de un objetivo táctil) y que **nunca tapa la barra de guardado**: el recorrido de Atera apunta ese defecto en su propio producto (línea 639, «el botón flotante de IA tapa el botón de guardar»). Mientras el botón está, `<body>` lleva `assistant-fab-on` y la `SaveBar` deja 72 px libres a su derecha, así que los dos no comparten la esquina a ningún ancho; el contenido deja 88 px al pie para que su última línea suba por encima del botón. Abre la página del Asistente: no es un panel de chat superpuesto, que sería una función nueva. La fila «Preguntar al Asistente» de la búsqueda sigue en todas las anchuras. Esto sustituye la decisión del 2026-09-08 («AI Analyst needs to be there», que puso el icono en la barra): el operador decide ahora dónde está, y el comentario de `AppShell.tsx` que la citaba pasa a citar esta respuesta.
- **D13.** En español, el riel dice «Inicio», «Equipos» y «Documentación», como pide el diseño §1.1 (hoy «Panel», «Dispositivos» y «Documentación de TI»). El inglés no cambia. «Equipos» para la lista de máquinas sigue el glosario («computer → equipo, a machine on the list»); la entrada `device → dispositivo` del glosario de `src/i18n/es/index.ts` gana una nota: «la lista de máquinas (`/devices`) se llama Equipos en el riel y en su página (rediseño 2026-11-23)». La O1 cambia el título de la página a juego.

- [ ] **Step 1: Las pruebas del modelo (fallan)**

En `frontend/src/shell/nav.test.ts`:

1. En el `describe("two layers", …)` (líneas 40 a 140) **se sustituyen tres casos y se conservan tres** (revisión previa, F-I3; Global Constraints: ninguna prueba se borra). Se sustituyen los que fijaban lo que el diseño cambia a propósito: «puts the daily work first, the business modules shown, and Admin last» (líneas 41-71), «folds only configuration away» (73-82) y «shows Governance and Assets & Spend pages as ordinary items» (108-115). Se conservan **sin tocar una letra** «puts every configuration and platform page in Admin» (84-106: las secciones de Administración son las de hoy salvo Clientes, y ninguna de sus etiquetas cambia), «names tasks, not machinery» (117-131) y «gives every area page a line saying what it is for» (133-139). Es decir: el `describe("two layers", () => {` de la línea 40 pasa a ser el `describe("the Atera order …", () => {` de abajo, con sus constantes y sus cinco casos nuevos al principio, y detrás de ellos siguen, tal como están, los tres conservados y el `});` que cierra. `EVERYTHING` y `shape` van antes del `describe`:

```ts
/** Every code the model names: an operator holding all of them sees every page. */
const EVERYTHING = navItems()
  .map((item) => item.permission)
  .filter((code): code is string => code !== null);

/** A rail entry as a short string: an item's route, or a folder's key and its routes. */
function shape(entry: RailEntry): string {
  if (isFolder(entry)) return `${entry.key}:[${entry.items.map((item) => item.to).join(",")}]`;
  return entry.to;
}

describe("the Atera order (redesign design §1.1, 2026-11-23)", () => {
  // REPLACES "two layers" (2026-09-23): daily work, governance and assets
  // under captions, and Admin folded. The redesign takes Atera's rail: no
  // captions, Clients third, two folders, and the Assistant and Admin
  // anchored at the bottom.
  const groups = visibleGroups(EVERYTHING, OPTIONAL_MODULES);
  const main = groups.find((group) => group.key === "main")?.items ?? [];
  const anchored = groups.find((group) => group.key === "anchored")?.items ?? [];

  it("lists the main rail in Atera's order, with Assets and More as folders", () => {
    expect(main.map(shape)).toEqual([
      "/dashboard",
      "/service-desk",
      "/clients",
      "/devices",
      "/health",
      "/patch",
      "/deploy",
      "/backup",
      "/remote",
      "assets:[/licenses,/subscriptions,/ipam]",
      "/docs",
      "/reports",
      "more:[/changes,/bcp,/budget]",
    ]);
  });

  it("anchors the Assistant and Admin at the bottom", () => {
    expect(anchored.map(shape)).toEqual(["/assistant", "/admin"]);
  });

  it("keeps Admin the only area, with today's sections in order and Clients out of it", () => {
    expect(navAreas().map((area) => area.to)).toEqual(["/admin"]);
    expect(navAreas()[0]?.sections.map((section) => section.items.map((item) => item.to))).toEqual([
      ["/notifications", "/email-intake", "/sla", "/portal"],
      ["/enrollment", "/agent-packages", "/agent-push/authorizations", "/device-groups", "/site-servers"],
      ["/assistant/autonomy", "/jobs"],
      ["/credentials", "/audit", "/security"],
      ["/platform-status", "/error-monitor", "/settings"],
    ]);
  });

  it("puts in More exactly the three optional modules, and drops More when none is on", () => {
    const more = main.find((entry) => isFolder(entry) && entry.key === "more");
    expect(more && isFolder(more) ? more.items.map((item) => item.module) : []).toEqual(["change", "bcp", "budget"]);
    const none = visibleGroups(EVERYTHING, new Set());
    expect(none.flatMap((group) => group.items).some((entry) => isFolder(entry) && entry.key === "more")).toBe(false);
  });

  it("keeps every code the rail had before the redesign (the permissions do not change, §1.1)", () => {
    expect(new Set(EVERYTHING)).toEqual(
      new Set([
        "devices.read", "health.read", "ticket.read", "patch.read", "deploy.read", "backup.read",
        "remote.control", "ai.use", "docs.read", "change.read", "bcp.read", "reporting.read",
        "licenses.read", "subscriptions.read", "budget.read", "ipam.read", "clients.read",
        "notification.read", "email_intake.read", "sla.read", "portal.ticket.read",
        "agent.release.read", "agent.push.scope.manage", "site.read", "ai.admin", "jobs.read",
        "credentials.read", "audit.read", "platform.status.read", "platform.errors.read", "orgs.manage",
      ]),
    );
  });
});
```

2. En `describe("the Clients page (MSP clients phase 3, design §4.1)")`, el caso «sits in Admin, gated on clients.read» pasa a:

```ts
  it("sits third in the main rail, gated on clients.read (redesign design §1.1: it leaves Admin)", () => {
    const main = NAV_GROUPS.find((group) => group.key === "main")?.items ?? [];
    expect(main.map((entry) => ("to" in entry ? entry.to : null))[2]).toBe("/clients");
    expect(navItems().find((item) => item.to === "/clients")?.permission).toBe("clients.read");
  });
```

3. En `describe("crumbFor")`: «gives every page a section that links to a rail entry» sigue igual; añadir:

```ts
  it("names a folder's page after itself, linked to itself: a folder is not a page", () => {
    expect(crumbFor("/licenses")).toEqual({ section: "Licenses", sectionTo: "/licenses", title: "Licenses" });
    expect(crumbFor("/licenses/lic-1")).toEqual({ section: "Licenses", sectionTo: "/licenses", title: "Licenses" });
  });

  it("names Clients as a main page now, not as an Admin page", () => {
    expect(crumbFor("/clients/c-1").sectionTo).toBe("/clients");
  });
```

y `isFolder, type RailEntry` en la importación de `./nav`.

4. Tres ajustes de tipo, porque un `RailEntry` puede ser ahora una carpeta, que no tiene `to` (`npm run typecheck` los señala; comprobado en una copia de trabajo el 2026-11-23):
   - línea 30: `const RAIL = NAV_GROUPS.flatMap((group) => group.items);` pasa a `const RAIL = NAV_GROUPS.flatMap((group) => group.items).flatMap((entry) => (isFolder(entry) ? [...entry.items] : [entry]));` (las páginas de una carpeta son entradas del riel a efectos de `NAV_UNLISTED` y de la miga);
   - las dos búsquedas `.find((entry) => entry.to === "/admin")` (líneas 200 y 509) pasan a `.find((entry) => "to" in entry && entry.to === "/admin")`.

5. «is offered to a person holding only clients.read» (línea 506) fijaba que Clientes estaba en la sección Organización de Administración. Pasa a:

```ts
  it("is offered to a person holding only clients.read, in the main rail (redesign design §1.1)", () => {
    const main = visibleGroups(["clients.read"], new Set()).find((group) => group.key === "main");
    expect(main?.items.map((entry) => ("to" in entry ? entry.to : entry.key))).toEqual(["/dashboard", "/clients"]);
  });
```

6. Tres guardas que hoy no ven las carpetas (revisión previa, F-M7). Siguen pasando sin cambiar, pero dejarían de afirmar lo que dicen en cuanto una carpeta tuviera un icono que no existe o quedara vacía:
   - en la importación de `./nav`, añadir `navFolders`;
   - «uses only icons the design actually defines»: `[...ALL_ITEMS, ...navAreas()]` pasa a `[...ALL_ITEMS, ...navAreas(), ...navFolders()]`;
   - «never returns a group or area with nothing in it»: dentro del `for (const entry of group.items)`, añadir `if (isFolder(entry)) expect(entry.items.length).toBeGreaterThan(0);`;
   - «drops an area none of whose pages is visible»: sus dos `expect` buscaban «Governance» y «Assets & Spend», etiquetas que ya no existen y que por tanto nunca fallarían. Pasan a:
     ```ts
         // An operator with devices.read and patch.read reaches no Assets page
         // and no optional module: neither folder is drawn (redesign §1.1).
         const keys = visibleGroups(held, OPTIONAL_MODULES)
           .flatMap((group) => group.items)
           .filter(isFolder)
           .map((folder) => folder.key);
         expect(keys).toEqual([]);
         // NON-VACUITY: with a license code the Assets folder is there.
         expect(
           visibleGroups([...held, "licenses.read"], OPTIONAL_MODULES)
             .flatMap((group) => group.items)
             .filter(isFolder)
             .map((folder) => folder.key),
         ).toEqual(["assets"]);
     ```
   y en `frontend/src/shell/icons.test.tsx:22`, `NAV_ICON_NAMES` pasa a `[...new Set([...navItems(), ...navAreas(), ...navFolders()].map((item) => item.icon))]`, con `navFolders` en su importación de `./nav`.

   `navFolders` es nueva en `nav.ts` (Step 4): las carpetas de unos grupos, `NAV_GROUPS` por defecto.

Las demás pruebas de `nav.test.ts` (códigos, duplicados, `visibleGroups`, búsqueda, `NAV_UNLISTED`, portal, módulos opcionales, Imaging) pasan sin cambios: en la copia de trabajo, con lo de arriba y el título de la Task 2, el archivo queda en verde.

`frontend/src/shell/newMenu.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import type { MeClient } from "../lib/api";
import { newMenuEntries, type NewMenuInput } from "./newMenu";

const A = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57";
const B = "0b6a2c1e-6d2f-4a51-9c3e-2f4d1a7b8c01";
const ALL = ["ticket.write", "clients.manage", "devices.read", "docs.manage", "change.request"];

function client(id: string, permissions: string[], kind: MeClient["kind"] = "customer"): MeClient {
  return { id, name: id, kind, enabled_modules: [], permissions };
}

function input(over: Partial<NewMenuInput> = {}): NewMenuInput {
  return {
    permissions: ALL,
    everywhere: ALL,
    clients: [client(A, ALL), client(B, ALL)],
    selected: null,
    modules: new Set(["change"]),
    ...over,
  };
}

describe("the New menu (redesign design §1.2: only real destinations, each with its permission)", () => {
  it("offers every destination, in the design's order, to someone who may create all of it", () => {
    expect(newMenuEntries(input()).map((entry) => [entry.kind, entry.to])).toEqual([
      ["ticket", "/service-desk?new=ticket"],
      ["client", "/clients?new=client"],
      ["contact", "/clients?new=contact"],
      ["device", "/devices/onboard"],
      ["document", "/docs?new=article"],
      ["change", "/changes?new=change"],
    ]);
  });

  it("offers nothing to someone who may create nothing, so there is no menu at all", () => {
    expect(newMenuEntries(input({ permissions: [], everywhere: [], clients: [client(A, [])] }))).toEqual([]);
  });

  it("offers a client only to someone who may manage clients everywhere, as the Clients page does", () => {
    const kinds = newMenuEntries(input({ everywhere: ["ticket.write"] })).map((entry) => entry.kind);
    expect(kinds).not.toContain("client");
  });

  it("takes a contact straight to the chosen client's contacts", () => {
    const contact = newMenuEntries(input({ selected: A })).find((entry) => entry.kind === "contact");
    expect(contact?.to).toBe(`/clients/${A}?tab=contacts&new=contact`);
  });

  it("offers no contact for a chosen client the person cannot manage, nor for Unassigned", () => {
    expect(newMenuEntries(input({ selected: A, clients: [client(A, ["clients.read"])] })).some((entry) => entry.kind === "contact")).toBe(false);
    expect(newMenuEntries(input({ clients: [client(A, ALL, "unassigned")] })).some((entry) => entry.kind === "contact")).toBe(false);
  });

  it("offers a change only where the change module is on", () => {
    expect(newMenuEntries(input({ modules: new Set() })).some((entry) => entry.kind === "change")).toBe(false);
  });
});
```

- [ ] **Step 2: Las pruebas de los componentes (fallan)**

`frontend/src/shell/NewMenu.test.tsx`:

```tsx
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderPage } from "../test/renderPage";
import { NewMenu } from "./NewMenu";
import type { NewMenuEntry } from "./newMenu";

const ENTRIES: NewMenuEntry[] = [
  { kind: "ticket", to: "/service-desk?new=ticket", icon: "ticket" },
  { kind: "device", to: "/devices/onboard", icon: "devices" },
];

describe("NewMenu", () => {
  it("is a menu button in the primary voice, and draws nothing when there is nothing to create", () => {
    renderPage("/x", <NewMenu entries={ENTRIES} />);
    const button = screen.getByRole("button", { name: "New" });
    expect(button).toHaveAttribute("aria-haspopup", "menu");
    expect(button).toHaveClass("primary");
    const empty = renderPage("/y", <NewMenu entries={[]} />);
    expect(empty.container.querySelector("button")).toBeNull();
  });

  it("opens on the first entry, and each entry goes to its destination", async () => {
    const { router } = renderPage("/x", <NewMenu entries={ENTRIES} />);
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    expect(screen.getByRole("menuitem", { name: "Ticket" })).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Computer" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(router.state.location.pathname).toBe("/devices/onboard");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape and gives focus back to the button", async () => {
    renderPage("/x", <NewMenu entries={ENTRIES} />);
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: "New" })).toHaveFocus();
  });
});
```

`frontend/src/shell/AdminPanel.test.tsx`:

```tsx
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import { renderPage } from "../test/renderPage";
import { Sidebar } from "./Sidebar";
import { navItems } from "./nav";

const EVERYTHING = navItems()
  .map((item) => item.permission)
  .filter((code): code is string => code !== null);

function rail(path: string) {
  return renderPage(path, <Sidebar permissions={EVERYTHING} collapsed={false} onToggleCollapsed={() => {}} orgName="Novieri" />);
}

describe("Admin, as Atera draws it (redesign design §1.1)", () => {
  it("is a button at the foot of the rail that swaps the rail for its panel, without navigating", async () => {
    const { router } = rail("/devices");
    await userEvent.click(screen.getByRole("button", { name: "Admin" }));
    expect(router.state.location.pathname).toBe("/devices");
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: "Back to the menu" })).toBeInTheDocument();
    expect(within(panel).getByRole("searchbox", { name: "Search Admin" })).toBeInTheDocument();
  });

  it("opens by itself on an Admin page, with that page's section open and the page current", () => {
    rail("/audit");
    const panel = screen.getByRole("navigation", { name: "Admin" });
    expect(within(panel).getByRole("button", { name: /Security/ })).toHaveAttribute("aria-expanded", "true");
    expect(within(panel).getByRole("button", { name: /Service desk/ })).toHaveAttribute("aria-expanded", "false");
    expect(within(panel).getByRole("link", { name: "Audit log" })).toHaveAttribute("aria-current", "page");
  });

  it("goes back to the main rail and stays on the page", async () => {
    const { router } = rail("/audit");
    await userEvent.click(screen.getByRole("button", { name: "Back to the menu" }));
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/audit");
  });

  it("finds a page by any part of its name, accents ignored", async () => {
    setActiveLanguage("es");
    try {
      rail("/settings");
      await userEvent.type(screen.getByRole("searchbox", { name: "Buscar en Administración" }), "auditoria");
      const panel = screen.getByRole("navigation", { name: "Administración" });
      expect(within(panel).getByRole("link", { name: "Registro de auditoría" })).toBeVisible();
      expect(within(panel).queryByRole("link", { name: "Configuración" })).toBeNull();
    } finally {
      setActiveLanguage("en");
    }
  });
});
```

En `frontend/src/shell/Sidebar.test.tsx` (revisión previa, F-I1: tres de los seis casos de «Sidebar current item» fallan si solo se busca en la navegación «Main», porque en una página de Administración el panel la sustituye y el Asistente pasa a la navegación anclada):

1. El ayudante `renderAt` busca en todo el riel, no solo en «Main». Sustituir sus líneas 19 a 31 por:
   ```tsx
   function renderAt(path: string, permissions: readonly string[] = EVERY_PERMISSION) {
     const { container } = render(
       <MemoryRouter initialEntries={[path]}>
         <Sidebar
           permissions={permissions}
           collapsed={false}
           onToggleCollapsed={() => {}}
           orgName="Acme"
         />
       </MemoryRouter>,
     );
     // THE WHOLE RAIL (redesign design §1.1): the main menu, the Admin panel
     // that replaces it on an Admin page, and the anchored Assistant and Admin.
     // The current item can be in any of the three now.
     return within(container.querySelector<HTMLElement>("#sidebar") as HTMLElement);
   }
   ```
   `currentAt` no cambia (sigue leyendo el `title` de cada enlace con `aria-current="page"`), y los seis casos de `describe("Sidebar current item")` se quedan **tal cual**: con este ayudante pasan los seis («AI automation» está en el panel de Administración, «Assistant» en la navegación anclada y «Admin» es el enlace del título del panel, que lleva `title={area.label}`: Step 6, `AdminPanel.tsx`).
2. `describe("the second layer")` se sustituye por el de abajo. De sus cinco casos, tres fijaban lo que el diseño cambia a propósito (Administración como enlace del riel que abre sus páginas debajo, las páginas de Gobierno sin carpeta) y se rehacen; dos siguen valiendo y se conservan adaptados al panel: «gives an open area's pages their own icons» y «opens only the pages this operator may reach».
   ```tsx
   describe("the rail's folders and anchored entries (redesign design §1.1)", () => {
     it("opens the folder that holds the current page, and only that one", () => {
       const rail = renderAt("/subscriptions");
       expect(rail.getByRole("button", { name: "Assets" })).toHaveAttribute("aria-expanded", "true");
       expect(rail.getByRole("link", { name: "Subscriptions" })).toHaveAttribute("aria-current", "page");
       expect(rail.getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "false");
     });

     it("opens and closes a folder in place", async () => {
       const rail = renderAt("/devices");
       const more = rail.getByRole("button", { name: "More" });
       await userEvent.click(more);
       expect(more).toHaveAttribute("aria-expanded", "true");
       expect(rail.getByRole("link", { name: "Changes" })).toBeVisible();
     });

     it("anchors the Assistant and Admin below the main rail, in their own navigation", () => {
       renderAt("/devices");
       const anchored = screen.getByRole("navigation", { name: "Assistant and Admin" });
       expect(within(anchored).getByRole("link", { name: "Assistant" })).toHaveAttribute("href", "/assistant");
       expect(within(anchored).getByRole("button", { name: "Admin" })).toBeInTheDocument();
     });

     it("marks the current page with its dot, which is decoration only", () => {
       const rail = renderAt("/devices");
       const current = rail.getByRole("link", { name: "Devices" });
       expect(current.querySelector(".nav-dot")).toHaveAttribute("aria-hidden", "true");
     });

     it("shows the brand: the symbol on its tile and the wordmark", () => {
       const rail = renderAt("/devices");
       expect(rail.getByRole("img", { name: "Novieri" })).toBeInTheDocument();
     });

     // Kept from "the second layer" (2026-09-23), on the Admin panel now.
     it("gives an open area's pages their own icons, like every other item", () => {
       const rail = renderAt("/notifications");
       expect(rail.getByRole("link", { name: "Email intake" }).querySelector("svg")).not.toBeNull();
     });

     // Kept from "the second layer" (2026-09-23), on the Admin panel now.
     it("opens only the pages this operator may reach", () => {
       const rail = renderAt("/enrollment", ["devices.read"]);
       expect(rail.getByRole("link", { name: "Device groups" })).toBeInTheDocument();
       expect(rail.queryByRole("link", { name: "Settings" })).toBeNull();
     });
   });
   ```
   con `import userEvent from "@testing-library/user-event";` entre las importaciones del archivo (hoy no la tiene).

`frontend/src/shell/newDestinations.test.tsx` (cada entrada de «Nuevo» abre de verdad lo que promete):

```tsx
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../fixtures/client";
import { ChangesPage } from "../modules/change/ChangesPage";
import { ClientDetailPage } from "../modules/clients/ClientDetailPage";
import { ClientsPage } from "../modules/clients/ClientsPage";
import { DocsPage } from "../modules/docs/DocsPage";
import { TicketListPage } from "../modules/service-desk/TicketListPage";
import { renderPage } from "../test/renderPage";

const mockUseApi = vi.fn();
vi.mock("../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

// The fixture person holds every code in every client (`src/fixtures/core.ts`).
beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

describe("every New entry is a real destination (redesign design §1.2, decision D8)", () => {
  it("Ticket opens New ticket on the ticket list", async () => {
    renderPage("/service-desk?new=ticket", <TicketListPage />);
    expect(await screen.findByRole("dialog", { name: "New ticket" })).toBeInTheDocument();
  });

  it("Client opens New client on the client list", async () => {
    renderPage("/clients?new=client", <ClientsPage />);
    expect(await screen.findByRole("dialog", { name: "New client" })).toBeInTheDocument();
  });

  it("Contact, with every client shown, first asks which client", async () => {
    renderPage("/clients?new=contact", <ClientsPage />);
    expect(await screen.findByRole("dialog", { name: "New contact" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Client" })).toBeInTheDocument();
  });

  it("Contact, for a chosen client, opens Add contact on its contacts tab", async () => {
    renderPage(
      "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=contacts&new=contact",
      <ClientDetailPage />,
      "/clients/:clientId",
    );
    expect(await screen.findByRole("dialog", { name: "Add a contact" })).toBeInTheDocument();
  });

  it("Document opens the article editor", async () => {
    renderPage("/docs?new=article", <DocsPage />);
    expect(await screen.findByRole("textbox", { name: "Title" })).toBeInTheDocument();
  });

  it("Change opens New change", async () => {
    renderPage("/changes?new=change", <ChangesPage />);
    expect(await screen.findByRole("dialog", { name: "New change" })).toBeInTheDocument();
  });
});
```

(Los nombres de los diálogos son los títulos de hoy en el catálogo inglés, comprobados el 2026-11-24: `serviceDesk.newTicket.title` «New ticket» (`i18n/en/serviceDesk.ts:189`), `clients.list.createTitle` «New client» (`clients.ts:34`), `clients.contacts.formAddTitle` «Add a contact» (`clients.ts:144`), y el diálogo del cambio, `change.form.newTitle` «New change» (`change.ts:114`, el que dibuja `ChangeRequestForm.tsx:210`). El editor de artículos no es un diálogo: la prueba busca su campo «Title». «New contact» es la clave nueva `clients.list.newContactTitle` de este paso.)

- [ ] **Step 3: Ejecutarlas y verlas fallar**

Run: `cd frontend && npx vitest run src/shell/nav.test.ts src/shell/newMenu.test.ts src/shell/NewMenu.test.tsx src/shell/AdminPanel.test.tsx src/shell/Sidebar.test.tsx src/shell/newDestinations.test.tsx`
Expected: FAIL (no existen `isFolder`, `newMenu`, `NewMenu`, el panel, las carpetas ni los parámetros `?new=`).

- [ ] **Step 4: El modelo de navegación**

En `frontend/src/shell/nav.ts`, borrar la `function group(label: MessageKey, …)` de la línea 68 y sustituir todo lo que va desde `export type RailEntry = NavItem | NavArea;` (línea 131) hasta el `];` que cierra `NAV_GROUPS` (línea 213) por:

```ts
/**
 * A FOLDER IN THE RAIL (redesign design §1.1, decision D9): Assets and More.
 * A heading that opens in place, not a page; each of its pages is a rail
 * destination of its own. No permission of its own: it shows when one of its
 * pages does.
 */
export interface NavFolder {
  readonly kind: "folder";
  readonly key: "assets" | "more";
  readonly label: string;
  readonly icon: string;
  readonly items: readonly NavItem[];
}

export type RailEntry = NavItem | NavArea | NavFolder;

/** `main` is the rail; `anchored` is its foot (the Assistant, Admin); `help` is the requester's. */
export interface NavGroup {
  readonly key: "main" | "anchored" | "help";
  readonly label: string;
  readonly items: readonly RailEntry[];
}

export function isArea(entry: RailEntry): entry is NavArea {
  return "sections" in entry;
}

export function isFolder(entry: RailEntry): entry is NavFolder {
  return "kind" in entry && entry.kind === "folder";
}

function group(key: NavGroup["key"], label: MessageKey, items: readonly RailEntry[]): NavGroup {
  return named({ key, items }, { label });
}

function folder(key: NavFolder["key"], label: MessageKey, icon: string, items: readonly NavItem[]): NavFolder {
  return named({ kind: "folder" as const, key, icon, items }, { label });
}

/**
 * THE RAIL IN ATERA'S ORDER (redesign design §1.1, approved 2026-11-23): no
 * group captions; Clients third, out of Admin; Assets and More as folders, More
 * holding only the optional modules on for the choice; the Assistant and Admin
 * anchored at the foot. Every item keeps the permission code its route
 * enforces server-side -- the codes did not change, only the order.
 */
export const NAV_GROUPS: readonly NavGroup[] = [
  group("main", "shell.nav.group.main", [
    page("/dashboard", "shell.nav.item.dashboard", "dashboard", null),
    page("/service-desk", "shell.nav.item.tickets", "ticket", "ticket.read"),
    page("/clients", "shell.nav.item.clients", "building", "clients.read", { hint: "shell.nav.hint.clients" }),
    page("/devices", "shell.nav.item.devices", "devices", "devices.read"),
    page("/health", "shell.nav.item.alerts", "health", "health.read", { badgeTone: "gold" }),
    page("/patch", "shell.nav.item.patching", "patch", "patch.read"),
    page("/deploy", "shell.nav.item.software", "package", "deploy.read"),
    page("/backup", "shell.nav.item.backup", "backup", "backup.read"),
    page("/remote", "shell.nav.item.remoteSessions", "remote", "remote.control"),
    folder("assets", "shell.nav.item.assets", "archive", [
      page("/licenses", "shell.nav.item.licenses", "license", "licenses.read", { hint: "shell.nav.hint.licenses" }),
      page("/subscriptions", "shell.nav.item.subscriptions", "subscription", "subscriptions.read", { hint: "shell.nav.hint.subscriptions" }),
      page("/ipam", "shell.nav.item.ipAddresses", "network", "ipam.read", { hint: "shell.nav.hint.ipam" }),
    ]),
    page("/docs", "shell.nav.item.itDocumentation", "book", "docs.read", { hint: "shell.nav.hint.docs" }),
    page("/reports", "shell.nav.item.reports", "chart", "reporting.read", { hint: "shell.nav.hint.reports" }),
    folder("more", "shell.nav.item.more", "dots", [
      page("/changes", "shell.nav.item.changes", "changes", "change.read", { hint: "shell.nav.hint.changes", module: "change" }),
      page("/bcp", "shell.nav.item.continuityTests", "bcp", "bcp.read", { hint: "shell.nav.hint.bcp", module: "bcp" }),
      page("/budget", "shell.nav.item.itBudget", "budget", "budget.read", { hint: "shell.nav.hint.budget", module: "budget" }),
    ]),
  ]),
  group("anchored", "shell.nav.group.main", [
    page("/assistant", "shell.nav.item.assistant", "sparkles", "ai.use"),
    named(
      {
        to: "/admin",
        icon: "adjustments",
        sections: [
          section("shell.nav.section.serviceDesk", [
            page("/notifications", "shell.nav.item.notifications", "bell", "notification.read", { hint: "shell.nav.hint.notifications" }),
            page("/email-intake", "shell.nav.item.emailIntake", "mail", "email_intake.read", { hint: "shell.nav.hint.emailIntake" }),
            page("/sla", "shell.nav.item.serviceLevels", "clock", "sla.read", { hint: "shell.nav.hint.sla" }),
            page("/portal", "shell.nav.item.endUserPortal", "portal", "portal.ticket.read", { hint: "shell.nav.hint.portal" }),
          ]),
          section("shell.nav.section.computers", [
            page("/enrollment", "shell.nav.item.enrollment", "certificate", "devices.read", { hint: "shell.nav.hint.enrollment" }),
            page("/agent-packages", "shell.nav.item.agentPackages", "package", "agent.release.read", { hint: "shell.nav.hint.agentPackages" }),
            // The scope code, not `agent.push.run`: both screens behind this
            // entry require it server-side.
            page("/agent-push/authorizations", "shell.nav.item.pushInstall", "shield", "agent.push.scope.manage", { hint: "shell.nav.hint.pushInstall" }),
            page("/device-groups", "shell.nav.item.deviceGroups", "building", "devices.read", { hint: "shell.nav.hint.deviceGroups" }),
            page("/site-servers", "shell.nav.item.siteServers", "server", "site.read", { hint: "shell.nav.hint.siteServers" }),
          ]),
          section("shell.nav.section.automation", [
            page("/assistant/autonomy", "shell.nav.item.aiAutomation", "automation", "ai.admin", { hint: "shell.nav.hint.aiAutomation" }),
            page("/jobs", "shell.nav.item.jobHistory", "jobs", "jobs.read", { hint: "shell.nav.hint.jobHistory" }),
          ]),
          section("shell.nav.section.security", [
            page("/credentials", "shell.nav.item.credentials", "lock", "credentials.read", { hint: "shell.nav.hint.credentials" }),
            page("/audit", "shell.nav.item.auditLog", "audit", "audit.read", { hint: "shell.nav.hint.auditLog" }),
            page("/security", "shell.nav.item.securityLogs", "logs", "audit.read", { hint: "shell.nav.hint.securityLogs" }),
          ]),
          section("shell.nav.section.platform", [
            // Not `null`: the page calls `GET /platform/status` on mount,
            // which requires this code.
            page("/platform-status", "shell.nav.item.platformStatus", "status", "platform.status.read", { hint: "shell.nav.hint.platformStatus" }),
            page("/error-monitor", "shell.nav.item.errorMonitor", "warning", "platform.errors.read", { hint: "shell.nav.hint.errorMonitor" }),
            page("/settings", "shell.nav.item.settings", "settings", "orgs.manage", { hint: "shell.nav.hint.settings" }),
          ]),
        ],
      },
      { label: "shell.nav.item.admin", description: "shell.nav.adminDescription" },
    ),
  ]),
];
```

(Borrar la `isArea` vieja, que se mueve arriba. El comentario de cabecera del archivo cambia su primer párrafo a: «THE RAIL IN ATERA'S ORDER (redesign 2026-11-23): fourteen destinations with no captions, two folders, and the Assistant and Admin anchored at the foot; Admin swaps the rail for its own panel.»; el resto del comentario, sobre nombres de tareas y autorización cosmética, se conserva.)

`navItems` y una función nueva `ownersOf`:

```ts
/** Every page in these groups: rail items, folder pages, and every area's pages. */
export function navItems(groups: readonly NavGroup[] = NAV_GROUPS): NavItem[] {
  return groups.flatMap((group) =>
    group.items.flatMap((entry) =>
      isFolder(entry) ? [...entry.items] : isArea(entry) ? entry.sections.flatMap((section) => section.items) : [entry],
    ),
  );
}

/** The rail's folders (Assets, More) in these groups, for the guards that read every icon and every page. */
export function navFolders(groups: readonly NavGroup[] = NAV_GROUPS): NavFolder[] {
  return groups.flatMap((group) => group.items.filter(isFolder));
}

/** What can own a destination: an item, a folder's page (it owns itself) or an area. */
function ownersOf(groups: readonly NavGroup[]): (NavItem | NavArea)[] {
  return groups.flatMap((group) => group.items.flatMap((entry) => (isFolder(entry) ? [...entry.items] : [entry])));
}
```

`destinations` usa `ownersOf(groups)` donde hoy usa `groups.flatMap((group) => group.items)`, tanto para las propias como para `NAV_UNLISTED` (así un `under` podría ser una página de carpeta). `RailEntry` en la firma de `Destination.owner` pasa a `NavItem | NavArea`.

`visibleGroups`, la rama del `flatMap`:

```ts
      items: group.items.flatMap((entry): RailEntry[] => {
        if (isFolder(entry)) {
          const items = entry.items.filter(allowed);
          return items.length > 0 ? [{ ...entry, items }] : [];
        }
        if (!isArea(entry)) return allowed(entry) ? [entry] : [];
        const sections = entry.sections
          .map((section) => ({ label: section.label, items: section.items.filter(allowed) }))
          .filter((section) => section.items.length > 0);
        return sections.length > 0 ? [{ ...entry, sections }] : [];
      }),
```

y el objeto que devuelve cada grupo lleva `key: group.key` además de `label`.

`searchDestinations`: una página de carpeta lleva `within: folder.label`. Su `flatMap` empieza ahora así (las ramas de área y de ítem siguen como hoy):

```ts
    group.items.flatMap((entry): SearchDestination[] =>
      isFolder(entry)
        ? entry.items.map((item) => ({ to: item.to, label: item.label, icon: item.icon, within: entry.label }))
        : isArea(entry)
```

`PORTAL_GROUPS`: `group("help", "shell.nav.group.help", [...])`.

- [ ] **Step 5: `newMenu.ts` y `NewMenu.tsx`**

`frontend/src/shell/newMenu.ts`:

```ts
/**
 * THE "NEW" MENU'S ENTRIES (redesign design §1.2): Ticket, Client, Contact,
 * Computer, Document and Change, each a real destination (decision D8:
 * `?new=` opens the page's own create dialog) and each shown only with the
 * code that page's own create button checks. Cosmetic, like the rail
 * (invariant 6): the server refuses the write either way.
 */
import type { MeClient } from "../lib/api";
import { ADD_COMPUTER_TO } from "./nav";

export type NewMenuKind = "ticket" | "client" | "contact" | "device" | "document" | "change";

export interface NewMenuEntry {
  readonly kind: NewMenuKind;
  readonly to: string;
  readonly icon: string;
}

export interface NewMenuInput {
  /** `/me.permissions`: codes held in at least one client of the current choice. */
  readonly permissions: readonly string[];
  /** `/me.permissions_everywhere`: codes held in every client. */
  readonly everywhere: readonly string[];
  /** `/me.clients`, each with the codes the person holds there. */
  readonly clients: readonly MeClient[];
  /** The client chosen in the top bar; `null` is every client. */
  readonly selected: string | null;
  /** The optional modules on for the current choice. */
  readonly modules: ReadonlySet<string>;
}

export function newMenuEntries(input: NewMenuInput): NewMenuEntry[] {
  const held = new Set(input.permissions);
  const entries: NewMenuEntry[] = [];
  // TicketListPage's New ticket: `ticket.write`.
  if (held.has("ticket.write")) entries.push({ kind: "ticket", to: "/service-desk?new=ticket", icon: "ticket" });
  // ClientsPage's New client: `clients.manage` in every client (a global route).
  if (input.everywhere.includes("clients.manage")) entries.push({ kind: "client", to: "/clients?new=client", icon: "building" });
  // ContactsTab's Add: `clients.manage` in THAT client, never Unassigned.
  const contactable = input.clients.filter(
    (client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"),
  );
  if (input.selected !== null) {
    const chosen = contactable.find((client) => client.id === input.selected);
    if (chosen) entries.push({ kind: "contact", to: `/clients/${chosen.id}?tab=contacts&new=contact`, icon: "user" });
  } else if (contactable.length > 0) {
    entries.push({ kind: "contact", to: "/clients?new=contact", icon: "user" });
  }
  // The single "Add a computer" entry (nav.ts): `devices.read`, as the top bar had it.
  if (held.has("devices.read")) entries.push({ kind: "device", to: ADD_COMPUTER_TO, icon: "devices" });
  // DocsPage's New article: `docs.manage`.
  if (held.has("docs.manage")) entries.push({ kind: "document", to: "/docs?new=article", icon: "book" });
  // ChangesPage's New change: `change.request`, and the module on for the choice.
  if (input.modules.has("change") && held.has("change.request")) {
    entries.push({ kind: "change", to: "/changes?new=change", icon: "changes" });
  }
  return entries;
}
```

`frontend/src/shell/NewMenu.tsx`:

```tsx
/**
 * THE "NEW" MENU (redesign design §1.2; Atera's "New ▾"): every way to create
 * something, from every page. The chrome's one primary control (DESIGN.md v4,
 * "Buttons"). Nothing at all when the person may create nothing: no dead
 * control. A WAI-ARIA menu button: opens on its first item, arrows move,
 * Escape closes and gives focus back.
 */
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import type { MessageKey } from "../i18n/core";
import { Icon } from "./icons";
import type { NewMenuEntry, NewMenuKind } from "./newMenu";

const LABEL: Record<NewMenuKind, MessageKey> = {
  ticket: "shell.newMenu.ticket",
  client: "shell.newMenu.client",
  contact: "shell.newMenu.contact",
  device: "shell.newMenu.device",
  document: "shell.newMenu.document",
  change: "shell.newMenu.change",
};

export function NewMenu({ entries }: { entries: readonly NewMenuEntry[] }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) button.current?.focus();
  };

  useEffect(() => {
    if (!open) return undefined;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close(true);
      }
    };
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !button.current?.contains(target)) close(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const items = Array.from(menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  };

  if (entries.length === 0) return null;
  return (
    <div className="new-menu-anchor">
      <button
        type="button"
        ref={button}
        className="btn primary new-menu-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="plus" size={14} />
        <span className="new-menu-label">{t("shell.newMenu.label")}</span>
        <Icon name="chevron-down" size={14} className="chev" />
      </button>
      {open ? (
        <div className="top-menu" role="menu" aria-label={t("shell.newMenu.label")} ref={menu} onKeyDown={move}>
          {entries.map((entry) => (
            <Link key={entry.kind} className="menu-item" role="menuitem" to={entry.to} onClick={() => close(false)}>
              <Icon name={entry.icon} size={15} />
              {t(LABEL[entry.kind])}
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 6: `Sidebar.tsx` y `AdminPanel.tsx`**

`frontend/src/shell/Sidebar.tsx`, archivo completo:

```tsx
/**
 * The rail (redesign design §1.1; DESIGN.md v4, "Navigation"): Atera's order
 * with no captions, the Assets and More folders (decision D9), and the
 * Assistant and Admin anchored at the foot; Admin swaps the rail for its own
 * panel (decision D10, `AdminPanel`).
 *
 * THE HIDING IS COSMETIC, AND SAYING SO MATTERS. Invariant 6 -- the frontend
 * never gates security. Every route behind these links is enforced
 * server-side by `require_permission`; if this component rendered every link
 * to everyone, the platform would be exactly as secure and merely ruder.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

import { useT } from "../i18n/context";
import { AdminPanel } from "./AdminPanel";
import { NovieriSymbol, NovieriWordmark } from "./brand";
import { Icon } from "./icons";
import { activeNavTo, isArea, isFolder, OPTIONAL_MODULES, visibleGroups, type NavFolder, type NavItem } from "./nav";

export interface SidebarProps {
  permissions: readonly string[];
  /** Optional modules on for the current choice (phase 3 design §2). Absent: every one. */
  modules?: ReadonlySet<string>;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Live counts, keyed by route. Absent = no badge, which is not the same as zero. */
  badges?: Readonly<Record<string, number>>;
  orgName: string;
  /** The client selector, on a handset only: it heads the drawer there (`./handset`). */
  clientSelector?: ReactNode;
}

/** One destination in the rail. The current one carries `aria-current` and its dot. */
export function RailLink({
  item,
  lit,
  count,
  sub = false,
}: {
  item: NavItem;
  lit: boolean;
  count?: number;
  sub?: boolean;
}) {
  // `.hot` is the alert count, drawn as a critical badge (DESIGN.md v4, D3).
  const hot = item.badgeTone !== undefined;
  return (
    <Link
      className={`nav-item${sub ? " sub" : ""}${lit ? " active" : ""}`}
      aria-current={lit ? "page" : undefined}
      to={item.to}
      title={item.label}
    >
      <Icon name={item.icon} size={sub ? 16 : 17} />
      <span className="lbl">{item.label}</span>
      {/* A zero renders nothing: "nothing to deal with" is not a 0 beside the label. */}
      {count ? <span className={hot ? "ct hot" : "ct"}>{count}</span> : null}
      {lit ? <span className="nav-dot" aria-hidden="true" /> : null}
    </Link>
  );
}

/** Assets or More: a heading that opens its pages in place, open by itself when it holds the current page. */
function Folder({
  folder,
  activeTo,
  badges,
}: {
  folder: NavFolder;
  activeTo: string | undefined;
  badges: Readonly<Record<string, number>>;
}) {
  const holds = folder.items.some((item) => item.to === activeTo);
  const [open, setOpen] = useState(holds);
  useEffect(() => {
    if (holds) setOpen(true);
  }, [holds]);
  const id = `nav-folder-${folder.key}`;
  return (
    <div className="nav-folder">
      <button
        type="button"
        className={holds && !open ? "nav-item nav-folder-h active" : "nav-item nav-folder-h"}
        aria-expanded={open}
        aria-controls={id}
        title={folder.label}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name={folder.icon} size={17} />
        <span className="lbl">{folder.label}</span>
        <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
      </button>
      <div id={id} hidden={!open}>
        {folder.items.map((item) => (
          <RailLink key={item.to} item={item} lit={item.to === activeTo} count={badges[item.to]} sub />
        ))}
      </div>
    </div>
  );
}

export function Sidebar({
  permissions,
  modules,
  collapsed,
  onToggleCollapsed,
  badges = {},
  orgName,
  clientSelector,
}: SidebarProps) {
  const t = useT();
  const groups = visibleGroups(permissions, modules ?? OPTIONAL_MODULES);
  const pathname = useLocation().pathname;
  const activeTo = activeNavTo(pathname, groups);
  const main = groups.find((group) => group.key === "main")?.items ?? [];
  const anchored = groups.find((group) => group.key === "anchored")?.items ?? [];
  const admin = anchored.find(isArea);
  const inAdmin = admin !== undefined && activeTo === admin.to;
  const [view, setView] = useState<"main" | "admin">(inAdmin ? "admin" : "main");
  // Arriving on an Admin page opens its panel; leaving Admin puts the rail back.
  useEffect(() => setView(inAdmin ? "admin" : "main"), [inAdmin]);

  return (
    <aside className="sidebar" id="sidebar">
      <div className="brand">
        <NovieriSymbol tile size={28} />
        <NovieriWordmark className="brand-name" height={14} />
        {/* Density is a standing preference and the shell persists it. */}
        <button
          className="collapse-btn"
          onClick={onToggleCollapsed}
          title={collapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          aria-label={collapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          aria-expanded={!collapsed}
        >
          <Icon name={collapsed ? "chevron-right" : "chevron-left"} size={15} />
        </button>
      </div>

      {clientSelector ? <div className="sidebar-client">{clientSelector}</div> : null}

      {view === "admin" && admin ? (
        <AdminPanel area={admin} pathname={pathname} collapsed={collapsed} onBack={() => setView("main")} />
      ) : (
        <nav className="nav-scroll" id="nav" aria-label={t("shell.sidebar.main")}>
          {main.map((entry) =>
            isFolder(entry) ? (
              <Folder key={entry.key} folder={entry} activeTo={activeTo} badges={badges} />
            ) : isArea(entry) ? null : (
              <RailLink key={entry.to} item={entry} lit={entry.to === activeTo} count={badges[entry.to]} />
            ),
          )}
        </nav>
      )}

      <nav className="nav-anchored" aria-label={t("shell.sidebar.anchored")}>
        {anchored.map((entry) =>
          isArea(entry) ? (
            <button
              key={entry.to}
              type="button"
              className={inAdmin ? "nav-item active" : "nav-item"}
              aria-expanded={view === "admin"}
              aria-controls="nav"
              title={entry.label}
              onClick={() => setView(view === "admin" ? "main" : "admin")}
            >
              <Icon name={entry.icon} size={17} />
              <span className="lbl">{entry.label}</span>
              {inAdmin ? <span className="nav-dot" aria-hidden="true" /> : null}
            </button>
          ) : isFolder(entry) ? null : (
            <RailLink key={entry.to} item={entry} lit={entry.to === activeTo} count={badges[entry.to]} />
          ),
        )}
      </nav>

      {/* IDENTITY, NOT A CONTROL: the one place the tenant is named. */}
      <div className="sidebar-footer">
        <div className="tenant-card" data-testid="tenant-identity">
          <div className="tenant-avatar">{orgName.slice(0, 2).toUpperCase()}</div>
          <div className="tenant-meta">
            <div className="tenant-name">{orgName}</div>
            <div className="tenant-plan">{t("shell.sidebar.plan")}</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
```

`frontend/src/shell/AdminPanel.tsx`:

```tsx
/**
 * ADMIN, AS ATERA DRAWS IT (redesign design §1.1, decision D10): the rail gives
 * way to a panel with a way back, a search and an accordion of the area's
 * sections, the one holding the current page open. The same pages and codes
 * as before; only Clients left, for the main rail. Folded, the panel is its
 * pages' icons.
 */
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { useT } from "../i18n/context";
import { SearchField } from "./components";
import { Icon } from "./icons";
import { activeNavTo, type NavArea } from "./nav";
import { RailLink } from "./Sidebar";

/** For matching a search: case and accents do not count ("auditoria" finds "auditoría"). */
export function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

export function AdminPanel({
  area,
  pathname,
  collapsed,
  onBack,
}: {
  area: NavArea;
  pathname: string;
  collapsed: boolean;
  onBack: () => void;
}) {
  const t = useT();
  const base = useId();
  const [query, setQuery] = useState("");
  const pages = area.sections.flatMap((section) => section.items);
  const current = activeNavTo(pathname, [{ key: "main", label: "", items: pages }]);
  const holding = area.sections.find((section) => section.items.some((item) => item.to === current))?.label;
  const [openSection, setOpenSection] = useState<string | undefined>(holding ?? area.sections[0]?.label);
  const wanted = fold(query);
  const sections = area.sections
    .map((section) => ({
      label: section.label,
      items: wanted === "" ? section.items : section.items.filter((item) => fold(item.label).includes(wanted)),
    }))
    .filter((section) => section.items.length > 0);

  return (
    <nav className="nav-scroll admin-panel" id="nav" aria-label={area.label}>
      <div className="admin-head">
        <button type="button" className="nav-item admin-back" onClick={onBack} title={t("shell.sidebar.backToMenu")}>
          <Icon name="chevron-left" size={16} />
          <span className="lbl">{t("shell.sidebar.backToMenu")}</span>
        </button>
        <Link
          className={pathname === area.to ? "admin-title active" : "admin-title"}
          to={area.to}
          title={area.label}
          aria-current={pathname === area.to ? "page" : undefined}
        >
          {area.label}
        </Link>
      </div>
      {collapsed ? null : (
        <div className="admin-search">
          <SearchField
            type="search"
            aria-label={t("shell.sidebar.adminSearch")}
            placeholder={t("shell.sidebar.adminSearch")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
      )}
      {sections.length === 0 ? <p className="admin-none">{t("shell.sidebar.adminNoMatch")}</p> : null}
      {sections.map((section, index) => {
        const open = collapsed || wanted !== "" || section.label === openSection;
        const id = `${base}-${index}`;
        return (
          <div key={section.label} className="admin-section">
            {collapsed ? null : (
              <button
                type="button"
                className="nav-item admin-acc"
                aria-expanded={open}
                aria-controls={id}
                onClick={() => setOpenSection(open && wanted === "" ? undefined : section.label)}
              >
                <span className="lbl">{section.label}</span>
                <span className="ct">{section.items.length}</span>
                <Icon name="chevron-down" size={14} className={open ? "chev open" : "chev"} />
              </button>
            )}
            <div id={id} hidden={!open}>
              {section.items.map((item) => (
                <RailLink key={item.to} item={item} lit={item.to === current} sub />
              ))}
            </div>
          </div>
        );
      })}
    </nav>
  );
}
```

(`AdminPanel` importa `RailLink` de `Sidebar` y `Sidebar` importa `AdminPanel`: la referencia circular es segura por el mismo motivo que la del kit, se usan solo al renderizar.)

- [ ] **Step 7: La barra superior, `Shell` y la portada de Administración**

`frontend/src/shell/AppShell.tsx`:
- prop nueva en `AppShellProps`: `/** The New menu's entries (newMenu.ts), computed by the app from /me. Absent: no menu. */ newMenu?: readonly NewMenuEntry[];`, desestructurada en `AppShell`;
- dentro de `<header className="topbar">`, justo después del botón `.nav-toggle`: `<NewMenu entries={newMenu ?? []} />`;
- borrar el bloque `{permissions.includes("devices.read") ? (<Link className="top-enrol primary" …>…</Link>) : null}` y su comentario (líneas 547-568); en su lugar: `{/* ADD A COMPUTER LEFT THE BAR (redesign design §1.2, decision D11): it is New → Computer and the Devices page's own head. */}`;
- borrar el bloque `{permissions.includes("ai.use") ? (<Link className="ib top-icon-btn" to="/assistant" …>…</Link>) : null}` y **su comentario de la regla del 2026-09-08** (líneas 573-592, de `{/* ONE CHROME SHORTCUT, AND IT IS THE ASSISTANT.` al `) : null}`), y en su lugar este comentario, que es el que la sustituye:
  ```tsx
            {/* THE ASSISTANT IS NOT IN THE BAR (decision D12; the operator,
                2026-11-24, on the redesign plan: "Como queda en las capturas
                de atera"). Atera's bar -- docs/design/referencias-atera/atera
                1.png -- is search, Install agent, bell, alerts, help and the
                profile, with no AI icon; its AI is anchored at the rail's foot
                and is a round button at the bottom right of every page. This
                console does the same: `nav.ts` anchors the Assistant, and the
                round button is drawn after `<main>` below (`.assistant-fab`).
                This replaces the ruling of 2026-09-08 ("AI Analyst needs to be
                there") that put the icon here. Its other half stands: no
                documentation link in the bar at any width (2026-09-09); /docs
                is a rail item and a search destination. */}
  ```
- en el cuerpo, junto a los demás efectos sobre `<body>` (después del de `sidebar-collapsed`):
  ```tsx
  // THE ASSISTANT'S ROUND BUTTON (decision D12): on every console page for
  // someone with `ai.use`, except the Assistant's own page. While it is drawn,
  // `body.assistant-fab-on` keeps the save bar out of its corner (`shell.css`):
  // Atera's walkthrough files its own AI button covering the save button
  // (Atera_recorrido_menus_UI_UX.md, line 639).
  const assistantFab = permissions.includes("ai.use") && location.pathname !== "/assistant";
  useLayoutEffect(() => {
    document.body.classList.toggle("assistant-fab-on", assistantFab);
    return () => document.body.classList.remove("assistant-fab-on");
  }, [assistantFab]);
  ```
- y dentro de `<div className="app-shell">`, justo después de `</main>`:
  ```tsx
        {/* THE ASSISTANT, ONE TAP FROM EVERY PAGE (decision D12). A link to
            the Assistant's page, as the bar's icon was: not a chat drawn over
            the page, which would be a feature this console does not have. */}
        {assistantFab ? (
          <Link className="assistant-fab" to="/assistant" aria-label={t("shell.app.askAssistant")} title={t("shell.app.askAssistant")}>
            <Icon name="sparkles" size={22} />
          </Link>
        ) : null}
  ```
- quitar `ADD_COMPUTER_TO` de las importaciones; importar `NewMenu` de `./NewMenu` y `type NewMenuEntry` de `./newMenu`.

`frontend/src/App.tsx`, en `Shell`, después de `const modules = useEnabledModules();`:

```tsx
  const { selected } = useClientScope();
  // The New menu (redesign design §1.2): every entry a real destination, each
  // gated on the code its page's own create button checks (`shell/newMenu.ts`).
  const newMenu = useMemo(
    () =>
      newMenuEntries({
        permissions,
        everywhere: me.data?.permissions_everywhere ?? permissions,
        clients: me.data?.clients ?? [],
        selected,
        modules,
      }),
    [permissions, me.data, selected, modules],
  );
```

y `newMenu={newMenu}` en el `<AppShell …>`; importar `useClientScope` de `./lib/clientScope` (si no está) y `newMenuEntries` de `./shell/newMenu`.

`frontend/src/shell/AreaPage.tsx` (portada de `/admin`, una de las 86): cambia la tabla de cada sección por una tarjeta con sus enlaces, en una rejilla de dos columnas, y una búsqueda arriba con el mismo `fold` del panel. Sustituir **desde la línea 33, `if (!area) return null;`, hasta el final de la función** (su `}` de cierre no) por el bloque de abajo, que empieza con el `useState` de la búsqueda **antes** del `if (!area) return null;` (un hook no puede ir detrás de un `return`):

```tsx
  const [query, setQuery] = useState("");
  const wanted = fold(query);
  const shown = sections
    .map((section) => ({ ...section, items: section.items.filter((item) => wanted === "" || fold(item.label).includes(wanted)) }))
    .filter((section) => section.items.length > 0);

  if (!area) return null;
  return (
    <>
      <PageHead title={area.label} description={area.description} />
      <QueryState loading={me.isLoading} error={me.error} empty={sections.length === 0} emptyLabel={t("shell.areaPage.empty")} emptyIcon="lock">
        <Panel bare>
          <ListToolbar
            search={
              <SearchField
                type="search"
                aria-label={t("shell.sidebar.adminSearch")}
                placeholder={t("shell.sidebar.adminSearch")}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            }
          />
        </Panel>
        {shown.length === 0 ? (
          <ListEmpty filtered onClear={() => setQuery("")} nothingYet={t("shell.areaPage.empty")} />
        ) : (
          <div className="admin-index">
            {shown.map((section) => (
              <Panel key={section.label} title={section.label}>
                <ul className="admin-index-list">
                  {section.items.map((item) => (
                    <li key={item.to}>
                      <Icon name={item.icon} size={18} />
                      <div>
                        <Link to={item.to}>{item.label}</Link>
                        {item.hint ? <div className="cell-sub">{item.hint}</div> : null}
                      </div>
                    </li>
                  ))}
                </ul>
              </Panel>
            ))}
          </div>
        )}
      </QueryState>
    </>
  );
```

con `import { useState } from "react";`, `import { fold } from "./AdminPanel";` y `ListEmpty`, `ListToolbar`, `SearchField` de `./components`; se quitan `Table`, `Td`, `Th`. Además, la línea 30 (`.find((entry) => entry.to === to)` sobre `group.items`) pasa a `.find((entry) => "to" in entry && entry.to === to)`: una carpeta no tiene `to` y TypeScript lo exige. `ListEmpty` aquí sirve al buscador: «nada coincide» y «Quitar filtros» vacían la búsqueda.

`frontend/src/shell/AreaPage.test.tsx` (revisión previa, F-I6: los casos buscan hoy `table` con nombre de sección, y una tarjeta es un `Panel`, un `<section>` sin nombre accesible, así que se buscan por su `h2`). Con `import userEvent from "@testing-library/user-event";` entre las importaciones y este ayudante después de `renderArea`:

```tsx
/** A section's card on the Admin index, found by its heading (a `Panel` is an unnamed <section>). */
async function card(name: string): Promise<HTMLElement> {
  return (await screen.findByRole("heading", { level: 2, name })).closest("section") as HTMLElement;
}
```

los tres casos cambian así (lo que afirman es lo mismo; cambia dónde se busca, porque el diseño cambió la tabla por una tarjeta):

```tsx
  it("lists the area's pages this operator may open, grouped, with what each is for", async () => {
    renderArea("/admin", ["notification.read", "jobs.read", "audit.read"]);
    expect(await screen.findByRole("heading", { name: "Admin", level: 1 })).toBeInTheDocument();
    expect(within(await card("Service desk")).getByRole("link", { name: "Notifications" })).toHaveAttribute(
      "href",
      "/notifications",
    );
    expect(within(await card("Automation")).getByRole("link", { name: "Job history" })).toBeInTheDocument();
    expect(screen.getByText("Who did what, and when.")).toBeInTheDocument();
  });

  it("offers no page the rail would hide", async () => {
    renderArea("/admin", ["notification.read"]);
    await screen.findByRole("link", { name: "Notifications" });
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
    expect(screen.queryByRole("heading", { level: 2, name: "Platform" })).toBeNull();
  });
```

(«says so when nothing in the area is open to the account» no cambia.) Y uno nuevo:

```tsx
  it("finds an Admin page by part of its name", async () => {
    renderArea("/admin", ["audit.read", "orgs.manage"]);
    await userEvent.type(await screen.findByRole("searchbox", { name: "Search Admin" }), "audit");
    expect(screen.getByRole("link", { name: "Audit log" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  });
```

- [ ] **Step 8: Los destinos de «Nuevo» (decisión D8)**

- `service-desk/TicketListPage.tsx`: `const [creating, setCreating] = useState(false);` (línea 96) pasa a `const created = useNewParam("ticket");`; el botón de la cabecera llama a `created.start`; `<NewTicketDialog open={created.open} onClose={created.close} …>`. Si la persona no tiene `ticket.write`, el diálogo no se abre aunque la URL lo pida: `open={canWrite && created.open}`.
- `docs/DocsPage.tsx`: `const newArticle = useNewParam("article");` y
  ```tsx
  // New → Document (redesign decision D8): the editor is inline, not a
  // dialog, so the parameter is consumed: it starts a new article once. NOT
  // BEFORE /me HAS ANSWERED: `canManage` is false until then, and consuming
  // the parameter on that first render would drop it with nothing opened
  // (pre-flight F-I4, measured).
  useEffect(() => {
    if (!newArticle.open || me.data === undefined) return;
    if (canManage) startNew();
    newArticle.close();
  }, [newArticle.open, canManage, me.data]);
  ```
  (después de la definición de `startNew`).
- `change/ChangesPage.tsx` (líneas 64, 145 y 275). Hoy `editing` solo guarda `"new"` (`grep -n "setEditing(" src/modules/change/ChangesPage.tsx` da dos líneas, 145 y 275: la página no edita cambios existentes, eso lo hace la ficha), así que la alta pasa entera a la URL y el estado desaparece:
  ```tsx
  // línea 64 (sustituye a `const [editing, setEditing] = useState<…>(null);`):
  // New → Change (redesign decision D8): `?new=change` opens the new form.
  const created = useNewParam("change");
  ```
  el botón de la línea 145 pasa a `<Button onClick={created.start}>`, y la línea 275 a:
  ```tsx
      {/* Open only for someone who may request a change: the same code the
          button checks, so a bare `?new=change` opens nothing for a reader. */}
      <ChangeRequestForm change={created.open && canRequest ? "new" : null} onClose={created.close} />
  ```
  con `useNewParam` importado de `../../shell/components`, y la importación de la línea 2 pasa a `import { useMemo } from "react";` (nada más usa `useState`, y `noUnusedLocals` lo exige). `ChangeRequestForm` no cambia: sigue recibiendo `"new"` o `null`, y el tipo `ChangeRequest` sigue usándose en `isOpen` (línea 41).
- `clients/ClientsPage.tsx`: `const [creating, setCreating] = useState(false);` pasa a `const created = useNewParam("client");` (`open={created.open && everywhere.has("clients.manage")}`); y un diálogo nuevo para `useNewParam("contact")`:
  ```tsx
  /**
   * New → Contact with every client shown (redesign design §1.2): a contact
   * belongs to a client, so the first question is which one. Only clients
   * where the person may manage contacts, never Unassigned -- the same rule
   * ContactsTab applies -- and the answer goes to that client's contacts tab,
   * where the form opens.
   */
  function ContactClientDialog({ onClose }: { onClose: () => void }) {
    const t = useT();
    const navigate = useNavigate();
    const { clients } = useClientScope();
    const choices = clients.filter((client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"));
    const [picked, setPicked] = useState(choices[0]?.id ?? "");
    return (
      <Dialog open onClose={onClose} title={t("clients.list.newContactTitle")}>
        <Form onSubmit={() => picked && navigate(`/clients/${picked}?tab=contacts&new=contact`)}>
          <Field label={t("shell.kit.client")}>
            <select className="select-field" value={picked} onChange={(event) => setPicked(event.target.value)}>
              {choices.map((client) => (
                <option key={client.id} value={client.id}>
                  {client.name}
                </option>
              ))}
            </select>
          </Field>
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              {t("common.actions.cancel")}
            </Button>
            <Button type="submit" disabled={!picked}>
              {t("clients.list.newContactContinue")}
            </Button>
          </div>
        </Form>
      </Dialog>
    );
  }
  ```
  dibujado como `{contact.open ? <ContactClientDialog onClose={contact.close} /> : null}`. Claves nuevas: `clients.list.newContactTitle` («New contact» / «Nuevo contacto»), `clients.list.newContactContinue` («Continue» / «Continuar»).
- `clients/ContactsTab.tsx`: `const newContact = useNewParam("contact");` y el diálogo de alta se abre con `editing === "new" || (canManage && newContact.open)`; al cerrarse llama también a `newContact.close()`.

- [ ] **Step 9: Textos y estilos**

`frontend/src/i18n/en/shell.ts`:
- en `nav.group`: borrar `daily`, `governance`, `assetsSpend` y `configure`; añadir `main: "Main menu",`;
- en `nav.item`: añadir `assets: "Assets",` y `more: "More",`;
- en `nav.section`: borrar `organization`;
- en `sidebar`: añadir `anchored: "Assistant and Admin",`, `backToMenu: "Back to the menu",`, `adminSearch: "Search Admin",`, `adminNoMatch: "No Admin page matches that.",`;
- bloque nuevo `newMenu: { label: "New", ticket: "Ticket", client: "Client", contact: "Contact", device: "Computer", document: "Document", change: "Change" },`.

`frontend/src/i18n/es/shell.ts`, lo mismo en español: `main: "Menú principal"`, `assets: "Activos"`, `more: "Más"`, `anchored: "Asistente y Administración"`, `backToMenu: "Volver al menú"`, `adminSearch: "Buscar en Administración"`, `adminNoMatch: "Ninguna página de Administración coincide."`, `newMenu: { label: "Nuevo", ticket: "Ticket", client: "Cliente", contact: "Contacto", device: "Equipo", document: "Documento", change: "Cambio" }`; y **D13**: `nav.item.dashboard: "Inicio"`, `nav.item.devices: "Equipos"`, `nav.item.itDocumentation: "Documentación"`.

`frontend/src/i18n/identical-ok.ts`: en `IDENTICAL_OK_SHARED` (Task 3, Step 3b), añadir `"shell.newMenu.ticket": "\"Ticket\" is the glossary term for a ticket in Spanish too (a loanword).",`.

**Los textos que nombran entradas del riel que cambian** (revisión previa, S-M-1). D13 cambia «Dispositivos» por «Equipos» en el riel, y D11 lleva «Añadir un equipo» a «Nuevo» y a la cabecera de Equipos; cinco cadenas españolas de cuatro oleadas distintas lo nombran a la antigua. Se cambian aquí, antes de las oleadas, para que ninguna oleada toque el catálogo de otra:
- `i18n/es/serviceDesk.ts:44`, `es/remote.ts:14`, `es/deploy.ts:41` y `es/backup.ts:77`: «(Dispositivos → Añadir un equipo)» → «(Equipos → Añadir un equipo)»;
- `i18n/es/agentReleases.ts:195`: «espera en Dispositivos → Pendientes de revisión» → «espera en Equipos → Pendientes de revisión»;
- y sus pruebas: `modules/service-desk/service-desk.es.test.tsx:1019` (`/Dispositivos → Añadir un equipo/` → `/Equipos → Añadir un equipo/`), `modules/deploy/deploy.es.test.tsx:807` (`"Dispositivos → Añadir un equipo"` → `"Equipos → Añadir un equipo"`), `modules/backup/backup.es.test.tsx:1257` (como la primera).
Las inglesas («Devices → Add a computer», «Devices → Waiting for review») siguen siendo ciertas: el riel inglés no cambia y la O1 pone «Add a computer» en la cabecera de Devices. Después: `grep -rn "Dispositivos →" frontend/src` no imprime nada.

`frontend/src/i18n/es/index.ts`: en la línea del glosario `computer → equipo (…)`, añadir al final del paréntesis `; the machine list, /devices, is "Equipos" in the rail and on its page (redesign 2026-11-23)`.

`frontend/src/shell/shell.css`, en la sección del riel (sustituye las reglas de `.nav-item`, `.nav-item.active`, `.nav-item svg`, `.nav-item.sub*` y `.sidebar .grp*`, que dejan de tener elementos salvo `.nav-item.sub`):

```css
/* The rail's items (DESIGN.md v4, "Navigation"; direction A's mock: 38px,
 * inset 10px, 6px radius). Current: the lifted ground, white text, and the
 * icon and a dot in the rail mark -- no accent edge: cobalt measures 2.32:1
 * on the rail (The Two Grounds Rule). */
.nav-item { display: flex; align-items: center; gap: 11px; min-height: 38px; margin: 1px 10px; padding: 0 12px; border: 0; border-radius: var(--r-ctl); background: transparent; color: var(--rail-ink); font: 400 var(--t-body)/1.2 var(--sans); white-space: nowrap; text-decoration: none; text-align: left; cursor: pointer; width: calc(100% - 20px); }
.nav-item:hover { color: var(--accent-ink); background: var(--rail-2); }
.nav-item.active { color: var(--accent-ink); background: var(--rail-2); font-weight: 500; }
.nav-item svg { flex: 0 0 auto; opacity: .9; }
.nav-item.active svg { color: var(--rail-mark); opacity: 1; }
.nav-item .lbl { flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.nav-item:focus-visible { outline: 2px solid var(--accent-ink); outline-offset: -2px; }
.nav-item.sub { padding-left: 40px; }
.nav-dot { flex: 0 0 auto; width: 7px; height: 7px; border-radius: 50%; background: var(--rail-mark); }
.nav-item .chev { margin-left: auto; transition: transform .15s ease; }
.nav-item .chev.open { transform: rotate(180deg); }
.nav-scroll { padding: 8px 0 16px; }
.nav-anchored { flex: 0 0 auto; padding: 8px 0; border-top: 1px solid var(--rail-2); }
.sidebar .admin-head { display: flex; flex-direction: column; gap: 2px; padding-bottom: 6px; }
.sidebar .admin-title { margin: 6px 22px 2px; color: var(--accent-ink); font-weight: 600; font-size: var(--t-h2); text-decoration: none; }
.sidebar .admin-title:focus-visible { outline: 2px solid var(--accent-ink); outline-offset: 2px; border-radius: var(--r-ctl); }
.admin-search { padding: 4px 10px 8px; }
.sidebar .admin-none { margin: 8px 22px; color: var(--rail-ink); font-size: var(--t-sm); }
.nav-item.admin-acc { font-weight: 500; }
@media (prefers-reduced-motion: reduce) { .nav-item .chev { transition: none; } }
```

y en el bloque `body.sidebar-collapsed` (líneas 479 a 499): sustituir `.sidebar .grp` por `.nav-item .chev, .admin-search, .sidebar .admin-title, .nav-dot` en la lista de `display: none`, y añadir `body.sidebar-collapsed .nav-item, body.sidebar-collapsed .nav-item.sub { justify-content: center; padding: 0; }` en lugar de la regla `.nav-item { justify-content: center; padding: 7px 0; }`. El ancho plegado **se queda en 76 px** (`body.sidebar-collapsed { --sidebar-w: 76px; }`, sin cambios): ni la marca ni el diseño piden otro, y DESIGN.md v4 «Layout» lo dice así (revisión previa, S-M-4).

**La búsqueda, ensanchada** (diseño §1.2): en `.topbar-find` (línea 220), `max-width: 520px;` → `max-width: 640px;`, y el comentario de esa regla pasa a decir «capped at 640px (redesign design §1.2, "ensanchada": the bar lost Add a computer and the Assistant's icon)». `responsive.test.ts` «gives the slack to the search» no cambia: comprueba `flex: 1` y `margin-right: auto`, que se quedan.

**El botón flotante del Asistente** (decisión D12), al final de la sección de la barra superior:

```css
/* THE ASSISTANT'S ROUND BUTTON (DESIGN.md v4, "Assistant button"; decision
 * D12, the operator on 2026-11-24): bottom right on every console page, the
 * one shadow, 48px -- over the 44px target on every pointer. Under the
 * drawer's scrim (35) and every dialog. */
.assistant-fab { position: fixed; right: 24px; bottom: 24px; z-index: 25; display: inline-flex; align-items: center; justify-content: center; width: 48px; height: 48px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); box-shadow: var(--shadow); text-decoration: none; }
.assistant-fab:hover { background: var(--accent-2); }
.assistant-fab:active { background: var(--accent-3); }
.assistant-fab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
/* IT NEVER COVERS THE SAVE BAR (Atera's walkthrough, line 639: "el botón
 * flotante de IA tapa el botón de guardar"). While it is drawn the bar keeps
 * 72px clear on its right (48 + 24), wherever the bar sits, so the two never
 * share the corner; and the page keeps room at its foot for its last line. */
body.assistant-fab-on .savebar { margin-right: 72px; }
body.assistant-fab-on .content { padding-bottom: 88px; }
```

y, dentro del bloque `@media (max-width: 640px)` que ya existe (línea 719):

```css
  .assistant-fab { right: 16px; bottom: 16px; }
  body.assistant-fab-on .savebar { margin-right: 64px; }
```

Para «Nuevo» y la portada de Administración (fuera del riel):

```css
/* The New menu (redesign design §1.2): the chrome's one primary, and its list. */
.new-menu-anchor { position: relative; flex: 0 0 auto; }
.new-menu-btn .chev { margin-right: -3px; }
.top-menu { position: absolute; top: calc(100% + 6px); left: 0; z-index: 60; min-width: 220px; padding: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-panel); box-shadow: var(--shadow); }
.top-menu .menu-item { display: flex; align-items: center; gap: 9px; }
@media (max-width: 640px) {
  .new-menu-btn .new-menu-label { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .new-menu-btn .chev { display: none; }
}

/* The Admin index (/admin): its sections as cards. */
.admin-index { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.admin-index-list { display: grid; gap: 12px; margin: 0; padding: 0; list-style: none; }
.admin-index-list li { display: flex; align-items: flex-start; gap: 10px; }
@media (max-width: 1024px) { .admin-index { grid-template-columns: minmax(0, 1fr); } }
```

Y borrar las reglas `.top-enrol*` (el elemento ya no existe).

`base-styles.test.ts`: `.nav-item.sub` y `.nav-item.sub.active` dejan de ser la transcripción de v3. Añadir a `V4_OVERRIDES` (Task 3, Step 7):

```ts
  // DESIGN.md v4 "Navigation" (redesign §1.1): a folder or Admin page is
  // indented by its padding, and its current state is the rail's own (no
  // second rule for it).
  ".nav-item.sub": ["padding-left: 40px"],
  ".nav-item.sub.active": [],
```

y el caso `it.each([".nav-item.sub", ".nav-item.sub.active"])` pasa a comprobar `expectedFor(design, selector)`; para `.nav-item.sub.active` (sin regla propia ahora) el caso comprueba que `declarations(shell, ".nav-item.sub.active")` es `[]`.

- [ ] **Step 10: Las pruebas del armazón que fijaban la barra de antes**

`frontend/src/shell/AppShell.test.tsx`, cambios deliberados, cada uno con un comentario que cita el diseño o la decisión. El ayudante `sidebar()` sigue buscando `{ name: "Main" }`: ninguna prueba del archivo buscaba en él el enlace «Admin» (`grep -n '"Admin"' src/shell/AppShell.test.tsx` no da nada), así que no hay más que cambiar ahí.

1. **Las tres pruebas del riel que fijaban los títulos de grupo** (revisión previa, F-I2: las tres fallan con el riel nuevo). Cada una pasa a:
   ```tsx
     it("does not leave an empty folder behind", () => {
       // Redesign design §1.1: no group captions any more ("Daily",
       // "Governance"); what can be left empty now is a folder. With only
       // patch.read neither Assets nor More has a page to show.
       renderShell(["patch.read"]);
       expect(sidebar().getByRole("link", { name: /Patching/ })).toBeInTheDocument();
       expect(sidebar().queryByRole("button", { name: "Assets" })).toBeNull();
       expect(sidebar().queryByRole("button", { name: "More" })).toBeNull();
     });

     it("is not vacuous: with the permissions, the folders appear and open on their pages", async () => {
       // NON-VACUITY. The assertions above are about ABSENCE and would pass
       // against a rail that rendered nothing at all.
       renderShell(["devices.read", "patch.read", "budget.read", "backup.read", "docs.read", "licenses.read"]);
       expect(sidebar().getByRole("link", { name: /IT documentation/ })).toBeInTheDocument();
       expect(sidebar().getByRole("button", { name: "Assets" })).toBeInTheDocument();
       await user.click(sidebar().getByRole("button", { name: "More" }));
       expect(sidebar().getByRole("link", { name: /IT budget/ })).toHaveAttribute("href", "/budget");
     });

     it("hides a module's page when no client of the choice uses the module (phase 3 design §2)", async () => {
       const shell = (modules: ReadonlySet<string>) => (
         <MemoryRouter>
           <AppShell
             permissions={["budget.read"]}
             modules={modules}
             orgName="Operations"
             title="Dashboard"
             user={{ name: "A B", role: "Operator" }}
             onSignOut={vi.fn()}
           >
             <p>x</p>
           </AppShell>
         </MemoryRouter>
       );
       const { unmount } = render(shell(new Set()));
       // No optional module on: More is not drawn at all (decision D9).
       expect(sidebar().queryByRole("button", { name: "More" })).toBeNull();
       expect(sidebar().queryByRole("link", { name: /IT budget/ })).not.toBeInTheDocument();
       unmount();
       render(shell(new Set(["budget"])));
       await user.click(sidebar().getByRole("button", { name: "More" }));
       expect(sidebar().getByRole("link", { name: /IT budget/ })).toHaveAttribute("href", "/budget");
     });
   ```
   (la primera se llamaba «does not leave an empty area behind» y la segunda «is not vacuous: with the permissions, the hidden areas appear»: el nombre cambia porque lo que puede quedar vacío ahora es una carpeta; lo que afirman, ausencia sin permiso y presencia con él, es lo mismo.)
2. `describe("the top bar's enrol affordance")` (líneas 1318 a 1390) pasa a `describe("Add a computer, in New (decision D11)")`: «puts getting an agent onto a machine on every page» monta el armazón con `newMenu={[{ kind: "device", to: "/devices/onboard", icon: "devices" }]}`, abre «New» y espera `menuitem` «Computer» con `href` `/devices/onboard`; «carries a visible label» y «keeps its name where the layout drops the label» pasan al botón «New» (la clase `.new-menu-label` se oculta visualmente bajo 640 px, nunca con `display: none`: se comprueba en `shell.css` igual que hoy se comprobaba `.top-enrol-label`); «is gated on the technician permission» comprueba que sin entradas no hay botón «New»; «is drawn in the primary voice» comprueba la clase `primary` en «New»; «does not push the chrome off the side of a phone» sigue igual sobre `.new-menu-btn`.
3. **El Asistente** (decisión D12, la respuesta del operador del 2026-11-24). «carries the assistant in the chrome and no documentation link at all» pasa a:
   ```tsx
     it("carries the Assistant as the round button and at the rail's foot, not in the bar, and no documentation link at all", async () => {
       // THE OPERATOR, 2026-11-24 ("Como queda en las capturas de atera",
       // docs/design/referencias-atera/atera 1.png): the Assistant leaves the
       // top bar for the rail's foot and a round button at the bottom right of
       // every page. This replaces the ruling of 2026-09-08 that put it in the
       // bar; its other half stands -- on 2026-09-09 the operator removed the
       // documentation link at every width.
       //
       // ASSERTED WITH `docs.read` GRANTED, which is the whole point: an operator
       // who MAY read documentation still gets no top-bar link.
       renderShell(["ai.use", "docs.read"]);
       const bar = document.querySelector("header.topbar") as HTMLElement;
       expect(bar).not.toBeNull();
       expect(within(bar).queryByRole("link", { name: "Ask the Assistant" })).toBeNull();
       const fab = await screen.findByRole("link", { name: "Ask the Assistant" });
       expect(fab).toHaveAttribute("href", "/assistant");
       expect(fab).toHaveClass("assistant-fab");
       expect(bar.contains(fab)).toBe(false);
       const anchored = screen.getByRole("navigation", { name: "Assistant and Admin" });
       expect(within(anchored).getByRole("link", { name: "Assistant" })).toHaveAttribute("href", "/assistant");
       expect(within(bar).queryByRole("link", { name: "IT documentation" })).toBeNull();
     });

     it("does not draw the round button on the Assistant's own page", () => {
       render(
         <MemoryRouter initialEntries={["/assistant"]}>
           <AppShell permissions={["ai.use"]} orgName="Operations" title="Assistant" user={{ name: "A B", role: "Operator" }} onSignOut={vi.fn()}>
             <p>x</p>
           </AppShell>
         </MemoryRouter>,
       );
       expect(screen.queryByRole("link", { name: "Ask the Assistant" })).toBeNull();
       expect(document.body).not.toHaveClass("assistant-fab-on");
     });

     it("tells the save bar to keep its corner clear while the round button is drawn", () => {
       // Atera's walkthrough files its own AI button covering the save button
       // (Atera_recorrido_menus_UI_UX.md, line 639). `shell.css` keys the save
       // bar's room off this class; responsive.test.ts checks the rule.
       const { unmount } = renderShell(["ai.use"]);
       expect(document.body).toHaveClass("assistant-fab-on");
       unmount();
       expect(document.body).not.toHaveClass("assistant-fab-on");
     });
   ```
   «never offers the assistant to an operator who cannot reach it» gana, al final, `expect(screen.queryByRole("link", { name: "Ask the Assistant" })).toBeNull();` y `expect(document.body).not.toHaveClass("assistant-fab-on");`: sin `ai.use`, ni la fila de la búsqueda ni el botón.

`frontend/src/test/responsive.test.ts:183-203`, el caso «has nothing left to stand down, because the docs link is gone at every width»: sus dos últimas aserciones (`toContain("shell.app.askAssistant")` y `en.shell.app.askAssistant` igual a «Ask the Assistant») **siguen siendo ciertas**, porque el botón flotante usa esa misma clave como nombre. Cambia su comentario y se le añaden dos líneas que fijan que ya no está en la barra:

```ts
    // The link's name is a catalogue entry; the shortcut survives where the
    // shell still renders that key and the English entry still says it. It
    // is the round button at the bottom right now (decision D12, the operator
    // on 2026-11-24), drawn after `</main>` and never inside the top bar.
    const header = shellTsx.slice(shellTsx.indexOf('<header className="topbar">'), shellTsx.indexOf("</header>"));
    expect(header, "the top bar draws the Assistant again").not.toContain("shell.app.askAssistant");
    expect(shellTsx).toContain('className="assistant-fab"');
    expect(shellTsx, "the assistant shortcut itself must survive").toContain("shell.app.askAssistant");
    expect(en.shell.app.askAssistant).toBe("Ask the Assistant");
```

y un `describe` nuevo al final del archivo (`SHELL` y `SHELL_BLOCKS` son los que el archivo ya define):

```ts
/**
 * THE ASSISTANT'S ROUND BUTTON (decision D12; DESIGN.md v4, "Assistant
 * button"). jsdom lays nothing out, so this is asserted on the declarations:
 * a 48px target, fixed in the corner, and the save bar's room beside it at
 * every width. The defect it guards is Atera's own (its walkthrough, line
 * 639: the floating AI button covers the save button).
 */
describe("the Assistant's round button", () => {
  it("is a fixed 48px circle, over the 44px target on every pointer", () => {
    const fab = /\.assistant-fab\s*\{([^}]*)\}/.exec(SHELL)?.[1] ?? "";
    expect(fab, "no `.assistant-fab` rule in shell.css").not.toBe("");
    expect(fab).toMatch(/position:\s*fixed/);
    expect(fab).toMatch(/width:\s*48px/);
    expect(fab).toMatch(/height:\s*48px/);
  });

  it("keeps the save bar out of its corner on a desk and on a phone", () => {
    expect(SHELL).toMatch(/body\.assistant-fab-on \.savebar\s*\{\s*margin-right:\s*72px;/);
    expect(SHELL_BLOCKS.get(640) ?? "").toMatch(/body\.assistant-fab-on \.savebar\s*\{\s*margin-right:\s*64px;/);
    expect(SHELL).toMatch(/body\.assistant-fab-on \.content\s*\{\s*padding-bottom:\s*88px;/);
  });
});
```

`frontend/src/shell/shell.es.test.tsx`: las etiquetas españolas del riel que espera pasan a «Inicio», «Equipos», «Documentación»; añadir que «Activos», «Más», «Nuevo» y «Volver al menú» se pintan en español, y que el botón flotante se llama «Preguntar al Asistente» (`shell.app.askAssistant`, `i18n/es/shell.ts:123`, sin cambios).

- [ ] **Step 11: Ejecutar todo**

Run: `cd frontend && npx vitest run src/shell src/test src/i18n src/App.shell.test.tsx src/App.crumb.test.tsx src/App.routing.test.tsx src/modules/clients src/modules/service-desk src/modules/docs src/modules/change src/modules/deploy/deploy.es.test.tsx src/modules/backup/backup.es.test.tsx src/modules/remote src/modules/agent-releases/agent-releases.es.test.tsx`
Expected: PASS.

Run: `npx vitest run && npm run typecheck && npm run build`
Expected: verde.

Run: `node scripts/harmony.mjs --strict-fixtures --lang es` y `--lang en` (todas las rutas: el armazón está en todas)
Expected: `ok …`. Mirar en las capturas: `/dashboard` (riel en el orden nuevo, «Nuevo» a la izquierda de la búsqueda, ningún icono del Asistente en la barra y su botón redondo abajo a la derecha), `/licenses` (la carpeta «Activos» abierta), `/audit` (el panel de Administración con «Seguridad» abierta), `/admin` (las tarjetas), `/assistant` (sin botón redondo), `/settings?tab=email` (el botón redondo no tapa la barra de guardado cuando la hay: en la captura de reposo no la hay, así que esto lo fija `responsive.test.ts`), y las de 390x844 (el botón «+» sin texto visible y el botón redondo a 16 px de las esquinas).

- [ ] **Step 12: Crítica y pulido de impeccable sobre el armazón**

Aplicar «Cierre de cada oleada», pasos 1 a 5, a `frontend/src/shell/Sidebar.tsx`, `frontend/src/shell/AppShell.tsx` y `frontend/src/shell/AreaPage.tsx`, con las rutas `/dashboard`, `/licenses`, `/audit`, `/admin`, `/assistant`. Sus preguntas las contesta el coordinador con `DESIGN.md`, el diseño aprobado y las decisiones del operador (resolución del coordinador del 2026-11-24), y esas respuestas se añaden a `.impeccable/critica-respuestas.md` (versionado; lo inauguró la Task 3); solo lo que esas fuentes no zanjan sube al operador.

- [ ] **Step 13: Commit**

```bash
git add frontend/src .impeccable/critique .impeccable/critica-respuestas.md
git commit -m "feat(ui): Atera-order rail with folders, Admin panel, New menu, the Assistant's round button and the restyled top bar

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación:** el riel está en el orden del diseño §1.1 sin títulos de grupo, plegado a 76 px; «Más» desaparece sin módulos opcionales; Administración se abre como panel con volver, búsqueda y acordeón; «Nuevo» solo enseña destinos con permiso y cada uno abre lo que promete; el Asistente no está en la barra, está anclado al pie del riel y es un botón redondo de 48 px abajo a la derecha en toda página salvo `/assistant`, con `ai.use`, y la barra de guardado le deja su esquina; la búsqueda llega a 640 px; ningún texto español nombra «Dispositivos →»; el selector de cliente se comporta igual (sus pruebas, sin cambios, en verde); harmony en verde en las 117 rutas de `routes.mjs` en los dos idiomas.

---

### Task 5: Clientes: la lista y la ficha 360 con pestañas en la URL

Diseño §1.3 («La ficha 360 de un cliente reúne sus datos en pestañas con URL propia: Resumen, Sedes, Contactos, Equipos, Tickets, Alertas, Activos y Módulos»; «Las pestañas son de una línea»). Es la primera página construida con el kit, y sirve de modelo a las oleadas.

**Files:**
- Modify: `frontend/src/modules/clients/ClientsPage.tsx` (receta L)
- Modify: `frontend/src/modules/clients/ClientDetailPage.tsx` (receta F, archivo completo)
- Modify: `frontend/src/modules/clients/SummaryTab.tsx` (las acciones suben a la cabecera)
- Create: `frontend/src/modules/clients/ClientActions.tsx`, `DevicesTab.tsx`, `TicketsTab.tsx`, `AlertsTab.tsx`, `AssetsTab.tsx`
- Modify: `frontend/src/i18n/en/clients.ts`, `frontend/src/i18n/es/clients.ts`, `frontend/src/i18n/identical-ok.ts` (`IDENTICAL_OK_SHARED`)
- Modify (el parámetro del recorrido, Step 3b): `frontend/src/modules/onboarding/journey.ts:45-47` y las pruebas que lo escriben a mano: `modules/onboarding/__tests__/journeyTarget.test.tsx`, `modules/onboarding/__tests__/journey.test.ts`, `modules/onboarding/OnboardPage.test.tsx`, `modules/ipam/__tests__/clients.test.tsx`, `modules/enrollment/clientSelection.test.tsx`, `modules/agent-releases/clientSelection.test.tsx`
- Modify: `frontend/scripts/routes.mjs` (variantes de pestaña que la puerta de armonía debe recorrer: las de esta tarea y las que las oleadas necesitan, Step 8)
- Test: `frontend/src/modules/clients/__tests__/clients.test.tsx` (casos que cambian), `frontend/src/modules/clients/__tests__/client360.test.tsx` (nuevo), `frontend/src/modules/clients/clients.es.test.tsx`

**Interfaces:**
- Consumes: `DetailHead`, `useUrlTab`, `UrlTabs`, `useApiFor`, `useUrlFilters`, `FilterChips`, `ListToolbar`, `ShowingCount`, `ListEmpty`, `useNewParam` (Task 3); `useRecordTitle` (Task 2); `StatsGrid`, `StatCard` (`shell/components`); `api.listDevices({ clientId, limit })`, `api.listTickets({ client_id, scope, limit })`, `api.getHealthAlertSummary()`, `api.listHealthAlerts({ state: "active" })` (`AlertFilters.state` es `AlertState | ""`, `modules/health/types.ts:27,315`: «open» no se puede pedir a la lista), `api.listLicenses()`, `api.listSubscriptions()`, `api.listIpamSubnets()` (todas existen hoy); `useNarrowing` (Task 3), `renderPage` con `options.page` y `recordingNarrowing` (Task 3) en las pruebas.
- Produces: las rutas de la ficha no cambian (`/clients/:clientId`); sus pestañas son `?tab=summary|sites|contacts|devices|tickets|alerts|assets|modules` (`summary`, `sites`, `contacts` y `modules` ya existían con esos nombres). Las oleadas enlazan a una lista filtrada por cliente con `?client=<id>`.

**Decisiones de esta tarea:**
- **D14.** La pestaña Activos de la ficha enseña tres tarjetas, licencias, suscripciones y redes (los activos del riel, diseño §1.1), con su número para este cliente y un enlace a la lista con el chip «Cliente» puesto (resolución del coordinador del 2026-11-24: «three count tiles linking to filtered lists»). No es el inventario de activos de Atera (ese no existe aquí); y una tabla de licencias necesitaría resolver productos y proveedores que la página de licencias ya resuelve.
- **D19.** La pestaña Alertas lista **las alertas activas** del cliente y dice, con el conteo del servidor, cuántas hay abiertas en total (resolución del coordinador del 2026-11-24). La lista de alertas toma un solo `state`, y «abierta» (activa, reconocida o silenciada) no se puede pedir (`types.ts`, `AlertSummary`); pedir sin `state` y quitar en el navegador las cerradas haría de una página de 200 un total que no lo es.
- **D20. `?client=` significa «estrecha esta lista» en toda la consola** (resolución del coordinador del 2026-11-24, revisión previa W-I5). Hoy el recorrido de «Añadir un equipo» usa `?client=` para **cambiar el cliente de toda la consola** (`JOURNEY_CLIENT_PARAM = "client"`, `onboarding/journey.ts:46`; `useAdoptJourneyClient` llama a `select()` en `EnrollmentPage`, `SubnetTreePage`, `ScanAuthorizationsPage`, `AgentReleasesPage`, `OnboardPage` y `CandidatesPage`), y el chip «Cliente» usa la misma clave para estrechar una lista. El recorrido pasa a `journey_client` (y su sede a `journey_site`, para dejar `site` a los filtros de IPAM y de Equipos). Nadie más escribe ese parámetro: ni el backend ni un correo (`grep` del 2026-11-24: lo producen solo `journeyQuery` y `withJourney`, y los dos leen la constante).

- [ ] **Step 1: Las pruebas de la ficha 360 (fallan)**

`frontend/src/modules/clients/__tests__/client360.test.tsx`:

```tsx
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { fixtureClient } from "../../../fixtures/client";
import { recordingNarrowing } from "../../../test/narrowing";
import { renderPage } from "../../../test/renderPage";
import { ClientDetailPage } from "../ClientDetailPage";

const ANDES = "5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57"; // src/fixtures/core.ts CUSTOMER_ID

const mockUseApi = vi.fn();
vi.mock("../../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));

function page(query = "") {
  return renderPage(`/clients/${ANDES}${query}`, <ClientDetailPage />, "/clients/:clientId");
}

beforeEach(() => mockUseApi.mockReturnValue(fixtureClient()));

describe("the client 360 (redesign design §1.3)", () => {
  it("names the client, its status and its kind, and leads back to the list", async () => {
    page();
    expect(await screen.findByRole("heading", { level: 1, name: "Andes Logistics" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All clients" })).toHaveAttribute("href", "/clients");
  });

  it("names the client in the browser tab", async () => {
    renderPage(`/clients/${ANDES}`, <ClientDetailPage />, "/clients/:clientId", { page: "shell.nav.item.clients" });
    await screen.findByRole("heading", { level: 1, name: "Andes Logistics" });
    expect(document.title).toBe("Andes Logistics · Clients · Novieri RMM");
  });

  it("has the eight tabs of the design, in one row, for someone who may read all of it", async () => {
    page();
    const tabs = await screen.findByRole("tablist", { name: "Client" });
    expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "Summary",
      "Sites",
      "Contacts",
      "Computers",
      "Tickets",
      "Alerts",
      "Assets",
      "Modules",
    ]);
    expect(tabs).toHaveClass("scroll");
  });

  it("puts the tab in the URL, so a tab reloads and links", async () => {
    const { router } = page("?tab=alerts");
    expect(await screen.findByRole("tab", { name: "Alerts" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Computers" }));
    expect(router.state.location.search).toBe("?tab=devices");
  });

  it("lists the client's computers and leads to the fleet list narrowed to it", async () => {
    page("?tab=devices");
    const link = await screen.findByRole("link", { name: /See every computer of this client/ });
    expect(link).toHaveAttribute("href", `/devices?client=${ANDES}`);
  });

  it("counts what is open for the client in the summary tiles, each leading to its tab", async () => {
    page();
    expect(await screen.findByRole("link", { name: /Open tickets/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=tickets`);
    expect(screen.getByRole("link", { name: /Open alerts/ })).toHaveAttribute("href", `/clients/${ANDES}?tab=alerts`);
  });

  it("reads the alerts tab narrowed to the client, the active ones, and leads to the alert list narrowed to it (D19)", async () => {
    const { api, narrowedTo } = recordingNarrowing(fixtureClient());
    mockUseApi.mockReturnValue(api);
    page("?tab=alerts");
    expect(await screen.findByRole("heading", { level: 2, name: "Active alerts" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See this client's alerts" })).toHaveAttribute("href", `/health?client=${ANDES}`);
    expect(narrowedTo).toContain(ANDES);
  });

  it("shows the assets as three counts, each leading to its list with the Client chip set (D14)", async () => {
    page("?tab=assets");
    expect(await screen.findByRole("link", { name: /Licenses/ })).toHaveAttribute("href", `/licenses?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Subscriptions/ })).toHaveAttribute("href", `/subscriptions?client=${ANDES}`);
    expect(screen.getByRole("link", { name: /Networks/ })).toHaveAttribute("href", `/ipam?client=${ANDES}`);
  });
});
```

En `frontend/src/modules/clients/__tests__/clients.test.tsx` (medido el 2026-11-24: sus casos de «Edit» y «Archive client» ya buscan con `screen`, no dentro del panel, así que el traslado de las acciones a la cabecera no los cambia; y ninguno mira la tira de pestañas entera):
- «reads archived clients only when asked to» (línea 234): el interruptor «Show archived clients» desaparece (lo sustituye el chip); la línea 241, `await user.click(screen.getByRole("switch", { name: "Show archived clients" }));`, pasa a `await user.click(screen.getByRole("button", { name: "Including archived" }));`, con el comentario `// Redesign design §2, Receta L: the archived filter is a chip now; the URL (?archived=1) and the read it makes are the same.` La aserción no cambia.
- Un caso nuevo de lista, con `import { renderPage } from "../../../test/renderPage";` entre las importaciones (el ayudante de la API del archivo se llama `api`):
  ```tsx
    it("keeps the archived filter in the URL as a chip, and says why the list is empty", async () => {
      mockUseApi.mockReturnValue(api({ listClients: vi.fn().mockResolvedValue([]) }));
      const user = userEvent.setup();
      const { router } = renderPage("/clients?archived=1", <ClientsPage />);
      expect(await screen.findByRole("button", { name: /Including archived/ })).toHaveAttribute("aria-pressed", "true");
      expect(screen.getByText("Nothing matches these filters.")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Clear filters" }));
      expect(router.state.location.search).toBe("");
    });
  ```

En `frontend/src/modules/clients/clients.es.test.tsx:99`, `expect(screen.getByRole("switch", { name: "Mostrar los clientes archivados" })).toBeInTheDocument();` pasa a `expect(screen.getByRole("button", { name: "Con archivados" })).toBeInTheDocument();` (el mismo filtro, ahora chip).

- [ ] **Step 2: Ejecutarlas y verlas fallar**

Run: `cd frontend && npx vitest run src/modules/clients`
Expected: FAIL (no hay pestañas Computers, Tickets, Alerts ni Assets; la tira no tiene nombre; no hay chips).

- [ ] **Step 3: Las acciones del cliente, en la cabecera**

Se mueven de `SummaryTab.tsx` a un componente propio, sin cambiar ninguna condición ni ninguna llamada: las mismas dos comprobaciones, la misma mutación, los mismos dos diálogos y el mismo párrafo de error.

`frontend/src/modules/clients/ClientActions.tsx`:

```tsx
/**
 * A client's two acts, in the head of its 360 page (redesign design §2,
 * "Ficha": the record's actions on the right): its edit (`clients.manage` ON
 * THIS CLIENT, from `/me.clients`) and its archive (`clients.manage` ON EVERY
 * CLIENT, a global route; never offered for the internal or the unassigned
 * client). Moved here from the summary tab's Details panel unchanged.
 *
 * AN ARCHIVE REFUSED IS EXPLAINED. The server's 409 says why in the person's
 * language -- "this client has 3 active devices; retire them before archiving
 * it" -- and the page puts it under the buttons, after a sentence saying the
 * archive did not happen.
 */
import { useState } from "react";

import { ConfirmDialog } from "../../components/confirm";
import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import { Btn } from "../../shell/components";
import { useArchiveClientMutation } from "./api";
import { ClientFormDialog } from "./ClientForm";

export function ClientActions({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const everywhere = useEverywherePermissions();
  const mine = clients.find((item) => item.id === client.id);
  const canEdit = client.status === "active" && (mine?.permissions.includes("clients.manage") ?? false);
  const canArchive =
    client.kind === "customer" && client.status === "active" && everywhere.has("clients.manage");
  const archive = useArchiveClientMutation(client.id);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  if (!canEdit && !canArchive) return null;
  return (
    <>
      {canEdit ? <Btn label={t("clients.summary.edit")} icon="edit" onClick={() => setEditing(true)} /> : null}
      {canArchive ? (
        <Btn
          label={t("clients.summary.archive")}
          icon="archive"
          variant="danger"
          onClick={() => {
            archive.reset();
            setConfirming(true);
          }}
        />
      ) : null}
      {archive.isError ? (
        <p role="alert" className="form-error">
          {t("clients.summary.archiveFailed")} {describeSubmitError(archive.error)}
        </p>
      ) : null}
      <ClientFormDialog
        key={editing ? "open" : "closed"}
        open={editing}
        client={client}
        onClose={() => setEditing(false)}
        onSaved={() => setEditing(false)}
      />
      <ConfirmDialog
        open={confirming}
        title={t("clients.summary.archiveTitle")}
        subject={client.name}
        body={t("clients.summary.archiveBody")}
        confirmLabel={t("clients.summary.archiveConfirm")}
        destructive
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          archive.mutate();
        }}
      />
    </>
  );
}
```

`frontend/src/modules/clients/SummaryTab.tsx`: el comentario de cabecera (líneas 1-11) pasa a decir solo «A client's details and the health profile its devices fall back to (design §5.1), read-only. Its edit and its archive are in the page's head (`ClientActions.tsx`, redesign design §2).»; las importaciones se quedan en `type ReactNode` de `react`, `Link`, `NoValue`, `useT`, `type ClientOut`, `useClientScope`, `Panel`, `QueryState`, `KV`, `useHealthAssignments` y `clientHealthDefault, clientKindLabel, clientStatusLabel` de `./labels`; y `SummaryTab` pasa a:

```tsx
export function SummaryTab({ client }: { client: ClientOut }) {
  const t = useT();
  const { clients } = useClientScope();
  const mine = clients.find((item) => item.id === client.id);
  const canReadHealth = mine?.permissions.includes("health.read") ?? false;
  const notSet = <NoValue label={t("clients.summary.notSet")} />;
  // Typed as tuples: an array literal of pairs would be inferred as arrays,
  // which `KV`'s `readonly [ReactNode, ReactNode]` does not accept.
  const facts: (readonly [string, ReactNode])[] = [
    [t("clients.summary.kind"), clientKindLabel(client.kind)],
    [t("clients.summary.status"), clientStatusLabel(client.status)],
    [t("clients.summary.emailDomains"), client.email_domains.length > 0 ? client.email_domains.join(", ") : notSet],
    [t("clients.summary.phone"), client.phone || notSet],
    [t("clients.summary.address"), client.address || notSet],
    [t("clients.summary.website"), client.website || notSet],
    [t("clients.summary.notes"), client.notes || notSet],
    [t("clients.summary.devices"), String(client.device_count)],
    [t("clients.summary.sites"), String(client.site_count)],
  ];

  return (
    <>
      <Panel title={t("clients.summary.title")}>
        <KV items={facts} />
      </Panel>
      {canReadHealth ? <HealthDefault clientId={client.id} /> : null}
    </>
  );
}
```

(`HealthDefault`, debajo, no cambia: su enlace `/health#profiles` lo lleva la O3 a la pestaña de perfiles, decisión D21.)

- [ ] **Step 3b: El recorrido de «Añadir un equipo» deja libre `?client=`** (decisión D20)

`frontend/src/modules/onboarding/journey.ts:45-47`:

```ts
/**
 * The URL parameters that carry the journey's client and site (design §4.4).
 * NOT `client` and `site` (redesign decision D20, 2026-11-24): across the
 * console the `client` query key narrows a list to one client (the Client
 * chip) and `site` is a list's site filter, while these two switch the
 * console's client and seed a page's site. One key, one meaning.
 */
export const JOURNEY_CLIENT_PARAM = "journey_client";
export const JOURNEY_SITE_PARAM = "journey_site";
```

Los productores (`journeyQuery`, `withJourney`, los pasos de `journeySteps` y los enlaces de `OnboardPage.tsx:234,238`) y el lector (`journeyTarget.ts`) leen las dos constantes, así que su código no cambia. Las pruebas que escriben el parámetro a mano, y los comentarios que lo nombran, sí; cambia el literal, no lo que afirman. Una sola sustitución, comprobada en una copia de trabajo el 2026-11-24 (todas las apariciones de `client=` y `&site=` de estos ocho archivos son del recorrido):

```bash
cd frontend
sed -i -E 's/([`"?&])client=/\1journey_client=/g; s/&site=/\&journey_site=/g' \
  src/modules/onboarding/journey.ts \
  src/modules/onboarding/journeyTarget.ts \
  src/modules/onboarding/__tests__/journeyTarget.test.tsx \
  src/modules/onboarding/__tests__/journey.test.ts \
  src/modules/onboarding/OnboardPage.test.tsx \
  src/modules/ipam/__tests__/clients.test.tsx \
  src/modules/enrollment/clientSelection.test.tsx \
  src/modules/agent-releases/clientSelection.test.tsx
grep -rn '[`"?&]client=\|&site=' src/modules/onboarding src/modules/ipam/__tests__/clients.test.tsx src/modules/enrollment/clientSelection.test.tsx src/modules/agent-releases/clientSelection.test.tsx | grep -v journey_
```

Expected: la última orden no imprime nada. `journey.test.ts:336` queda `/\?journey_client=c-andes&journey_site=s-main$/` y `:347-351` esperan `"journey_client=c+1&journey_site=s-main"`, `"journey_client=c1"`, `"/ipam?journey_client=c1&journey_site=s1"`, `"/x?a=1&journey_client=c1"` y `"/health?journey_client=c1#profiles"`.

Run: `cd frontend && npx vitest run src/modules/onboarding src/modules/ipam/__tests__/clients.test.tsx src/modules/enrollment/clientSelection.test.tsx src/modules/agent-releases/clientSelection.test.tsx`
Expected: PASS (7 archivos, 96 pruebas al 2026-11-24).

- [ ] **Step 4: Las cuatro pestañas nuevas**

`frontend/src/modules/clients/DevicesTab.tsx`:

```tsx
/**
 * The client's computers (design §1.3), the first 25 by the fleet list's own
 * order, and the way to all of them: the fleet list with its Client chip set.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { NoValue } from "../../components/emptystate";
import { RelTime } from "../../components/reltime";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { ListEmpty, Panel, QueryState, StatusPill, useApiFor } from "../../shell/components";
import { OsIcon } from "../../shell/viz";

export function DevicesTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const api = useApiFor(client.id);
  const devices = useQuery({
    queryKey: ["client360", client.id, "devices"],
    queryFn: () => api.listDevices({ clientId: client.id, limit: 25 }),
    enabled,
  });
  const rows = devices.data?.items ?? [];
  return (
    <Panel
      title={t("clients.devices.title")}
      bare
      actions={
        <Link className="btn small" to={`/devices?client=${client.id}`}>
          {t("clients.devices.seeAll", { count: devices.data?.total ?? 0 })}
        </Link>
      }
    >
      <QueryState loading={!enabled || devices.isLoading} error={devices.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty
            filtered={false}
            onClear={() => {}}
            nothingYet={t("clients.devices.empty")}
            icon="devices"
            action={
              <Link className="btn small" to="/devices/onboard">
                {t("clients.devices.add")}
              </Link>
            }
          />
        ) : (
          <Table label={t("clients.devices.title")}>
            <thead>
              <tr>
                <Th>{t("clients.devices.colComputer")}</Th>
                <Th>{t("clients.devices.colAgent")}</Th>
                <Th>{t("clients.devices.colLastSeen")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((device) => (
                <tr key={device.id}>
                  <Td lead={<OsIcon os={device.os_family ?? ""} />}>
                    <div className="cell-title">
                      <Link to={`/devices/${device.id}`}>{device.hostname ?? device.fqdn ?? device.id}</Link>
                    </div>
                    <div className="cell-sub">{device.os_version}</div>
                  </Td>
                  <Td>{device.agent_status ? <StatusPill value={device.agent_status} /> : <NoValue label={t("clients.devices.noAgent")} />}</Td>
                  <Td>{device.last_seen_at ? <RelTime iso={device.last_seen_at} /> : <NoValue label={t("clients.devices.never")} />}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
    </Panel>
  );
}
```

`frontend/src/modules/clients/TicketsTab.tsx`:

```tsx
/**
 * The client's open tickets (design §1.3), the first 25 by the queue's own
 * order, and the way to all of them: the ticket list with its Client chip set.
 * `client_id` narrows the read on the server; `useApiFor` narrows it again with
 * `X-Client-Id` (decision D6), and `enabled` waits until that is decided.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { useClientScope } from "../../lib/clientScope";
import { ListEmpty, Panel, QueryState, StatusPill, useApiFor } from "../../shell/components";

export function TicketsTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const api = useApiFor(client.id);
  const { clients } = useClientScope();
  // TicketListPage's own create gate (`ticket.write`), in THIS client. Cosmetic (invariant 6).
  const canWrite = clients.find((item) => item.id === client.id)?.permissions.includes("ticket.write") ?? false;
  const tickets = useQuery({
    queryKey: ["client360", client.id, "tickets"],
    queryFn: () => api.listTickets({ client_id: client.id, scope: "open", limit: 25 }),
    enabled,
  });
  const rows = tickets.data?.items ?? [];
  return (
    <Panel
      title={t("clients.tickets.title")}
      bare
      actions={
        <Link className="btn small" to={`/service-desk?client=${client.id}`}>
          {t("clients.tickets.seeAll")}
        </Link>
      }
    >
      <QueryState loading={!enabled || tickets.isLoading} error={tickets.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty
            filtered={false}
            onClear={() => {}}
            nothingYet={t("clients.tickets.empty")}
            icon="ticket"
            action={
              canWrite ? (
                <Link className="btn small" to="/service-desk?new=ticket">
                  {t("clients.tickets.add")}
                </Link>
              ) : undefined
            }
          />
        ) : (
          <Table label={t("clients.tickets.title")}>
            <thead>
              <tr>
                <Th>{t("clients.tickets.colTicket")}</Th>
                <Th>{t("clients.tickets.colPriority")}</Th>
                <Th>{t("clients.tickets.colStatus")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ticket) => (
                <tr key={ticket.id}>
                  <Td>
                    <div className="cell-title">
                      <Link to={`/service-desk/tickets/${ticket.id}`}>{ticket.subject}</Link>
                    </div>
                    <div className="cell-sub mono">{ticket.number}</div>
                  </Td>
                  <Td>
                    <StatusPill value={ticket.priority} />
                  </Td>
                  <Td>
                    <StatusPill value={ticket.status} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
    </Panel>
  );
}
```

`frontend/src/modules/clients/AlertsTab.tsx` (decisión D19):

```tsx
/**
 * The client's ACTIVE alerts (design §1.3; decision D19). The alert list takes
 * one `state`, and "open" (active, acknowledged or muted) cannot be asked of
 * it (`modules/health/types.ts`, `AlertSummary`), so this lists the active
 * ones and says how many are open in all from the server's own count -- never
 * a total made from one page. Every read is this client's: `useApiFor` narrows
 * it with `X-Client-Id` (decision D6), and `enabled` waits until that is decided.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { RelTime } from "../../components/reltime";
import { Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { ListEmpty, Panel, QueryState, StatusPill, useApiFor } from "../../shell/components";

export function AlertsTab({ client, enabled }: { client: ClientOut; enabled: boolean }) {
  const t = useT();
  const api = useApiFor(client.id);
  const alerts = useQuery({
    queryKey: ["client360", client.id, "alerts"],
    queryFn: () => api.listHealthAlerts({ state: "active" }),
    enabled,
  });
  // The same key as the 360's "Open alerts" tile, so the two read once.
  const summary = useQuery({
    queryKey: ["client360", client.id, "alertSummary"],
    queryFn: () => api.getHealthAlertSummary(),
    enabled,
  });
  const rows = alerts.data ?? [];
  return (
    <Panel
      title={t("clients.alerts.title")}
      subtitle={summary.data ? t("clients.alerts.openInAll", { count: summary.data.open }) : undefined}
      bare
      actions={
        <Link className="btn small" to={`/health?client=${client.id}`}>
          {t("clients.alerts.seeAll")}
        </Link>
      }
    >
      <QueryState loading={!enabled || alerts.isLoading} error={alerts.error} empty={false} emptyLabel="">
        {rows.length === 0 ? (
          <ListEmpty filtered={false} onClear={() => {}} nothingYet={t("clients.alerts.empty")} icon="health" />
        ) : (
          <Table label={t("clients.alerts.title")}>
            <thead>
              <tr>
                <Th>{t("clients.alerts.colComputer")}</Th>
                <Th>{t("clients.alerts.colCheck")}</Th>
                <Th>{t("clients.alerts.colSeverity")}</Th>
                <Th>{t("clients.alerts.colOpened")}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((alert) => (
                <tr key={alert.id}>
                  <Td>
                    <Link to={`/devices/${alert.device_id}?tab=health`}>{alert.device_hostname}</Link>
                  </Td>
                  <Td>
                    <div className="cell-title">{alert.check_type}</div>
                    <div className="cell-sub mono">{alert.observed_value}</div>
                  </Td>
                  <Td>
                    <StatusPill value={alert.severity} />
                  </Td>
                  <Td>
                    <RelTime iso={alert.opened_at} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </QueryState>
    </Panel>
  );
}
```

`frontend/src/modules/clients/AssetsTab.tsx` (decisión D14):

```tsx
/** The client's assets, as the rail names them (design §1.1): licenses, subscriptions, networks. */
import { useQuery } from "@tanstack/react-query";

import { useT } from "../../i18n/context";
import type { ClientOut } from "../../lib/api";
import { StatCard, StatsGrid, useApiFor } from "../../shell/components";

export function AssetsTab({ client, codes, enabled }: { client: ClientOut; codes: ReadonlySet<string>; enabled: boolean }) {
  const t = useT();
  const api = useApiFor(client.id);
  const licenses = useQuery({
    queryKey: ["client360", client.id, "licenses"],
    queryFn: () => api.listLicenses(),
    enabled: enabled && codes.has("licenses.read"),
  });
  const subscriptions = useQuery({
    queryKey: ["client360", client.id, "subscriptions"],
    queryFn: () => api.listSubscriptions(),
    enabled: enabled && codes.has("subscriptions.read"),
  });
  const subnets = useQuery({
    queryKey: ["client360", client.id, "subnets"],
    queryFn: () => api.listIpamSubnets(),
    enabled: enabled && codes.has("ipam.read"),
  });
  return (
    <StatsGrid columns={3}>
      {codes.has("licenses.read") ? (
        <StatCard label={t("clients.assets.licenses")} value={String(licenses.data?.length ?? 0)} pending={licenses.data === undefined} to={`/licenses?client=${client.id}`} icon="license" />
      ) : null}
      {codes.has("subscriptions.read") ? (
        <StatCard label={t("clients.assets.subscriptions")} value={String(subscriptions.data?.length ?? 0)} pending={subscriptions.data === undefined} to={`/subscriptions?client=${client.id}`} icon="subscription" />
      ) : null}
      {codes.has("ipam.read") ? (
        <StatCard label={t("clients.assets.networks")} value={String(subnets.data?.length ?? 0)} pending={subnets.data === undefined} to={`/ipam?client=${client.id}`} icon="network" />
      ) : null}
    </StatsGrid>
  );
}
```

(`StatCard` toma `value: string` y `pending`: con `pending` dibuja `n/a`, nunca un `0` inventado mientras carga o si la lectura falla; `shell/components.tsx:256-296`.)

- [ ] **Step 5: La ficha 360**

`frontend/src/modules/clients/ClientDetailPage.tsx`, archivo completo:

```tsx
/**
 * THE CLIENT 360 (redesign design §1.3; Atera's customer page): everything
 * about one client, in eight tabs that change the URL and fit one row --
 * Atera's eleven wrap into two, a flaw its own walkthrough files.
 *
 * EVERY READ IS THIS CLIENT'S (decision D6): `useApiFor` narrows each request
 * to it while every client is shown above; with this client chosen above,
 * the console's own client already is.
 *
 * ANOTHER CLIENT CHOSEN ABOVE: with another client's id in `X-Client-Id`
 * this client does not exist for the request -- a 404 on every tab -- so the
 * page reads nothing and says which client the console is working on, with
 * the one act that fixes it. NOT BEFORE `/me` HAS ANSWERED (phase 3 final
 * review C): until then the page decides nothing.
 *
 * A TAB SHOWS ONLY WITH ITS CODE IN THIS CLIENT. Cosmetic (invariant 6): the
 * server refuses the read either way.
 */
import { useParams } from "react-router-dom";

import { useT } from "../../i18n/context";
import type { MessageKey } from "../../i18n/core";
import { useClientScope } from "../../lib/clientScope";
import { Btn, DetailHead, QueryState, StatCard, StatsGrid, UrlTabs, useApiFor, useNarrowing, useUrlTab } from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useRecordTitle } from "../../shell/title";
import { KIND_ICON } from "../../shell/viz";
import { useQuery } from "@tanstack/react-query";
import { AlertsTab } from "./AlertsTab";
import { useClientRecord } from "./api";
import { AssetsTab } from "./AssetsTab";
import { ClientActions } from "./ClientActions";
import { ContactsTab } from "./ContactsTab";
import { DevicesTab } from "./DevicesTab";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";
import { ModulesTab } from "./ModulesTab";
import { SitesTab } from "./SitesTab";
import { SummaryTab } from "./SummaryTab";
import { TicketsTab } from "./TicketsTab";

const TABS = ["summary", "sites", "contacts", "devices", "tickets", "alerts", "assets", "modules"] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, MessageKey> = {
  summary: "clients.detail.tabs.summary",
  sites: "clients.detail.tabs.sites",
  contacts: "clients.detail.tabs.contacts",
  devices: "clients.detail.tabs.devices",
  tickets: "clients.detail.tabs.tickets",
  alerts: "clients.detail.tabs.alerts",
  assets: "clients.detail.tabs.assets",
  modules: "clients.detail.tabs.modules",
};

/** The codes a tab needs IN THIS CLIENT (any one of them); `null`: whoever can open the client. */
const TAB_CODES: Record<Tab, readonly string[] | null> = {
  summary: null,
  sites: null,
  contacts: null,
  devices: ["devices.read"],
  tickets: ["ticket.read"],
  alerts: ["health.read"],
  assets: ["licenses.read", "subscriptions.read", "ipam.read"],
  modules: null,
};

export function ClientDetailPage() {
  const t = useT();
  const { clientId = "" } = useParams();
  const { selected, select, clients, ready } = useClientScope();
  const elsewhere = selected !== null && selected !== clientId;
  const client = useClientRecord(clientId, !elsewhere);
  const codes = new Set(clients.find((item) => item.id === clientId)?.permissions ?? []);
  const visible = TABS.filter((key) => TAB_CODES[key]?.some((code) => codes.has(code)) ?? true);
  const [tab, setTab] = useUrlTab<Tab>(visible, "summary");
  const api = useApiFor(clientId);
  // THE READS ARE THIS CLIENT'S AND NOBODY ELSE'S (decisions D6, D17): narrowed
  // to it with X-Client-Id, or the top bar has chosen it. Nothing before /me
  // has answered, so every client's rows never sit under this client's name.
  const narrowing = useNarrowing(clientId);
  const here = narrowing.ready && (narrowing.client === clientId || selected === clientId);
  const reads = !elsewhere && client.data !== undefined && here;
  const openTickets = useQuery({
    queryKey: ["client360", clientId, "openTickets"],
    queryFn: () => api.listTickets({ client_id: clientId, scope: "open", limit: 1 }),
    enabled: reads && codes.has("ticket.read"),
  });
  const alerts = useQuery({
    queryKey: ["client360", clientId, "alertSummary"],
    queryFn: () => api.getHealthAlertSummary(),
    enabled: reads && codes.has("health.read"),
  });
  useRecordTitle(client.data?.name);
  const back = { to: "/clients", label: t("clients.detail.allClients") };

  if (elsewhere && !ready) {
    return (
      <>
        <DetailHead back={back} title={t("clients.detail.title")} />
        <QueryState loading error={null} empty={false} emptyLabel="">
          {null}
        </QueryState>
      </>
    );
  }

  if (elsewhere) {
    const reachable = clients.some((item) => item.id === clientId);
    return (
      <>
        <DetailHead back={back} title={t("clients.detail.title")} />
        <div role="status" className="notice mb-4">
          <Icon name={KIND_ICON.info} size={17} />
          <div>
            {t("clients.detail.otherSelected", {
              selected: clients.find((item) => item.id === selected)?.name ?? selected,
            })}{" "}
            <Btn
              small
              label={reachable ? t("clients.detail.workOnThis") : t("clients.detail.workOnAll")}
              onClick={() => select(reachable ? clientId : null)}
            />
          </div>
        </div>
      </>
    );
  }

  const data = client.data;
  const here = (key: Tab) => `/clients/${clientId}${key === "summary" ? "" : `?tab=${key}`}`;
  return (
    <>
      <DetailHead
        back={back}
        title={data?.name ?? t("clients.detail.title")}
        status={data ? { kind: clientStatusKind(data.status), label: clientStatusLabel(data.status) } : undefined}
        context={data ? clientKindLabel(data.kind) : undefined}
        actions={data ? <ClientActions client={data} /> : undefined}
      />
      <QueryState loading={client.isLoading} error={client.error} empty={false} emptyLabel="">
        {data ? (
          <>
            {data.status === "archived" ? (
              <div role="status" className="notice mb-4">
                <Icon name={KIND_ICON.info} size={17} />
                <div>{t("clients.detail.archived")}</div>
              </div>
            ) : null}
            <StatsGrid columns={4}>
              {visible.includes("devices") ? (
                <StatCard label={t("clients.detail.tiles.devices")} value={String(data.device_count)} to={here("devices")} icon="devices" />
              ) : null}
              <StatCard label={t("clients.detail.tiles.sites")} value={String(data.site_count)} to={here("sites")} icon="map-pin" />
              {visible.includes("tickets") ? (
                <StatCard label={t("clients.detail.tiles.openTickets")} value={String(openTickets.data?.total ?? 0)} pending={openTickets.data === undefined} to={here("tickets")} icon="ticket" />
              ) : null}
              {visible.includes("alerts") ? (
                <StatCard label={t("clients.detail.tiles.openAlerts")} value={String(alerts.data?.open ?? 0)} pending={alerts.data === undefined} to={here("alerts")} icon="health" />
              ) : null}
            </StatsGrid>
            <UrlTabs
              label={t("clients.detail.title")}
              items={visible.map((key) => ({ key, label: t(TAB_LABEL[key]) }))}
              active={tab}
              onSelect={setTab}
            >
              {tab === "summary" ? (
                <SummaryTab client={data} />
              ) : tab === "sites" ? (
                <SitesTab client={data} />
              ) : tab === "contacts" ? (
                <ContactsTab client={data} />
              ) : tab === "devices" ? (
                <DevicesTab client={data} enabled={reads} />
              ) : tab === "tickets" ? (
                <TicketsTab client={data} enabled={reads} />
              ) : tab === "alerts" ? (
                <AlertsTab client={data} enabled={reads} />
              ) : tab === "assets" ? (
                <AssetsTab client={data} codes={codes} enabled={reads} />
              ) : (
                <ModulesTab client={data} />
              )}
            </UrlTabs>
          </>
        ) : null}
      </QueryState>
    </>
  );
}
```

(Ordenar las importaciones como el resto del módulo: la de `@tanstack/react-query` primero.)

- [ ] **Step 6: La lista de clientes**

`frontend/src/modules/clients/ClientsPage.tsx` aplica la receta L y es el modelo de las oleadas. Archivo completo (incluye lo que la Task 4 le añadió: `useNewParam("client")`, `useNewParam("contact")` y `ContactClientDialog`):

```tsx
/**
 * Clients (MSP clients phase 3, design §4.1): the companies this organisation
 * serves, and its two system clients -- its own IT (`internal`) and
 * `unassigned`, which `GET /clients` lists only to a person with every
 * client. Archived clients are hidden unless asked for.
 *
 * ON THE LIST KIT (redesign design §2, Receta L; the model the waves copy):
 * the search and the archived chip live in the URL (`?q=`, and `?archived=1`,
 * the address it always had), "Showing x of y", an empty list that says its
 * cause, and the two create dialogs open from `?new=` (decision D8) so the New
 * menu links to them.
 *
 * "NEW CLIENT" NEEDS `clients.manage` ON EVERY CLIENT (`POST /clients` is a
 * global route), so it is offered on `permissions_everywhere`. Offering it is
 * a courtesy: the server checks the code on every request (invariant 6).
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { Form } from "../../components/form";
import { Button, Dialog, Field, Table, Td, Th } from "../../components/ui";
import { useT } from "../../i18n/context";
import { useClientScope } from "../../lib/clientScope";
import { useEverywherePermissions } from "../../lib/mePermissions";
import {
  Btn,
  FilterChips,
  ListEmpty,
  ListToolbar,
  PageHead,
  Panel,
  QueryState,
  SearchField,
  ShowingCount,
  StatusPill,
  useNewParam,
  useUrlFilters,
} from "../../shell/components";
import { Icon } from "../../shell/icons";
import { useClientList } from "./api";
import { ClientFormDialog } from "./ClientForm";
import { clientKindLabel, clientStatusKind, clientStatusLabel } from "./labels";

/** For the search: case and accents do not count ("bogota" finds "Bogotá"), the Admin panel's rule. */
function fold(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
}

export function ClientsPage() {
  const t = useT();
  const navigate = useNavigate();
  const filters = useUrlFilters(["q", "archived"] as const);
  const archived = filters.values.archived === "1";
  const everywhere = useEverywherePermissions();
  const canCreate = everywhere.has("clients.manage");
  const clients = useClientList(archived);
  const created = useNewParam("client");
  const contact = useNewParam("contact");
  const all = clients.data ?? [];
  const wanted = fold(filters.values.q);
  // `GET /clients` answers every client at once, so the search filters here,
  // by name and by e-mail domain, and the total is the server's whole answer.
  const rows =
    wanted === ""
      ? all
      : all.filter(
          (client) => fold(client.name).includes(wanted) || client.email_domains.some((domain) => fold(domain).includes(wanted)),
        );
  const newClient = canCreate ? (
    <Btn label={t("clients.list.newClient")} icon="plus" variant="primary" onClick={created.start} />
  ) : undefined;

  return (
    <>
      <PageHead title={t("clients.list.title")} description={t("clients.list.description")} actions={newClient} />
      <Panel bare>
        <ListToolbar
          search={
            <SearchField
              aria-label={t("clients.list.search")}
              placeholder={t("clients.list.search")}
              value={filters.values.q}
              onChange={(event) => filters.set("q", event.target.value)}
            />
          }
          chips={
            <FilterChips
              label={t("clients.list.archivedLabel")}
              options={[
                { value: "", label: t("clients.list.chipActive") },
                { value: "1", label: t("clients.list.chipArchived") },
              ]}
              value={filters.values.archived}
              onChange={(value) => filters.set("archived", value)}
            />
          }
          end={<ShowingCount shown={rows.length} total={all.length} />}
        />
        <QueryState loading={clients.isLoading} error={clients.error} empty={false} emptyLabel="">
          {rows.length === 0 ? (
            <ListEmpty
              filtered={filters.active}
              onClear={filters.clear}
              nothingYet={t("clients.list.emptyYet")}
              icon="building"
              action={newClient}
            />
          ) : (
            <Table label={t("clients.list.tableLabel")}>
              <thead>
                <tr>
                  <Th>{t("clients.list.colClient")}</Th>
                  <Th>{t("clients.list.colKind")}</Th>
                  <Th>{t("clients.list.colDevices")}</Th>
                  <Th>{t("clients.list.colSites")}</Th>
                  <Th>{t("clients.list.colStatus")}</Th>
                </tr>
              </thead>
              <tbody>
                {rows.map((client) => (
                  <tr key={client.id}>
                    <Td lead={<Icon name="building" size={18} />}>
                      <div className="cell-title">
                        <Link to={`/clients/${client.id}`}>{client.name}</Link>
                      </div>
                      {client.email_domains.length > 0 ? (
                        <div className="cell-sub">{client.email_domains.join(", ")}</div>
                      ) : null}
                    </Td>
                    <Td>{clientKindLabel(client.kind)}</Td>
                    <Td>
                      <span className="mono">{client.device_count}</span>
                    </Td>
                    <Td>
                      <span className="mono">{client.site_count}</span>
                    </Td>
                    <Td>
                      <StatusPill
                        value={client.status}
                        kind={clientStatusKind(client.status)}
                        label={clientStatusLabel(client.status)}
                      />
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </QueryState>
      </Panel>
      {/* KEYED so a refusal cannot outlive the dialog that produced it. Open
          only for someone who may create one, whatever the URL asks. */}
      <ClientFormDialog
        key={created.open ? "open" : "closed"}
        open={created.open && canCreate}
        client={null}
        onClose={created.close}
        onSaved={(saved) => navigate(`/clients/${saved.id}`)}
      />
      {contact.open ? <ContactClientDialog onClose={contact.close} /> : null}
    </>
  );
}

/**
 * New → Contact with every client shown (redesign design §1.2): a contact
 * belongs to a client, so the first question is which one. Only clients
 * where the person may manage contacts, never Unassigned -- the same rule
 * ContactsTab applies -- and the answer goes to that client's contacts tab,
 * where the form opens.
 */
function ContactClientDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const navigate = useNavigate();
  const { clients } = useClientScope();
  const choices = clients.filter((client) => client.kind !== "unassigned" && client.permissions.includes("clients.manage"));
  const [picked, setPicked] = useState(choices[0]?.id ?? "");
  return (
    <Dialog open onClose={onClose} title={t("clients.list.newContactTitle")}>
      <Form onSubmit={() => picked && navigate(`/clients/${picked}?tab=contacts&new=contact`)}>
        <Field label={t("shell.kit.client")}>
          <select className="select-field" value={picked} onChange={(event) => setPicked(event.target.value)}>
            {choices.map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
          </select>
        </Field>
        <div className="flex flex-wrap items-center justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("common.actions.cancel")}
          </Button>
          <Button type="submit" disabled={!picked}>
            {t("clients.list.newContactContinue")}
          </Button>
        </div>
      </Form>
    </Dialog>
  );
}
```

(`SearchField`, `FilterChips`, `ListToolbar`, `ShowingCount`, `ListEmpty`, `useNewParam` y `useUrlFilters` llegan de `shell/components`, que reexporta el kit.)

- [ ] **Step 7: Textos**

`frontend/src/i18n/en/clients.ts`:
- `detail.tabs`: añadir `devices: "Computers"`, `tickets: "Tickets"`, `alerts: "Alerts"`, `assets: "Assets"`;
- `detail.tiles`: `{ devices: "Computers", sites: "Sites", openTickets: "Open tickets", openAlerts: "Open alerts" }`;
- `list`: `search: "Search clients"`, `archivedLabel: "Archived clients"`, `chipActive: "Active"`, `chipArchived: "Including archived"`, `emptyYet: "No clients yet. Add the first one to give its machines, sites and contacts a home."`; borrar `showArchived` y `empty` (ya no los usa nadie: `ClientDetailPage` deja de usar `empty` con el archivo nuevo del Step 5; `grep -rn "clients.list.empty\b\|clients.list.showArchived" frontend/src` sin resultados después);
- `devices`: `{ title: "Computers", seeAll: { one: "See every computer of this client ({count})", other: "See every computer of this client ({count})" }, empty: "This client has no computers yet.", add: "Add a computer", colComputer: "Computer", colAgent: "Agent", colLastSeen: "Last seen", noAgent: "No agent", never: "Never" }`;
- `tickets`: `{ title: "Open tickets", seeAll: "See this client's tickets", empty: "This client has no open tickets.", add: "New ticket", colTicket: "Ticket", colPriority: "Priority", colStatus: "Status" }`;
- `alerts`: `{ title: "Active alerts", openInAll: { one: "{count} open alert in all, counting acknowledged and muted ones", other: "{count} open alerts in all, counting acknowledged and muted ones" }, seeAll: "See this client's alerts", empty: "No alert is active for this client.", colComputer: "Computer", colCheck: "Check", colSeverity: "Severity", colOpened: "Opened" }`;
- `assets`: `{ licenses: "Licenses", subscriptions: "Subscriptions", networks: "Networks" }`.

`frontend/src/i18n/es/clients.ts`, lo mismo: `devices: "Equipos"`, `tickets: "Tickets"` (en `IDENTICAL_OK_SHARED` de `identical-ok.ts`: `"clients.detail.tabs.tickets"`, y también `"clients.tickets.colTicket"`, con la razón del glosario: «"Ticket" is the glossary term for a ticket in Spanish too (a loanword).»), `alerts: "Alertas"`, `assets: "Activos"`; `tiles`: «Equipos», «Sedes», «Tickets abiertos», «Alertas abiertas»; `list`: «Buscar clientes», `archivedLabel: "Clientes archivados"`, «Activos», «Con archivados», «Aún no hay clientes. Añade el primero para dar un sitio a sus equipos, sedes y contactos.»; `devices`: «Equipos», «Ver todos los equipos de este cliente ({count})», «Este cliente aún no tiene equipos.», «Añadir un equipo», «Equipo», «Agente», «Visto por última vez», «Sin agente», «Nunca»; `tickets`: «Tickets abiertos», «Ver los tickets de este cliente», «Este cliente no tiene tickets abiertos.», «Nuevo ticket», «Ticket», «Prioridad», «Estado»; `alerts`: «Alertas activas», `openInAll: { one: "{count} alerta abierta en total, contando las reconocidas y las silenciadas", other: "{count} alertas abiertas en total, contando las reconocidas y las silenciadas" }`, «Ver las alertas de este cliente», «Este cliente no tiene alertas activas.», «Equipo», «Comprobación», «Gravedad», «Abierta»; `assets`: «Licencias», «Suscripciones», «Redes».

- [ ] **Step 8: Verde, crítica y pulido**

Run: `cd frontend && npx vitest run src/modules/clients src/shell/newDestinations.test.tsx && npx vitest run && npm run typecheck && npm run build`
Expected: verde.

`harmony.mjs --routes` solo acepta rutas de `ROUTES`, así que las pestañas que importan se añaden como variantes (lo permite `routes-covered.test.ts`: «a query variant only of a route it also walks plainly»). En `frontend/scripts/routes.mjs`, justo después de `"/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57",`:

```js
  // The client 360's tabs that read other modules (redesign design §1.3).
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=devices",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=tickets",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=alerts",
  "/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=assets",
```

y, después de `"/devices/dev-1",`, para la O1: `"/devices/dev-1?tab=hardware",` y `"/devices/dev-1?tab=health",`; después de `"/health",`, para la O3 (decisión D21: la configuración de Alertas pasa a vistas en la URL, y sin estas variantes la puerta dejaría de medir esos paneles, que hoy mide en `/health`):

```js
  // Alerts' configuration views (redesign decision D21): the profiles, the
  // approved actions and the settings that /health alone no longer draws.
  "/health?view=profiles",
  "/health?view=actions",
  "/health?view=settings",
```

y después de `"/ipam/subnets/subnet-1",`, para la O5 (5.6, las pestañas de la subred pasan a la URL): `"/ipam/subnets/subnet-1?tab=sweeps",`. Son **diez variantes** en total (127 entradas en `ROUTES`, de 117), todas puestas aquí para que ninguna oleada tenga que tocar `routes.mjs`; una oleada que necesite otra la pide como «petición al kit» (Archivos compartidos). Antes de que la O3 haga las vistas, `/health?view=…` dibuja `/health` y pasa igual: la variante mide lo mismo hasta que hay algo distinto que medir.

Aplicar «Cierre de cada oleada» (pasos 1 a 6) a `ClientsPage.tsx` y `ClientDetailPage.tsx` con las rutas `/clients,/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57,/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=devices,/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=tickets,/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=alerts,/clients/5e0c3a7d-2b9f-4d61-8a4e-6f1b0c9d2e57?tab=assets`.

- [ ] **Step 9: Commit**

```bash
git add frontend/src frontend/scripts/routes.mjs .impeccable/critique
git commit -m "feat(ui): client 360 with URL tabs, the client list on the list kit, and ?client= freed for the Client chip

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación:** las ocho pestañas en una sola fila, con URL; una pestaña sin su código no se dibuja; los datos de Equipos, Tickets, Alertas y Activos son solo del cliente (estrechados por `X-Client-Id` o por `client_id`) y no se leen antes de que `/me` lo confirme; Alertas lista las activas y dice cuántas hay abiertas en total (D19); Activos son tres conteos con su enlace a la lista con el chip puesto (D14); la cabecera lleva estado, tipo y acciones; la pestaña dice `Andes Logistics · Clientes · Novieri RMM` (y su prueba lo fija con `options.page`); el recorrido de «Añadir un equipo» usa `journey_client`/`journey_site` y `?client=` ya solo estrecha listas (D20); `routes.mjs` tiene las diez variantes.

---

## Las oleadas (Tasks 6 a 13)

**Lo que el coordinador copia en el encargo de cada oleada** (el implementador de una oleada solo ve su tarea; revisión previa, W-M6): las Global Constraints; «Archivos compartidos (dueño único)»; las cuatro recetas; «Cierre de cada oleada»; este preámbulo entero; los bloques **Interfaces** de las Tasks 2 y 3 (las firmas exactas de lo que consume); la tarea de la oleada; las decisiones D12, D16, D17, D20 y las de su propia tarea del Apéndice A; y `.impeccable/critica-respuestas.md`, que ya está en la rama base. La worktree se prepara como dicen las Global Constraints («Worktrees»), con su propia carpeta `node_modules`.

Lo común a todas, para no repetirlo en cada página:

- **Antes de tocar una página:** leer las recetas y el «Cierre de cada oleada»; leer la sección de Atera que la oleada cita; abrir la página en el navegador con `npx vite` desde `frontend/` en esta worktree, en la dirección que `npx vite` imprime (`Local: http://localhost:<puerto>/`, su puerto propio: varias worktrees corren a la vez) más `<ruta>?__fixtures`, en los dos idiomas (el idioma se cambia en el menú de la cuenta).
- **Los menús de sección** de un módulo (`deployNav.tsx`, `backupNav.tsx`, `IpamNav.tsx`, `licenseNav.tsx`, `provisioningNav.tsx`, `PushNav.tsx`, `ServiceDeskNav.tsx`, `PortalNav.tsx`) pasan a dibujarse con `SectionNav` del kit, conservando su nombre exportado, su comprobación de permiso y sus etiquetas. Sus pruebas que buscaban `role="tablist"` o `role="tab"` buscan ahora `navigation` y `link` con `aria-current="page"`.
- **Pestañas que filtran** una lista (`?state=`, con `Tabs`) pasan a `FilterChips` con el mismo parámetro.
- **Filtros en `useState`** pasan a `useUrlFilters` con las claves que nombra cada página.
- **Un diálogo de alta** se abre con `useNewParam("<tipo>")` (decisión D8) para que su URL sea enlazable; el botón de la cabecera llama a `start`.
- **Una ficha** llama a `useRecordTitle(<nombre del registro>)`, y su prueba de título usa `renderPage(…, { page: "shell.nav.item.<entrada>" })` (Receta F).
- **El chip «Cliente»** (`ClientFilter` + `useNarrowing` + `useApiFor`, decisiones D6 y D17) va en las listas de cosas de un cliente: equipos, tickets, alertas, parches, despliegues, copias, sesiones remotas, trabajos, licencias, suscripciones, redes, documentación, credenciales, cambios y continuidad. **Presupuesto no**: trabaja con un cliente cada vez y, con «Todos mis clientes», pide elegir uno arriba (decisión de la fase 3), así que no hay lista de todos que estrechar. La oleada lo pone en las que nombra. **`?client=` solo estrecha la lista** (decisión D20): nunca cambia el cliente de toda la consola, que es lo que hace el selector de arriba; el recorrido de «Añadir un equipo» usa `journey_client` desde la Task 5.
- **Las listas que leen por un hook de su módulo** cambian el hook como dice la Receta L, punto 7: toma el valor del chip, lee con `useApiFor` y pone el cliente estrechado en su clave. **No es cambiar una llamada a la API**: la misma función con los mismos argumentos. Cada oleada nombra, página por página, qué hook cambia. Las escrituras siguen con `useApi()`.
- **Toda lista con chip «Cliente»** añade la prueba de la Receta L «reads the list narrowed to the Client chip's client», con `recordingNarrowing` (`src/test/narrowing.ts`).
- **Pruebas por página:** las de la receta (L: filtros en la URL y vacío con causa, o la variante de búsqueda, o solo el vacío para una lista sin filtros; F: pestañas en la URL y título; FO: barra de guardado y aviso al salir, y «leaves for what it saved» si se va al guardar) más lo que la página nombre. Las pruebas existentes de la página se conservan; se cambia la consulta, nunca la aserción, salvo donde el diseño cambia lo que se ve, y entonces el comentario cita el diseño.
- **Textos:** las claves nuevas van en el catálogo del módulo (`src/i18n/{en,es}/<módulo>.ts`), en los dos idiomas; una que queda idéntica en los dos va en el `identical-ok.o<N>.ts` de la oleada, con su razón.
- **Lo que un módulo exporta a otro conserva su firma** («Archivos compartidos»).
- **Prohibido en una oleada:** editar un archivo de las clases 1 o 2 de «Archivos compartidos» que no sea suyo; cambiar una llamada a la API, un código de permiso o un texto de error; borrar o saltar una prueba.
- **Interfaces de toda oleada.** *Consume:* del kit (Task 3) `useUrlFilters`, `FilterChips`, `ShowingCount`, `BulkBar`, `ListEmpty`, `ClientFilter`, `useNarrowing`, `useApiFor`, `useNewParam`, `ListToolbar`, `SectionNav`, `DetailHead`, `useUrlTab`, `UrlTabs`, `DetailGrid`, `ClientContext`, `FormPage`, `FormSection`, `SaveBar`, `UnsavedGuard`, `useUnsavedGuard`, `useDraft`, todos desde `../../shell/components`; `useRecordTitle` y `useDocumentTitle` de `../../shell/title`; `NovieriLockup` y `PRODUCT_NAME` de `../../shell/brand` (O2 y O8); en las pruebas, `renderPage` y `TitleShell` de `../../test/renderPage` y `recordingNarrowing` de `../../test/narrowing`; las firmas exactas están en el bloque **Interfaces** de las Tasks 2 y 3. *Produce:* nada que otra oleada consuma; las URL que enlaza la ficha 360 (`/devices?client=`, `/service-desk?client=`, `/health?client=`, `/licenses?client=`, `/subscriptions?client=`, `/ipam?client=`) deben estrechar su lista al cliente, y eso lo cumplen O1, O2, O3 y O5, cada una con su prueba.

### Task 6: Oleada O1, Equipos (11 páginas)

Atera: `Atera_recorrido_menus_UI_UX.md`, «Devices» (líneas 164-182) y captura «atera 8» (ficha del equipo). Módulos: `devices`, `inventory`, `onboarding`, `agent-push`, `agent-releases`.

**Files:**
- Modify: `frontend/src/modules/devices/**`, `frontend/src/modules/inventory/**`, `frontend/src/modules/onboarding/**`, `frontend/src/modules/agent-push/**`, `frontend/src/modules/agent-releases/**`
- Modify: `frontend/src/i18n/{en,es}/{devices,inventory,onboarding,agentPush,agentReleases}.ts`, `frontend/src/i18n/identical-ok.o1.ts`

**Rutas de la oleada** (para `--routes`): `/devices,/devices/dev-1,/devices/dev-1?tab=hardware,/devices/dev-1?tab=health,/software,/discovered,/device-groups,/devices/onboard,/devices/push-runs,/devices/push-runs/run-1,/agent-push/authorizations,/agent-push/credential-scopes,/agent-packages`

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 1.1 | `devices/DevicesPage.tsx` | `/devices` | L |
| 1.2 | `inventory/DeviceDetailPage.tsx` | `/devices/:deviceId` | F |
| 1.3 | `inventory/SoftwarePage.tsx` | `/software` | L |
| 1.4 | `devices/CandidatesPage.tsx` | `/discovered` | L |
| 1.5 | `devices/GroupsPage.tsx` | `/device-groups` | L |
| 1.6 | `onboarding/OnboardPage.tsx` | `/devices/onboard` | O |
| 1.7 | `agent-push/PushRunsPage.tsx` | `/devices/push-runs` | L |
| 1.8 | `agent-push/PushRunPage.tsx` | `/devices/push-runs/:runId` | F |
| 1.9 | `agent-push/PushAuthorizationsPage.tsx` | `/agent-push/authorizations` | L |
| 1.10 | `agent-push/PushCredentialScopesPage.tsx` | `/agent-push/credential-scopes` | L |
| 1.11 | `agent-releases/AgentReleasesPage.tsx` | `/agent-packages` | O |

**1.1 `DevicesPage.tsx` (`/devices`, L).** Hoy: una tira `Tabs` para `?state=` con los conteos de `state_counts`, tres `FilterBar`, `SearchField` para `?q=`, selects de origen (`?provenance=`) y sede (`?site=`), cinco tiles de la flota, paginación por `?offset=`, selección con «Push», «Approve all» y «Dispatch job».
- Cabecera: título «Equipos» en español (decisión D13: `devices.page.title` y su descripción; el inglés sigue «Devices»); acción principal **«Añadir un equipo»** (enlace `Link className="btn primary"` a `ADD_COMPUTER_TO`, solo con `devices.read`; diseño §1.2); «Exportar inventario» secundaria.
- Los tiles de la flota se quedan encima del panel.
- El panel: `ListToolbar` con la búsqueda; `FilterChips` «Estado» con `all/online/offline/assets/certs/review` y sus conteos de `state_counts` (`review` solo si el servidor lo da); `FilterChips` «Origen» con `agent/operator/promoted/sweep` sin conteo; `ClientFilter` (clave `client`) y, solo con un cliente elegido arriba o en el chip, el select de sede tal como es hoy dentro de un `.chip-select`; `ShowingCount` con el total de la respuesta.
- La selección existente pasa a `BulkBar` con sus tres acciones de hoy (mismas condiciones y diálogos).
- El vacío: `ListEmpty` con «Aún no hay equipos. Instala el agente en el primero.» y la acción «Añadir un equipo».
- La columna «Cliente» se queda (con «Todos mis clientes» es lo que dice de quién es cada equipo).
- Lectura (Receta L, 6): la lista lee hoy con `useQuery` directo (`DevicesPage.tsx:331`, clave `["devices", query]`); pasa a `const narrowing = useNarrowing(filters.values.client); const narrowed = useApiFor(filters.values.client);`, `queryKey: ["devices", query, narrowing.client]`, `queryFn: () => narrowed.listDevices(query)` y `enabled: narrowing.ready`. `invalidateQueries({ queryKey: ["devices"] })` (línea 394) sigue alcanzándola. Las tres acciones en lote siguen con `api` (`useApi()`).
- Pruebas: `DevicesPage.test.tsx`, `DevicesPushSelection.test.tsx`, `DevicesApproveAll.test.tsx`, `DevicesReview.test.tsx` cambian `getByRole("tab", …)` por `getByRole("button", { name: /…/ })` con `aria-pressed`; nuevas: las dos de la receta L, «reads the list narrowed to the Client chip's client» (Receta L, con `recordingNarrowing`), y «puts Add a computer at the head of the page».
- Acepta: la URL `/devices?state=offline&client=<id>` abre la vista filtrada y solo con equipos de ese cliente; sin selección no hay barra de lote; el vacío dice su causa.

**1.2 `DeviceDetailPage.tsx` (`/devices/:deviceId`, F, captura «atera 8»).** Hoy: `PageHead`, `Tabs` en `?tab=` (11: overview, hardware, software, changes, jobs, docs, patch, deploy, backup, provisioning, health), acciones del equipo (`DeviceActions`), dos `FilterBar`, cuatro tablas; la pestaña Resumen es `DeviceOverview.tsx`, con cinco paneles: salud, actualizaciones, copia de seguridad, últimos trabajos y tickets abiertos, cada uno enlazando a su pestaña o página (`DeviceOverview.tsx:1-80`).
- `DetailHead`: volver «Todos los equipos» → `/devices`; título el hostname; estado en línea / sin conexión (`agent_status`, `kind` ok / mut); contexto `ClientContext` con el cliente y la sede; acciones: las de `DeviceActions` de hoy, con «Conectar» (sesión remota) primero.
- Debajo, dos tarjetas de resumen como las de Atera: **Estado de alertas** (las `open_alerts` de `getDeviceHealth` contadas por gravedad, cada una con su etiqueta y su icono, enlazando a `?tab=health`) y **Estado de parches** (lo que hoy da la pestaña de parches: pendientes por gravedad y reinicio pendiente, enlazando a `?tab=patch`). Cada tarjeta se dibuja solo con su permiso (`devices.read` y `patch.read`, como hoy las pestañas).
- `UrlTabs` con las mismas 11 claves (las URL de hoy siguen valiendo), en una sola fila.
- La pestaña Resumen pasa a `DetailGrid`: a la izquierda «Información del equipo» (los `Facts` de hoy: SO, arquitectura, número de serie, CPU, memoria, IP, último visto, versión del agente, origen) y «Relaciones» (cliente, sede, grupo, persona asignada); a la derecha **«Comprobaciones»** (las comprobaciones de salud del equipo con su último valor, `checks[].last_value` y `reported_at`, de `getDeviceHealth`), **«Actividad»** (los últimos trabajos **tal como hoy**: la primera página de los trabajos de la organización filtrada a este equipo, porque `listJobs` no tiene filtro por equipo, `JobListParams` en `lib/api.ts:1161-1165` es `{limit, offset, order}`, y leída solo con `jobs.read`, que es lo que `DeviceDetailPage.test.tsx:197-212` fija; y los últimos cambios de inventario, `getDeviceChanges`) y los dos paneles de hoy que no caben en las tarjetas de arriba: **«Copia de seguridad»** y **«Tickets abiertos»**, los de `DeviceOverview` con sus enlaces, sin cambiar sus lecturas ni sus permisos. Salud y actualizaciones pasan a las dos tarjetas de arriba. Las métricas de CPU y memoria en el tiempo de la captura no existen en el servidor; **el operador decidió el 2026-11-24 que la ficha enseña las últimas lecturas y que las series quedan fuera de este proyecto** (otro proyecto, por el presupuesto de reposo del agente): Apéndice E, B1. No se dibuja un gráfico vacío.
- `useRecordTitle(hostname)`.
- Pruebas: `DeviceDetailPage.test.tsx`, `.hub.test.tsx`, `.assets.test.tsx`, `.resync.test.tsx` cambian solo consultas (la de las líneas 197-212, «no `listJobs` antes de la pestaña Trabajos sin `jobs.read`», sigue igual y en verde); nuevas: receta F, «draws the alert and patch summaries and each leads to its tab», «keeps the backup and open-ticket panels of the overview», y la de título con `{ page: "shell.nav.item.devices" }` (espera `"<hostname> · Devices · Novieri RMM"`).
- Acepta: `?tab=hardware` recarga en Hardware; los cinco datos del Resumen de hoy siguen en la ficha (salud y parches arriba, copia, trabajos y tickets a la derecha); la ficha a 390 px apila la rejilla en una columna sin desbordar.

**1.3 `SoftwarePage.tsx` (`/software`, L, solo búsqueda).** Hoy: `?q=` y `?title=` (el título elegido), dos tablas (títulos; equipos con el elegido) y exportar. Cabecera con «Exportar»; `ListToolbar` con la búsqueda y `ShowingCount`; el título elegido abre su lista de equipos en un `Panel` debajo con un enlace «Quitar» que borra `?title=`. Sin chip «Cliente» (el inventario de software es de la flota). Pruebas: la variante de búsqueda de la Receta L («keeps its search in the URL, so a reload finds the same rows») y «says why the list is empty» con una búsqueda que no encuentra nada; nueva «opens a title's computers from ?title= and Quitar takes it away». Acepta: `?q=` y `?title=` enlazables y recargables; «Exportar» exporta lo mismo que hoy.

**1.4 `CandidatesPage.tsx` (`/discovered`, L).** Hoy: tres tiles, una tabla de máquinas encontradas por barrido con selección y «Avalar», y `ScanSubnetDialog`; es una de las seis páginas del recorrido de «Añadir un equipo» (`useAdoptJourneyClient`, que desde la Task 5 lee `?journey_client=`). `ListToolbar` con `ClientFilter` y la búsqueda (clave `q`, filtro en el navegador sobre dirección y nombre); `BulkBar` con «Avalar» (la acción de hoy); «Escanear una red» como acción principal (`useNewParam("scan")`); vacío con la causa («Ningún barrido ha encontrado máquinas todavía» o «nada con estos filtros»). Lectura: `useQuery` directo (`CandidatesPage.tsx:166`, clave `["asset-candidates"]`) → clave `["asset-candidates", narrowing.client]`, `queryFn` con `useApiFor`, `enabled: narrowing.ready`; avalar sigue con `useApi()`. Pruebas: `CandidatesPage.test.tsx` adaptadas; receta L (búsqueda) y la del chip; nueva «narrows the list and leaves the console's client alone» (decisión D20): con `/discovered?client=<ANDES>` la lectura pasa por `forClient(ANDES)` y `setSelectedClient` no se llama (el selector de arriba sigue en «Todos mis clientes»); y la de `?journey_client=<ANDES>` sigue eligiendo el cliente, como hoy. Acepta: avalar varias sigue mandando una petición por cliente; `?client=` estrecha y `?journey_client=` cambia el cliente, nunca al revés.

**1.5 `GroupsPage.tsx` (`/device-groups`, L).** Hoy: lista de grupos, «mostrar archivados», editor (crear y editar con reglas y vista previa) y grupo abierto con sus miembros. Lista con `ListToolbar` (búsqueda `q`, chip «Archivados» con la clave `archived`, `ClientFilter`); el grupo abierto pasa a `?group=<id>` (enlazable) en un `Panel` a la derecha con `DetailGrid` en escritorio y debajo en móvil; el editor se abre con `?new=group` o `?edit=<id>` y sigue siendo un diálogo grande. Lectura: `GroupsPage.tsx:517`, clave `["device-groups", showArchived]` → `["device-groups", showArchived, narrowing.client]` con `useApiFor` y `enabled: narrowing.ready`; la del selector de miembros (`["devices", "of-client", memberClient]`) no cambia: ya lee el cliente del grupo. Pruebas: `GroupsPage.test.tsx` adaptadas; nuevas: receta L, la del chip y «opens a group from the URL». Acepta: ningún cambio en las llamadas de reglas, miembros o archivado; `?group=` y `?edit=` enlazables.

**1.6 `OnboardPage.tsx` (`/devices/onboard`, O).** «Añadir un equipo», el destino de «Nuevo → Equipo». `FormPage` con volver «Equipos» y su título; las secciones de hoy (instalar en una máquina: instaladores y comando; muchas a la vez: barrido) como `FormSection`, sin barra de guardado (no guarda nada); el paso a paso (`Steps`) se conserva. Pruebas: `OnboardPage.test.tsx` adaptadas; nueva: «leads back to the computers». Acepta: el atajo de Atera «Install agent» queda aquí y en «Nuevo».

**1.7 `PushRunsPage.tsx` (`/devices/push-runs`, L).** Lista de ejecuciones con `FilterChips` de estado (`?state=`, los estados que `runState.ts` ya conoce, sin conteo si el servidor no lo da), `ShowingCount` y vacío con causa («Aún no has instalado el agente en lote.» con «Instalar en varias»; «nada con estos filtros» con «Quitar filtros»); volver a «Equipos» con un `back-link`. Sin chip «Cliente» (una ejecución ya es de un cliente y la fila lo dice). Pruebas: las dos de la receta L en `pushRuns.test.tsx`. Acepta: `/devices/push-runs?state=<un estado>` abre filtrado; ninguna acción de una ejecución cambia (pausar, reanudar, cancelar siguen en su ficha, 1.8).

**1.8 `PushRunPage.tsx` (`/devices/push-runs/:runId`, F).** `DetailHead` con el estado de la ejecución, contexto (cliente) y las acciones de hoy (pausar, reanudar y cancelar con su `ConfirmDialog`: decisión del operador 2a(e), una persona); tiles de destinos (totales, en curso, correctos, fallidos); la tabla paginada de destinos debajo. `useRecordTitle` con el nombre de la ejecución. Pruebas: `pushRuns.test.tsx` adaptadas; receta F sin pestañas: solo la de título, con `{ page: "shell.nav.item.devices" }` (la ejecución vive bajo Equipos: espera `"<nombre de la ejecución> · Devices · Novieri RMM"`). Acepta: las acciones siguen pidiendo confirmación; ninguna se ofrece al pacer (invariante 2a(e)).

**1.9 `PushAuthorizationsPage.tsx` (`/agent-push/authorizations`, L).** Hoy: `PushNav`, `PushReadiness`, un formulario en línea para crear (prefijo, nota, caduca) y «incluir inactivas». `PushNav` → `SectionNav`; la alta pasa a un diálogo con `useNewParam("authorization")` y botón principal «Nueva autorización»; «incluir inactivas» → chip con la clave `inactive`; `ShowingCount`; vacío con causa. Pruebas: `pushPages.test.tsx` adaptadas (la alta se abre por el botón o por `?new=authorization`). Acepta: el texto de seguridad del formulario (invariante 2a) se queda en el diálogo.

**1.10 `PushCredentialScopesPage.tsx` (`/agent-push/credential-scopes`, L).** Igual que 1.9: `SectionNav`; alta en diálogo con `useNewParam("scope")`; «incluir desvinculados» → chip `unbound`; `ShowingCount`; vacío con causa. Pruebas: `pushPages.test.tsx`.

**1.11 `AgentReleasesPage.tsx` (`/agent-packages`, O).** Hoy: cuatro tiles, lista de versiones, canales, enlace de instalación, despliegue gestionado y «publicar versión»; es una página del recorrido (`?journey_client=` desde la Task 5). Cabecera con «Publicar versión» como principal (`useNewParam("release")`); los tiles; las secciones de hoy (`ReleaseChannels`, `InstallLinkPanel`, `AgentUpdatesSection`) como `Panel` en este orden: versiones (lista con `ShowingCount` y «ver anteriores» como chip `older`), canales, enlace de instalación, actualizaciones. Pruebas: `AgentReleasesPage.test.tsx`, `MsiDownload.test.tsx` (el nombre del MSI es el de la Task 2), `PublishReleaseDialog.test.tsx` (los binarios ya se llaman `novieri-agent-*` desde la Task 2) adaptadas solo en consultas; nueva «opens Publish a release from ?new=release». Acepta: nada de lo firmado cambia (invariante 1); publicar reconoce la plataforma de `novieri-agent-<os>-<arch>`.

- [ ] **Step 1:** escribir, página por página, las pruebas nuevas que piden los bloques de arriba en los archivos de prueba que nombran; ejecutar `cd frontend && npx vitest run src/modules/devices src/modules/inventory src/modules/onboarding src/modules/agent-push src/modules/agent-releases` y verlas fallar.
- [ ] **Step 2:** aplicar cada bloque; tras cada página, `npx vitest run <su carpeta de módulo>` en verde antes de pasar a la siguiente.
- [ ] **Step 3:** textos nuevos en los cinco catálogos, en los dos idiomas; `npx vitest run src/i18n src/modules/devices/devices.es.test.tsx src/modules/inventory/inventory.es.test.tsx src/modules/onboarding/onboarding.es.test.tsx src/modules/agent-push/agent-push.es.test.tsx src/modules/agent-releases/agent-releases.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8, con las rutas de arriba.
- [ ] **Step 5: Commit**

```bash
git add frontend/src/modules/devices frontend/src/modules/inventory frontend/src/modules/onboarding frontend/src/modules/agent-push frontend/src/modules/agent-releases frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o1.ts .impeccable/critique
git commit -m "feat(ui): wave 1, computers: list, record, software, discovery, groups, onboarding, push install, agent packages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013TnTdGAz4rSe1YRXXjEqyr"
```

**Criterios de aceptación de la oleada:** las 11 páginas usan el kit y cumplen su «Acepta:»; `/devices?client=<id>` (el enlace de la ficha 360) estrecha la lista y su prueba lo fija; harmony en verde en sus 13 rutas en los dos idiomas; cada página tiene su crítica persistida y cerrada (versionada en el commit); el informe lista las capturas de 1440 y 390 de cada ruta en los dos idiomas.

---

### Task 7: Oleada O2, Inicio, tickets y portal (11 páginas)

Atera: «Dashboard» (líneas 98-111), «Tickets» (112-135), «Admin › Support › Email settings» (550-567), «Admin › Business administration › SLAs» (721-732), «Admin › Customer service › Customer Portal» (641-650). Módulos: `dashboard`, `service-desk`, `sla`, `email-intake`, `notification`, `portal`.

**Files:**
- Modify: `frontend/src/modules/{dashboard,service-desk,sla,email-intake,notification,portal}/**`
- Modify: `frontend/src/i18n/{en,es}/{dashboard,serviceDesk,sla,emailIntake,notification,portal}.ts`, `frontend/src/i18n/identical-ok.o2.ts`
- Modify: `frontend/src/App.portal.test.tsx` (de esta oleada y de ninguna otra: «Archivos compartidos», clase 3; revisión previa W-I13)
- **No** se toca `frontend/src/preview/PreviewPage.tsx`: la Task 3 dejó en él la sección «Portal frame», que dibuja `PortalShell` con un `/portal/me` sembrado, y se repinta sola cuando esta oleada cambia el marco (la puerta de armonía no lo ve: su persona de fixtures no es solo de portal).

**Rutas de la oleada:** `/dashboard,/service-desk,/service-desk/my-work,/service-desk/admin,/service-desk/tickets/tkt-1,/sla,/email-intake,/notifications,/portal,/portal/new,/portal/tickets/tkt-1`

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 2.1 | `dashboard/DashboardPage.tsx` | `/dashboard` | O |
| 2.2 | `service-desk/TicketListPage.tsx` | `/service-desk` | L |
| 2.3 | `service-desk/TicketDetailPage.tsx` | `/service-desk/tickets/:ticketId` | F |
| 2.4 | `service-desk/MyWorkPage.tsx` | `/service-desk/my-work` | L |
| 2.5 | `service-desk/ServiceDeskAdminPage.tsx` | `/service-desk/admin` | FO |
| 2.6 | `sla/SlaAdminPage.tsx` | `/sla` | FO |
| 2.7 | `email-intake/EmailIntakeAdminPage.tsx` | `/email-intake` | FO |
| 2.8 | `notification/NotificationAdminPage.tsx` | `/notifications` | FO |
| 2.9 | `portal/MyTicketsPage.tsx` | `/portal` | L |
| 2.10 | `portal/NewTicketPage.tsx` | `/portal/new` | FO |
| 2.11 | `portal/PortalTicketDetailPage.tsx` | `/portal/tickets/:ticketId` | F |
| + | `email-intake/OAuthCallback.tsx` | `/settings/email-intake/oauth/callback` | O |
| + | `portal/PortalShell.tsx`, `portal/PortalNav.tsx` | marco del portal | — |

**2.1 `DashboardPage.tsx` (`/dashboard`, O).** El tablero personalizable (diseño 2026-09-01: rejilla de seis columnas, tamaños con nombre, disposición guardada por persona en el servidor) **no cambia de modelo**. Cambia la forma: `PageHead` «Inicio» en español (D13) con «Editar» como acción secundaria; cada mosaico es un `Panel` del kit con su título, su número y un destino (regla «Do give every tile a destination»); un mosaico sin datos dice por qué en una frase (Atera los deja en blanco: «Los widgets vacíos quedan en blanco, sin mensaje ni paso siguiente»); quitar un mosaico en modo edición se puede deshacer (Atera no deja: «Quitar un widget es un clic sin confirmación ni deshacer»): el modo edición tiene «Descartar» y «Guardar» en la `SaveBar` del kit, con `useDraft` sobre la disposición. Pruebas: `DashboardPage.test.tsx` y `__tests__/layout.test.ts` adaptadas; nuevas: «discards an edit of the layout», «a tile with nothing measured says so».

**2.2 `TicketListPage.tsx` (`/service-desk`, L).** Hoy: ocho selects (`?assignee`, `?origin`, `?priority`, `?queue`, `?scope`, `?sort`, `?status`, `?type`), `?q`, chips, cinco tiles, `NewTicketDialog`, `ServiceDeskNav`. `ServiceDeskNav` → `SectionNav`. `ListToolbar`: búsqueda; `FilterChips` «Alcance» (`scope`: mine, unassigned, open, all; los conteos que el pie ya calcula, si los hay); `FilterChips` «Estado» (`status`); `ClientFilter`; y en una segunda fila del mismo panel los demás filtros como `.chip-select` (cola, prioridad, tipo, origen, responsable) con su etiqueta visible; el orden (`sort`) a la derecha. Las claves de la URL no cambian. **El alcance no es un filtro cualquiera** (revisión previa, W-M4): sin `?scope=` es `open` (`DEFAULT_SCOPE`, `TicketListPage.tsx:60`), no «todos», así que el chip «Abiertos» es el del valor `""` (el que no escribe nada) y «Todos» escribe `scope=all`; y elegir «Míos» o «Sin asignar» sigue borrando `assignee`, como hace hoy `setScope` (líneas 121-126): los chips llaman a ese `setScope`, no a `filters.set("scope", …)`. Lectura: las dos lecturas de `useTickets` (líneas 151 en adelante, la de la cola y la del resto, cada una con su `enabled`) pasan el chip: `useTickets(filters, { enabled, chip: clientChip })`, y el hook (`service-desk/api.ts:105`) pasa a leer con `useApiFor(options.chip ?? "")`, clave `["service-desk-tickets", filters, narrowing.client]` y `enabled: (options.enabled ?? true) && narrowing.ready` (Receta L, 7); `MyWorkPage` lo sigue llamando sin chip. `ShowingCount` con `total` (la respuesta trae `{items, total}`). Vacío con causa. «Nuevo ticket» ya va por `useNewParam` (Task 4). Pruebas: `__tests__/ticketQueue.test.tsx`, `serviceDesk.test.tsx` adaptadas; receta L, la del chip, y nueva «picking Mine clears the assignee, and Open is the chip that writes nothing». Acepta: `/service-desk` sin parámetros enseña los abiertos con el chip «Abiertos» marcado; `/service-desk?client=<id>` (el enlace de la ficha 360) estrecha a ese cliente.

**2.3 `TicketDetailPage.tsx` (`/service-desk/tickets/:ticketId`, F, dos paneles de Atera).** `DetailHead`: volver «Tickets»; título `#<número> <asunto>`; estado (`StatusPill` del estado) y prioridad; contexto con el cliente (`ClientContext`) y el solicitante; acciones de hoy (asignar, cambiar estado, sesión remota con `StartSessionDialog`). Debajo, `DetailGrid`: a la izquierda la conversación y el editor de respuesta (pública / nota interna, respuestas guardadas), a la derecha las propiedades (cola, categoría, prioridad, responsable, equipo, asunto, descripción, estado con su motivo). **Hoy ya hay un solo «Save changes»** (`TicketDetailPage.tsx:962-964`), y detrás va una confirmación (`ConfirmDialog`, `:966-978`) y un guardado en orden (al reabrir, primero el estado, después la asignación y los campos; si no, campos, asignación y estado con su motivo; `:798-826`). Ese botón pasa a la `SaveBar` del kit **sin tocar su lógica**: el `dirty` que la página ya calcula (`:794-795`), `onSave={submit}` (el `submit` de hoy, que abre la confirmación cuando toca o llama a `save()`), `saving={saving}`, `error={error}` (la barra lo anuncia con `role="alert"`, como hoy el aviso rojo), `blockedReason` con la frase de que falta el asunto cuando `!subject.trim()` (hoy el botón se desactiva sin decirlo; clave nueva `serviceDesk.detail.subjectRequired`), y `onDiscard` que devuelve los nueve estados a los valores del ticket. Las nueve variables de estado (`:769-777`) se quedan: no hace falta `useDraft`, porque la página ya sabe qué cambió. `UnsavedGuard dirty={dirty}`. El equipo enlazado muestra su estado de parches como hoy. `useRecordTitle("#<número>")`. Pruebas: `__tests__/ticketWorkbench.test.tsx` adaptadas (donde pulsaban «Save changes» pulsan el «Save» de la barra; la confirmación y el orden se prueban igual); nuevas: receta FO sobre las propiedades («asks before leaving with an unsaved property»), «keeps the confirmation and the save order of today», y la de título con `{ page: "shell.nav.item.tickets" }` (espera `"#<número> · Tickets · Novieri RMM"`). Acepta: cerrar un ticket con cambios pide la misma confirmación de hoy; el orden de las tres escrituras no cambia.

**2.4 `MyWorkPage.tsx` (`/service-desk/my-work`, L, sin filtros).** Tres tiles y la lista de lo asignado a la persona (`useTickets({ scope: "mine", sort: "due", limit: 200 })`, sin chip: es lo de la persona, de todos sus clientes); `SectionNav`; `ShowingCount` con el `total` de la respuesta; vacío «No tienes tickets asignados.» con enlace a la cola. Pruebas: solo «says why the list is empty» (Receta L, 8) con una respuesta vacía, y «puts the service desk's section navigation at the top, with My work current». Acepta: los tres tiles cuentan lo mismo que hoy; el vacío enlaza a `/service-desk`.

**2.5 `ServiceDeskAdminPage.tsx` (`/service-desk/admin`, FO).** Hoy: colas y categorías con altas y ediciones en línea (once campos, cuatro primarios). Dos `FormSection` (Colas, Categorías), cada una una tabla con su alta en diálogo (`useNewParam("queue")`, `useNewParam("category")`) y la edición de una fila en diálogo: así desaparecen los cuatro primarios de la página. No hay `SaveBar`: cada cambio es una alta o edición de una fila que se guarda al confirmar su diálogo. Pruebas: `serviceDesk.test.tsx` adaptadas; nuevas «opens New queue from ?new=queue» y «edits a category in its dialog and saves it there». Acepta: ningún botón primario en el cuerpo salvo el del diálogo abierto; las llamadas de alta y edición son las de hoy.

**2.6 `SlaAdminPage.tsx` (`/sla`, FO).** Políticas y calendarios: dos `FormSection`; las políticas como lista con alta en diálogo (`useNewParam("policy")`) y chip «Cliente» para las de un cliente (el servidor ya distingue `client_id` nulo de uno): `useSlaPolicies()` (`sla/api.ts:194`) pasa a `useSlaPolicies(chip = "")`, Receta L, 7; los calendarios igual, con `useNewParam("calendar")`, sin chip (son de la organización). Pruebas: `__tests__/sla.test.tsx`, `policiesClient.test.tsx` adaptadas; la del chip sobre las políticas. Acepta: una política «para todos los clientes» se sigue viendo con el chip puesto (el servidor la incluye en la intersección).

**2.7 `EmailIntakeAdminPage.tsx` (`/email-intake`, FO).** Los buzones y su conexión con Microsoft: `FormSection` por buzón; los ajustes editables con `SaveBar` y `UnsavedGuard`; la conexión OAuth sin cambios. El registro de mensajes entrantes es de todos los clientes y solo se lee con el permiso en todos (invariante 5a): **sin** chip «Cliente». `OAuthCallback.tsx`: el mismo panel de hoy dentro del armazón, con `PageHead` y un enlace de vuelta a «Entrada de correo» (la puerta no recorre esta ruta, `EXCLUDED`; se prueba en jsdom). Pruebas: `__tests__/emailIntake.test.tsx`, `oauth.test.tsx` adaptadas; receta FO sobre los ajustes de un buzón. Acepta: conectar con Microsoft sigue el mismo flujo; un ajuste sin guardar avisa al salir.

**2.8 `NotificationAdminPage.tsx` (`/notifications`, FO).** Las reglas y los canales (correo, webhooks): `FormSection` por grupo; las reglas como lista con alta en diálogo (`useNewParam("rule")`); los ajustes con `SaveBar`. El registro de entregas es de todos los clientes (invariante 5a): **sin** chip. Pruebas: `__tests__/notification.test.tsx` adaptadas; receta FO sobre los ajustes. Acepta: los secretos de un canal se siguen escribiendo y nunca se leen (invariante 3); un ajuste sin guardar avisa al salir.

**2.9 `MyTicketsPage.tsx` (`/portal`, L, marco del portal).** Lista de los tickets del solicitante con `FilterChips` de estado (`?state=`: abiertos, el valor `""`, y todos), `ShowingCount` y vacío «Aún no has abierto ningún ticket.» con «Nuevo ticket». Sin chip «Cliente» (el portal es de un contacto). Pruebas: `__tests__/portal.test.tsx` adaptadas; las dos de la receta L. Acepta: `/portal?state=all` recarga con los cerrados; el vacío ofrece «Nuevo ticket» solo con `portal.ticket.write`.

**2.10 `NewTicketPage.tsx` (`/portal/new`, FO, se va al guardar).** `FormPage` con volver «Mis tickets»; una `FormSection`; sugerencias de ayuda (`HelpSuggestions`) a la derecha en escritorio; un formulario de alta se envía una vez, así que su botón es el «Enviar» de hoy y no hay `SaveBar`, pero sí la guarda mientras hay texto escrito (perder una descripción larga es lo que evita). Como al enviar navega al ticket creado (`` navigate(`/portal/tickets/${ticketId}`) ``, `NewTicketPage.tsx:140`), usa `const { guard, release } = useUnsavedGuard(subject.trim() !== "" || description.trim() !== "")` y llama a `release()` justo antes de ese `navigate` (Receta FO, 6; decisión D16): sin él, la página se quedaba en `/portal/new` con «¿Salir sin guardar?» abierto (revisión previa, I-1, reproducido). Pruebas: nuevas «asks before leaving a half-written ticket» y «leaves for the ticket it filed without asking». Acepta: enviar lleva al ticket nuevo sin preguntar; salir con texto escrito pregunta.

**2.11 `PortalTicketDetailPage.tsx` (`/portal/tickets/:ticketId`, F, sin pestañas).** `DetailHead` con estado y volver «Mis tickets»; la conversación y la respuesta; adjuntos. `useDocumentTitle("#<número>")` (el portal no tiene `AppShell`, así que aquí sí `useDocumentTitle` y no `useRecordTitle`). Pruebas: `__tests__/portal.test.tsx` adaptadas; nueva «names the ticket in the browser tab» (espera `"#<número> · Novieri RMM"`, sin `TitleShell`). Acepta: la respuesta del contacto se envía igual que hoy; los adjuntos se descargan igual.

**El marco del portal (`PortalShell.tsx`, `PortalNav.tsx`).** Diseño §4: «El portal del usuario final … recibe la misma marca dentro de su propio marco». La cabecera de hoy (un texto «Support portal» en negrita) pasa a: `NovieriLockup height={20}` enlazado a `/portal`, el nombre de la sección («Portal de soporte», `portal.shell.brand`) a su lado en la tinta secundaria, la persona y «Cerrar sesión» a la derecha; fondo `--surface` con la línea inferior, contenido en `--bg` con el ancho máximo de la consola. `PortalNav` → `SectionNav`. `useDocumentTitle` con el título de cada página del portal. Pruebas: `App.portal.test.tsx` y `modules/portal/__tests__/portalPrimitives.test.tsx` adaptadas; nueva: «draws the Novieri RMM lockup and names the page in the tab».

- [ ] **Step 1:** pruebas nuevas de los bloques; `npx vitest run src/modules/dashboard src/modules/service-desk src/modules/sla src/modules/email-intake src/modules/notification src/modules/portal src/App.portal.test.tsx` falla en ellas.
- [ ] **Step 2:** aplicar página por página, con la carpeta del módulo en verde tras cada una.
- [ ] **Step 3:** textos en los seis catálogos, en los dos idiomas; las pruebas `*.es.test.tsx` de los seis módulos en verde.
- [ ] **Step 4:** `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs` en verde: mide el marco nuevo del portal en la sección «Portal frame» que la Task 3 dejó en `/__preview` (este archivo no se toca).
- [ ] **Step 5:** «Cierre de cada oleada», pasos 1 a 8, con las rutas de arriba.
- [ ] **Step 6: Commit** (`git add frontend/src/modules/{dashboard,service-desk,sla,email-intake,notification,portal} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o2.ts frontend/src/App.portal.test.tsx .impeccable/critique`; mensaje `feat(ui): wave 2, dashboard, tickets, service desk admin and the portal frame` con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; una sola forma de guardar en las páginas de configuración de la oleada; el ticket guarda sus propiedades con la barra, con la confirmación y el orden de hoy; enviar un ticket del portal lleva a él sin preguntar; `/service-desk?client=<id>` estrecha la cola (su prueba lo fija); el marco del portal lleva la marca y se mide en `/__preview`; harmony en verde en las 11 rutas en los dos idiomas.

---

### Task 8: Oleada O3, Alertas, parches, software, sesiones, trabajos y registros (11 páginas)

Atera: «Alerts» (184-198), «Patch Management» (199-215), «Admin › Monitoring › Scripts» (466-476), «Admin › Monitoring › Software management» (516-524), «Admin › Users and security › Audit log» (436-444). Módulos: `health`, `patch`, `deploy`, `remote`, `jobs`, `error-monitor`, `platform-status`, `security`, `audit`.

**Files:**
- Modify: `frontend/src/modules/{health,patch,deploy,remote,jobs,error-monitor,platform-status,security,audit}/**`
- Modify: `frontend/src/i18n/{en,es}/{health,patch,deploy,remote,jobs,errorMonitor,platformStatus,security,audit,auditVocabulary}.ts`, `frontend/src/i18n/identical-ok.o3.ts`

**Rutas de la oleada:** `/health,/health?view=profiles,/health?view=actions,/health?view=settings,/patch,/patch/policies,/deploy,/deploy/packages,/deploy/scripts,/remote,/remote/sess-1,/remote/sess-1/recording,/jobs,/error-monitor,/platform-status,/security,/audit` (las tres variantes de `/health` las puso la Task 5, Step 8)

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 3.1 | `health/HealthPage.tsx` | `/health` | L |
| 3.2 | `patch/FleetPatchPage.tsx` | `/patch` | L |
| 3.3 | `deploy/DeployPage.tsx` | `/deploy` | L |
| 3.4 | `deploy/PackagesPage.tsx` | `/deploy/packages` | L |
| 3.5 | `deploy/ScriptsPage.tsx` | `/deploy/scripts` | L |
| 3.6 | `remote/SessionsPage.tsx` | `/remote` | L |
| 3.7 | `jobs/JobsPage.tsx` | `/jobs` | L |
| 3.8 | `error-monitor/ErrorMonitorPage.tsx` | `/error-monitor` | L |
| 3.9 | `platform-status/PlatformStatusPage.tsx` | `/platform-status` | O |
| 3.10 | `security/SecurityPage.tsx` | `/security` | L |
| 3.11 | `audit/AuditPage.tsx` | `/audit` | L |
| + | `patch/PatchPolicyEditor.tsx` | `/patch/policies` | FO |
| + | `remote/ScreenViewer.tsx` | `/remote/:sessionId` | O |
| + | `remote/RecordingPlayer.tsx` | `/remote/:sessionId/recording` | O |

**3.1 `HealthPage.tsx` (`/health`, L, con vistas: decisión D21).** «Alertas» en el riel. Hoy: cuatro tiles, la consola de alertas (`AlertConsole`) con `?alert=<id>` para el detalle, el informe (`HealthReportPanel`, `HealthPage.tsx:243`) y debajo, cada uno en su ancla, perfiles con su editor y sus asignaciones (`#profiles`), acciones aprobadas (`#approved-actions`) y ajustes (`#health-settings`), con `useScrollToHash` (`:131`) para llegar a ellos; tres módulos enlazan a esas anclas (`health/AlertReadiness.tsx:38`, `clients/SummaryTab.tsx:122` a `/health#profiles`; `ai-automation/AutomationPrerequisites.tsx:70` a `/health#approved-actions`).
- **Las vistas** (decisión D21; la pregunta abierta 3 resuelta por el coordinador el 2026-11-24 con el valor por defecto del plan): `UrlTabs` con `useUrlTab(["alerts", "profiles", "actions", "settings"], "alerts", "view")` en la misma ruta; por defecto `alerts`, sin parámetro. **Cada panel de hoy tiene su sitio**: `alerts` es la cola, sus tiles y el informe (`HealthReportPanel`, debajo de la cola: es el informe de las alertas); `profiles` son `ProfileList`, `ProfileEditor` y `ProfileAssignments`; `actions` es `ApprovedActionList`; `settings` es `HealthSettingsPanel`. Los envoltorios con `id` se quedan dentro de su vista (`<div id="profiles">`, `<div id="approved-actions">`, `<div id="health-settings">`). No es `SectionNav`: sus enlaces comparan la ruta, no la query, y los cuatro se marcarían a la vez.
- **Un enlace de antes abre su vista** y después baja al panel, como hoy:
  ```tsx
  /** The anchors other modules link to, and the view that now holds each (decision D21). */
  const HASH_VIEW: Readonly<Record<string, "profiles" | "actions" | "settings">> = {
    "#profiles": "profiles",
    "#approved-actions": "actions",
    "#health-settings": "settings",
  };
  // A LINK FROM BEFORE THE VIEWS (`/health#profiles`; three modules still
  // write them) opens the view that holds its panel, keeping the fragment, so
  // `useScrollToHash` below still lands on it.
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    const view = HASH_VIEW[location.hash];
    if (!view || new URLSearchParams(location.search).has("view")) return;
    const next = new URLSearchParams(location.search);
    next.set("view", view);
    navigate({ search: `?${next.toString()}`, hash: location.hash }, { replace: true });
  }, [location.hash, location.search, navigate]);
  ```
  (con `useLocation` y `useNavigate` de `react-router-dom` y `useEffect` de `react`). Los tres módulos que enlazan no cambian.
- **La cola** (vista `alerts`): `ListToolbar` con `FilterChips` «Estado» (activas, reconocidas, silenciadas: las de `AlertState` menos `cleared`) y «Gravedad» (crítica, advertencia, información), **con los conteos de `getHealthAlertSummary`** (`by_state`, `by_severity`), `ClientFilter` y `ShowingCount`; la alerta elegida (`?alert=`) se abre en el `Drawer` de hoy. Lectura: `useHealthAlerts(filters)` (`health/api.ts:257`, clave `["health-alerts", filters]`) pasa a `useHealthAlerts(filters, chip = "")` con `useApiFor(chip)`, clave `["health-alerts", filters, narrowing.client]` y `enabled: narrowing.ready` (Receta L, 7); `AlertConsole` le pasa el chip. El vacío dice la causa real: sin equipos, «Ningún equipo envía todavía su estado.» con «Añadir un equipo» (el defecto que el recorrido de Atera señala); con filtros, «nada con estos filtros».
- Pruebas: `__tests__/health.test.tsx`, `alertTriage.test.tsx`, `assignments.test.tsx` adaptadas; receta L; la del chip; nuevas «says no computer reports yet when there are none» y «opens each configuration view from the URL». **Dos casos de `alertTriage.test.tsx` cambian a propósito** (295-313, `describe("Health page anchors")`), con el comentario `// Redesign decision D21 (open question 3, resolved 2026-11-24): each panel lives in its view now; an old anchor opens that view.`: «gives profiles, approved actions and health settings an anchor each, around the right panel» monta cada vista (`/health?view=profiles`, `/health?view=actions`, `/health?view=settings`) y busca en cada una su ancla y su título, que son los de hoy; «scrolls to the panel a link names on arrival» sigue montando `/health#approved-actions` y además espera la pestaña «Approved actions» con `aria-selected="true"` antes de comprobar el `scrollIntoView`. `alertReadiness.test.tsx:75` (el enlace `/health#profiles`) no cambia.
- Acepta: `/health#profiles`, `/health#approved-actions` y `/health#health-settings` abren su vista y bajan a su panel; `/health?client=<id>` (el enlace de la ficha 360) estrecha la cola; los cuatro paneles de configuración y el informe siguen alcanzables y la puerta los mide en sus variantes.

**3.2 `FleetPatchPage.tsx` (`/patch`, L) y `PatchPolicyEditor.tsx` (`/patch/policies`, FO).** Hoy: `Tabs` por familia de SO en `?os=`, `?q`, `?severity`, `?offset`, `?devices`, exportar y selección. La familia sigue en `UrlTabs` con `param="os"` (cambia el contenido, no filtra una lista); dentro, `ListToolbar` con búsqueda, `FilterChips` «Gravedad» (las de hoy, multi: un chip por gravedad con `aria-pressed`, la URL conserva su formato de lista), `ClientFilter`, `ShowingCount`; la selección pasa a `BulkBar` con sus acciones. Lectura: `usePatchFleetReport()` (`patch/api.ts:202`) y `usePatchApprovals(filters)` (`:311`) toman el chip como último argumento (`chip = ""`), Receta L, 7; aprobar y escanear siguen con `useApi()`. «Políticas» como acción secundaria de la cabecera. `PatchPolicyEditor` es receta FO: `FormPage` con volver «Parches», `FormSection` por grupo (anillos, ventana con `WeekPainter`, aprobación), `SaveBar`. Pruebas: `__tests__/patch.test.tsx`, `selection.test.ts`, `apps.test.tsx` adaptadas; receta L; la del chip; receta FO en el editor. Acepta: ninguna decisión de parches cambia de parámetros (es un trabajo firmado: invariante 1); `?os=` y la gravedad recargan igual.

**3.3 `DeployPage.tsx` (`/deploy`, L).** `deployNav` → `SectionNav`. `?q`, `?state` → búsqueda y `FilterChips` de estado; seis selects del diálogo de alta se quedan en el diálogo; `ClientFilter`; «Nuevo despliegue» principal con `useNewParam("deployment")`; ejecutar sigue con `ConfirmDialog`. Lectura: `useDeployments()` (`deploy/api.ts:146`) pasa a `useDeployments(chip = "")`, Receta L, 7. Pruebas: `__tests__/deploy.test.tsx` adaptadas; receta L; la del chip; «opens New deployment from ?new=deployment». Acepta: ejecutar un despliegue sigue pidiendo confirmación y firma el mismo trabajo (invariante 1).

**3.4 `PackagesPage.tsx` (`/deploy/packages`, L).** Hoy: la lista de paquetes de `usePackages()`, sin filtros, «Nuevo paquete» en un `useState` (`createOpen`, línea 50) y borrar con `ConfirmDialog`. Pasa a: `SectionNav`; `useUrlFilters(["q", "os"])`: búsqueda `q` en el navegador sobre nombre y versión, y `FilterChips` «Sistema» (`os`: todos, Windows, Linux, macOS, sin conteo: la respuesta no lo da) que filtra en el navegador por `os_family`; `ShowingCount` con el total de la respuesta; «Nuevo paquete» con `useNewParam("package")` en lugar de `createOpen`; borrar con `ConfirmDialog` como hoy; vacío «Aún no hay paquetes.» con «Nuevo paquete», o «nada con estos filtros». Sin chip «Cliente» (los paquetes son de la organización). Pruebas: las dos de la receta L (`?os=linux` y un chip distinto); «opens New package from ?new=package»; «still asks before deleting a package». Acepta: la alta y el borrado mandan lo mismo que hoy.

**3.5 `ScriptsPage.tsx` (`/deploy/scripts`, L).** Hoy: la lista de scripts, «Nuevo script» en `useState` (`authorOpen`, línea 64), editar (`editing`) y ejecutar (`running`) en diálogos. Pasa a: `SectionNav`; `useUrlFilters(["q", "os", "interpreter"])`: búsqueda `q` en el navegador sobre el nombre, `FilterChips` «Sistema» (`os`) e «Intérprete» (`interpreter`: uno por valor de `INTERPRETERS`, `deploy/types.ts:51`, con la etiqueta de `interpreterLabel`), sin conteo; `ShowingCount`; «Nuevo script» con `useNewParam("script")` (`AuthorScriptDialog`); editar y ejecutar siguen en sus diálogos con su estado de hoy (ejecutar firma un trabajo: invariante 1, no cambia); vacío con causa. Sin chip «Cliente». Pruebas: las dos de la receta L; «opens New script from ?new=script»; «runs a script through the same signed-job dialog as today» (la prueba de hoy, adaptada solo en consultas). Acepta: ejecutar un script manda el mismo trabajo y pide lo mismo que hoy.

**3.6 `SessionsPage.tsx` (`/remote`, L), `ScreenViewer.tsx`, `RecordingPlayer.tsx` (O).** La tira `Tabs` de `?state=` pasa a `FilterChips` con el mismo parámetro; `?q`; `ClientFilter`; «Iniciar sesión remota» principal (`useNewParam("session")`, `StartSessionDialog`; el diálogo lo importa también `service-desk`, así que su firma no cambia). Lectura: `useQuery` directo (`SessionsPage.tsx:100`, clave `["remote-sessions"]`) → `["remote-sessions", narrowing.client]` con `useApiFor` y `enabled: narrowing.ready`. El visor y el reproductor conservan su lienzo; su cabecera pasa a `DetailHead` (volver «Sesiones remotas», el equipo, el estado de la sesión, «Finalizar» como acción) y `useRecordTitle` con el equipo. Pruebas: `SessionsPage.test.tsx`, `RecordingPlayer.test.tsx` adaptadas (`ITMSRSP1` se queda: es formato); receta L; la del chip; la de título en el visor con `{ page: "shell.nav.item.remoteSessions" }`. Acepta: iniciar y finalizar una sesión hacen lo mismo que hoy y quedan en el registro (invariante 4).

**3.7 `JobsPage.tsx` (`/jobs`, L).** Hoy: `?q`, `?status` y `?job` (el trabajo abierto) ya están en la URL (`JobsPage.tsx:195-209`), con selects. Pasa a: `FilterChips` de estado sobre `status` (la respuesta de `listJobs` no trae conteos: sin número); la búsqueda `q`; `ClientFilter`; `ShowingCount`; el trabajo abierto en su `Drawer` con `?job=`; cancelar y reintentar con su confirmación. Lectura: `useQuery` directo (`:241`, clave `["jobs"]`) → `["jobs", narrowing.client]` con `useApiFor` y `enabled: narrowing.ready`; `invalidateQueries({ queryKey: ["jobs"] })` (`:287, 311, 321`) la sigue alcanzando; cancelar y reintentar con `useApi()`. Pruebas: las dos de la receta L; la del chip; «opens a job from ?job= in its drawer». Acepta: cancelar y reintentar piden confirmación como hoy; `?job=` enlazable.

**3.8 `ErrorMonitorPage.tsx` (`/error-monitor`, L).** Hoy: `?q`, `?since`, `?sort`, `?state`, `?where` ya en la URL (`ErrorMonitorPage.tsx:186-201`) con selects, y el grupo elegido en `useState` (`selectedId`). Pasa a: `FilterChips` de estado (`state`) y de origen (`where`: `WHERE_VALUES`), el periodo (`since`) como chips (las claves de `SINCE_DAYS`: 24 h, 7 días, 30 días), el orden (`sort`) como `.chip-select`; el grupo elegido pasa a `?group=<id>` en su panel; resolver con confirmación. Sin chip «Cliente»: los errores son de la plataforma, no de un cliente. Pruebas: las dos de la receta L sobre `state`; «opens a group from ?group=»; «still asks before resolving». Acepta: las mismas lecturas y la misma resolución de hoy; `?since=` y `?where=` recargan igual.

**3.9 `PlatformStatusPage.tsx` (`/platform-status`, O).** Panel de estado: tiles por dependencia con su estado (`StatusPill` con icono), una tabla de sondas y otra de módulos; el estado global en `PageHead` como etiqueta. Sin cambios de datos. Pruebas: `PlatformStatusPage.test.tsx` adaptadas.

**3.10 `SecurityPage.tsx` (`/security`, L).** Hoy: eventos de seguridad con filtros en un objeto `filters` (clave `["security-events", filters]`, `SecurityPage.tsx:174`), el elegido en `useState` (`selected`) y «Exportar evidencia». Pasa a: `useUrlFilters(["q", "type", "severity", "client"])` con búsqueda, chips de tipo y gravedad (sin conteo: la respuesta no lo da), `ClientFilter`, `ShowingCount`; el evento elegido en su `Drawer` con `?event=<id>`; «Exportar evidencia» secundaria. Lectura: clave `["security-events", filters, narrowing.client]`, `useApiFor`, `enabled: narrowing.ready` (los eventos son de un cliente en el servidor, `security_logs/api.py:123`). Pruebas: las dos de la receta L; la del chip; «opens an event from ?event=». Acepta: la evidencia exportada es la de hoy; sin `audit.read` la página no se ofrece (el riel ya lo hace, invariante 6).

**3.11 `AuditPage.tsx` (`/audit`, L).** Ya tiene `?tab=` (eventos / integridad) con `TAB_PARAM`: pasa a `UrlTabs` con el mismo parámetro; en Eventos, `search` y `action` (hoy en `useState`) pasan a `useUrlFilters(["q", "action"])`; el registro es de todos los clientes y solo se lee con el permiso en todos (invariante 5a), así que **no** lleva `ClientFilter`. Pruebas: `AuditPage.test.tsx` adaptadas; receta L sobre `q` y `action`.

- [ ] **Step 1:** pruebas nuevas; `npx vitest run src/modules/health src/modules/patch src/modules/deploy src/modules/remote src/modules/jobs src/modules/error-monitor src/modules/platform-status src/modules/security src/modules/audit` falla en ellas.
- [ ] **Step 2:** aplicar página por página, con su carpeta en verde tras cada una.
- [ ] **Step 3:** textos en los nueve catálogos; sus `*.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8.
- [ ] **Step 5: Commit** (`git add frontend/src/modules/{health,patch,deploy,remote,jobs,error-monitor,platform-status,security,audit} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o3.ts .impeccable/critique`; mensaje `feat(ui): wave 3, alerts, patching, software, remote sessions, jobs and logs`, con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; los conteos de los chips de Alertas salen del resumen del servidor; las anclas de antes de `/health` abren su vista; `/health?client=<id>` estrecha la cola; ninguna lista de la oleada tiene una tira de pestañas para filtrar; ningún trabajo firmado cambia de parámetros; harmony en verde en las 17 rutas.

---

### Task 9: Oleada O4, Copias, continuidad, imágenes, servidores de sitio e inscripción (11 páginas)

Atera: no tiene copias ni continuidad; se aplican los patrones de «Patrones UI/UX transversales» (773-796). Módulos: `backup`, `bcp`, `provisioning`, `site-servers`, `enrollment`.

**Files:**
- Modify: `frontend/src/modules/{backup,bcp,provisioning,site-servers,enrollment}/**`
- Modify: `frontend/src/i18n/{en,es}/{backup,bcp,provisioning,siteServers,enrollment}.ts`, `frontend/src/i18n/identical-ok.o4.ts`

**Rutas de la oleada:** `/backup,/backup/repositories,/backup/policies,/backup/coverage,/backup/offsite,/bcp,/bcp/plans/plan-1,/bcp/report,/bcp/tests/test-1,/bcp/tests/test-1/report,/provisioning,/provisioning/images,/provisioning/profiles,/site-servers,/site-servers/ss-1,/enrollment`

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 4.1 | `backup/BackupPointsPage.tsx` | `/backup` | L |
| 4.2 | `backup/RepositoriesPage.tsx` | `/backup/repositories` | L |
| 4.3 | `backup/CoveragePage.tsx` | `/backup/coverage` | L |
| 4.4 | `backup/OffsitePage.tsx` | `/backup/offsite` | FO |
| 4.5 | `bcp/PlansPage.tsx` | `/bcp` | L |
| 4.6 | `provisioning/ProvisioningRunsPage.tsx` | `/provisioning` | L |
| 4.7 | `provisioning/ImagesPage.tsx` | `/provisioning/images` | L |
| 4.8 | `provisioning/ProfilesPage.tsx` | `/provisioning/profiles` | L |
| 4.9 | `site-servers/SiteServersPage.tsx` | `/site-servers` | L |
| 4.10 | `site-servers/SiteServerDetailPage.tsx` | `/site-servers/:siteServerId` | F |
| 4.11 | `enrollment/EnrollmentPage.tsx` | `/enrollment` | L |
| + | `backup/BackupPolicyEditor.tsx` | `/backup/policies` | FO |
| + | `bcp/PlanDetail.tsx` | `/bcp/plans/:planId` | F |
| + | `bcp/TestRecorder.tsx` | `/bcp/tests/:testId` | FO |
| + | `bcp/BcpReport.tsx` | `/bcp/report`, `/bcp/tests/:testId/report` | O |

**4.1 `BackupPointsPage.tsx` (`/backup`, L).** `backupNav` → `SectionNav`. Cinco tiles; `?verify` se conserva; `deviceId` (hoy `useState`) pasa a `useUrlFilters(["device", "client"])`; `ClientFilter`; «Hacer una copia» principal (`useNewParam("run")`, `RunBackupDialog`); restaurar en `RestoreDialog` como hoy. Lectura: `useBackupPoints()` (`backup/api.ts:114`, clave `["backup-points"]`) pasa a `useBackupPoints(chip = "")`, Receta L, 7; copiar y restaurar siguen con `useApi()`. Pruebas: `__tests__/backup.test.tsx` adaptadas; receta L; la del chip; «opens Run a backup from ?new=run». Acepta: restaurar pide lo mismo que hoy y firma el mismo trabajo (invariante 1).

**4.2 `RepositoriesPage.tsx` (`/backup/repositories`, L).** `?state` → `FilterChips` (activos / retirados); «Nuevo repositorio» (`useNewParam("repository")`); retirar con confirmación (fase 3). Pruebas: `__tests__/retire.test.tsx` adaptadas; receta L.

**4.3 `CoveragePage.tsx` (`/backup/coverage`, L).** `?coverage` → `FilterChips` (cubiertos, sin cubrir, todos) con los conteos que la página ya calcula del informe (el informe entero llega en una respuesta, así que esos conteos son totales, no de una página); `ClientFilter`. Lectura: `useBackupCoverage(coverage)` (`backup/api.ts:274`) pasa a `useBackupCoverage(coverage, chip = "")`, Receta L, 7. Pruebas: `coverage.fixture.test.tsx` adaptadas; las dos de la receta L; la del chip. Acepta: los conteos de los chips suman el total del informe.

**4.4 `OffsitePage.tsx` (`/backup/offsite`, FO).** El asistente de destino externo (clave pública, prueba, guardado): `FormPage`-like dentro de `PageHead` con `SectionNav`; cada destino como `FormSection`; su edición con `useDraft` + `SaveBar` + `UnsavedGuard` (hoy, «editing/values» en estado y dos primarios); `TargetDialog` para el alta (`useNewParam("target")`). Pruebas: `__tests__/offsite.test.tsx` adaptadas; receta FO.

**4.5 `PlansPage.tsx` (`/bcp`, L), `PlanDetail.tsx` (F), `TestRecorder.tsx` (FO), `BcpReport.tsx` (O).** Lista de planes con cuatro tiles, búsqueda, `ClientFilter` y alta (`useNewParam("plan")`, el `editing` de hoy). Lectura: `usePlans()` (`bcp/api.ts:53`) pasa a `usePlans(chip = "")`, Receta L, 7. `PlanDetail`: `DetailHead` con el estado del plan, acciones (editar, programar prueba), `UrlTabs` si el plan tiene más de una sección (pasos, pruebas, evidencias), `useRecordTitle` con el nombre del plan. `TestRecorder`: el registro de una prueba es un formulario largo (16 campos): `FormSection` por paso y `SaveBar` con `UnsavedGuard` (perder un ejercicio a medio registrar es lo que la guarda evita); si al guardar va al informe de la prueba, usa `useUnsavedGuard` y `release()` (Receta FO, 6). `BcpReport`: informe imprimible, `PageHead` con «Imprimir» y volver. Pruebas: `__tests__/bcp.test.tsx` adaptadas; recetas L (con la del chip), F (con la de título, `{ page: "shell.nav.item.continuityTests" }`) y FO en sus páginas. Acepta: el registro de una prueba avisa antes de salir; el informe imprime lo mismo que hoy.

**4.6 `ProvisioningRunsPage.tsx` (`/provisioning`, L).** Hoy: `?state` con una tira (`ProvisioningRunsPage.tsx:110`), «Construir un equipo» en `useState` (`provisionOpen`, línea 105), `useProvisionRuns()`. Pasa a: `provisioningNav` → `SectionNav`; `?state` → `FilterChips` con el mismo parámetro, sin conteo (la respuesta no lo da); `ShowingCount`; «Construir un equipo» con `useNewParam("run")` (`ProvisionDialog`); vacío «Aún no se ha construido ningún equipo.» con «Construir un equipo», o «nada con estos filtros». Sin chip «Cliente» (Imaging trabaja en el cliente elegido arriba). Imaging no está en el riel ni en la búsqueda (decisión P3 del proyecto de clientes); la miga la pone bajo Administración y eso no cambia. Pruebas: las dos de la receta L sobre `state`; «opens Build a computer from ?new=run». Acepta: construir manda el mismo trabajo firmado que hoy (invariante 1).

**4.7 `ImagesPage.tsx` (`/provisioning/images`, L).** Hoy: `?state` (`ImagesPage.tsx:53`), «Nueva imagen» en `useState` (`createOpen`, línea 49), solo con `provisioning.manage` en todos los clientes. Pasa a: `SectionNav`; `?state` → `FilterChips`, sin conteo; `ShowingCount`; «Nueva imagen» con `useNewParam("image")`, abierta solo con `canManage` aunque la URL lo pida; vacío con causa. Pruebas: las dos de la receta L; «opens New image from ?new=image only for someone who may manage images». Acepta: la alta manda lo mismo que hoy.

**4.8 `ProfilesPage.tsx` (`/provisioning/profiles`, L).** Hoy: `?enabled` (`ProfilesPage.tsx:45`) y `editing: ProvisionProfile | "new" | null` en `useState` (línea 42). Pasa a: `SectionNav`; `?enabled` → `FilterChips` (activos, el valor `""`; todos), sin conteo; `ShowingCount`; la alta con `useNewParam("profile")` y la edición con `?edit=<id>` (el `ProfileEditor` de hoy, con sus nueve campos, en su diálogo); vacío con causa. Pruebas: las dos de la receta L; «opens a profile's editor from ?edit=<id>». Acepta: el editor siembra sus campos igual que hoy (`ProfilesPage.tsx:225`).

**4.9 `SiteServersPage.tsx` (`/site-servers`, L).** Hoy: `?state` (`SiteServersPage.tsx:58`), «Promover un equipo» en `useState` (`promoteOpen`, línea 52), `useSiteServers()`. Pasa a: `?state` → `FilterChips`, sin conteo; `ClientFilter`; `ShowingCount`; «Promover un equipo» con `useNewParam("promote")` (`PromoteDialog`); vacío con causa («Aún no hay servidores de sitio: promueve un equipo de la red del cliente.»). Lectura: `useSiteServers(filters, options)` (`site-servers/api.ts:76`) gana `chip?: string` en `options`, como `useTickets`: clave `[SITE_SERVERS_KEY, filters, narrowing.client]`, `enabled: (options.enabled ?? true) && narrowing.ready`. Pruebas: las dos de la receta L; la del chip; «opens Promote a computer from ?new=promote». Acepta: promover sigue siendo un acto de una persona y queda en el registro (invariante 2a(a): el servidor de sitio es el único que origina un push).

**4.10 `SiteServerDetailPage.tsx` (`/site-servers/:siteServerId`, F, sin pestañas).** `DetailHead` con el equipo, su estado y su cliente y sede; «Encender» (Wake-on-LAN, el `wakeJobId` de hoy) como acción; `useRecordTitle`. Prueba: la de título con `{ page: "shell.nav.item.siteServers" }` (espera `"<nombre del equipo> · Site servers · Novieri RMM"`). Acepta: «Encender» manda el mismo trabajo firmado de hoy.

**4.11 `EnrollmentPage.tsx` (`/enrollment`, L con pestañas).** Ya tiene `?tab=`: pasa a `UrlTabs` (tokens, certificados) con el mismo parámetro; «mostrar revocados» de cada pestaña (`showRevokedTokens`, `showRevokedCerts`, líneas 347-348) → chip `revoked`; «solo ilimitados» (`unlimitedOnly`, 362) → chip `unlimited`; la búsqueda (`search`, 395) → `q`; el alta de un token (`formOpen`) → `useNewParam("token")`; el token recién creado se muestra una vez como hoy (invariante 1c: nunca en una respuesta MCP; aquí no cambia nada). Es una página del recorrido de «Añadir un equipo»: lee `?journey_client=` desde la Task 5 y no lleva chip «Cliente». Pruebas: `EnrollmentPage.test.tsx` adaptadas; receta L sobre `revoked`; `clientSelection.test.tsx` en verde con `journey_client` (Task 5). Acepta: `/enrollment?tab=certificates&revoked=1` recarga en esa vista; el token se ve una sola vez.

- [ ] **Step 1:** pruebas nuevas; `npx vitest run src/modules/backup src/modules/bcp src/modules/provisioning src/modules/site-servers src/modules/enrollment` falla en ellas.
- [ ] **Step 2:** aplicar página por página.
- [ ] **Step 3:** textos en los cinco catálogos; sus `*.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8 (las críticas incluyen `BackupPolicyEditor.tsx`, `PlanDetail.tsx`, `TestRecorder.tsx` y `BcpReport.tsx`).
- [ ] **Step 5: Commit** (`git add frontend/src/modules/{backup,bcp,provisioning,site-servers,enrollment} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o4.ts .impeccable/critique`; mensaje `feat(ui): wave 4, backup, continuity, imaging, site servers and enrollment`, con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; el registro de una prueba de continuidad avisa antes de salir; ningún diálogo de alta queda sin URL; las listas con chip «Cliente» lo prueban; harmony en verde en las 16 rutas.

---

### Task 10: Oleada O5, Activos: licencias, suscripciones y direcciones IP (11 páginas)

Atera: «Asset Inventory» (216-229), «Network Discovery» (230-241), «Admin › Business administration › Contracts» (708-720) y «Contract expiration» (756-759) como referencia de renovaciones. Módulos: `licenses`, `subscriptions`, `ipam`. Es la oleada con más pantallas extra (siete); el coordinador puede darle dos implementadores, uno para `licenses` + `subscriptions` y otro para `ipam`, porque no comparten archivos.

**Files:**
- Modify: `frontend/src/modules/{licenses,subscriptions,ipam}/**`
- Modify: `frontend/src/i18n/{en,es}/{licenses,subscriptions,ipam}.ts`, `frontend/src/i18n/identical-ok.o5.ts`

**Rutas de la oleada:** `/licenses,/licenses/lic-1,/licenses/vendors,/licenses/products,/licenses/compliance,/licenses/review-queue,/licenses/renewals,/subscriptions,/subscriptions/sub-1,/subscriptions/spend,/subscriptions/renewals,/ipam,/ipam/subnets/subnet-1,/ipam/subnets/subnet-1?tab=sweeps,/ipam/drift,/ipam/search,/ipam/registry,/ipam/scan-authorizations,/ipam/import` (la variante `?tab=sweeps` la puso la Task 5, Step 8)

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 5.1 | `licenses/LicensesPage.tsx` | `/licenses` | L |
| 5.2 | `licenses/ProductsPage.tsx` | `/licenses/products` | L |
| 5.3 | `licenses/VendorsPage.tsx` | `/licenses/vendors` | L |
| 5.4 | `subscriptions/SubscriptionsPage.tsx` | `/subscriptions` | L |
| 5.5 | `ipam/SubnetTreePage.tsx` | `/ipam` | L |
| 5.6 | `ipam/SubnetDetailPage.tsx` | `/ipam/subnets/:subnetId` | F |
| 5.7 | `ipam/DriftPage.tsx` | `/ipam/drift` | L |
| 5.8 | `ipam/SearchPage.tsx` | `/ipam/search` | L |
| 5.9 | `ipam/RegistryPage.tsx` | `/ipam/registry` | FO |
| 5.10 | `ipam/ScanAuthorizationsPage.tsx` | `/ipam/scan-authorizations` | L |
| 5.11 | `ipam/ImportPage.tsx` | `/ipam/import` | O |
| + | `licenses/LicenseDetail.tsx` | `/licenses/:licenseId` | F |
| + | `licenses/ComplianceDashboard.tsx` | `/licenses/compliance` | O |
| + | `licenses/ReviewQueue.tsx` | `/licenses/review-queue` | L |
| + | `licenses/RenewalCalendar.tsx` | `/licenses/renewals` | O |
| + | `subscriptions/SubscriptionDetail.tsx` | `/subscriptions/:subscriptionId` | F |
| + | `subscriptions/SpendDashboard.tsx` | `/subscriptions/spend` | O |
| + | `subscriptions/RenewalCalendar.tsx` | `/subscriptions/renewals` | O |

**5.1 `LicensesPage.tsx` (`/licenses`, L) y sus pantallas.** `licenseNav` → `SectionNav` (licencias, productos, proveedores, cumplimiento, revisión, renovaciones). `search` (hoy `useState`) → `useUrlFilters(["q", "type", "client"])`; chip de tipo de licencia; `ClientFilter` (la ficha 360 enlaza aquí con `?client=`); cinco tiles; «Nueva licencia» con `useNewParam("license")` (`NewLicenseDialog`; la clave se ve una vez como hoy). Lectura: `useLicenses()` pasa a `useLicenses(chip = "")` exactamente como el ejemplo de la Receta L, punto 7; `VendorsPage` lo sigue llamando sin chip. `LicenseDetail`: `DetailHead` (producto, tipo como etiqueta, cliente), tiles de asientos (con derecho, en uso, libres), asignaciones debajo, `useRecordTitle`. **`ComplianceDashboard`** (`/licenses/compliance`, O): `PageHead` con su frase y `SectionNav`; sus paneles de hoy (`useCompliance()`), cada cifra un `StatCard` con `to` a la lista filtrada que la explica (las licencias de ese producto: `/licenses?q=<producto>`); un producto sin datos de uso dice «sin datos de uso», nunca `0` (Absent Zero); prueba «leads each figure to the list that explains it»; acepta: las mismas cifras de hoy. **`RenewalCalendar`** (`/licenses/renewals`, O): `PageHead` + `SectionNav`; la tabla de renovaciones de hoy (`useRenewals()`) agrupada por mes con `ListEmpty` «Ninguna licencia se renueva en este periodo.»; prueba «says why the calendar is empty»; acepta: ninguna fecha cambia de zona (la del perfil, como hoy). `ReviewQueue`: receta L con `BulkBar` donde hoy aprueba en lote. Pruebas: `__tests__/licenses.test.tsx` adaptadas; recetas L (con la del chip) y F (con la de título, `{ page: "shell.nav.item.licenses" }`). Acepta: `/licenses?client=<id>` (el enlace de la ficha 360) estrecha la lista; la clave de una licencia nueva se ve una sola vez.

**5.2 `ProductsPage.tsx` (`/licenses/products`, L, solo búsqueda).** Hoy: `useProducts()` sin filtros y `editing: ProductOut | "new" | null` en `useState` (`ProductsPage.tsx:41`), solo con `licenses.manage` en todos los clientes. Pasa a: `SectionNav`; `useUrlFilters(["q"])` con la búsqueda en el navegador sobre nombre y editor; `ShowingCount`; la alta con `useNewParam("product")` y la edición con `?edit=<id>`, en el diálogo de hoy con sus reglas de coincidencia, abiertos solo con `canManage`; vacío «Aún no hay productos.» con «Nuevo producto», o «nada coincide». Sin chip «Cliente» (los productos son de la organización). Pruebas: la variante de búsqueda de la Receta L; «says why the list is empty»; «opens a product's editor from ?edit=<id>». Acepta: las reglas de coincidencia se guardan igual que hoy.

**5.3 `VendorsPage.tsx` (`/licenses/vendors`, L, solo búsqueda).** Hoy: `useVendors()` sin filtros, la cuenta de licencias de cada proveedor sobre `useLicenses()`, y «Nuevo proveedor» en `useState` (`creating`, `VendorsPage.tsx:21`). Pasa a: `SectionNav`; `useUrlFilters(["q"])` con la búsqueda en el navegador sobre nombre y web; `ShowingCount`; «Nuevo proveedor» con `useNewParam("vendor")`, abierto solo con `canManage`; vacío con causa. Sin chip. Pruebas: la variante de búsqueda; «says why the list is empty»; «opens New vendor from ?new=vendor». Acepta: la cuenta de licencias por proveedor es la de hoy.

**5.4 `SubscriptionsPage.tsx` (`/subscriptions`, L) y sus pantallas.** Seis tiles; `search` → `useUrlFilters(["q", "cycle", "client"])`; chip de ciclo de facturación; `ClientFilter`; «Nueva suscripción» (`useNewParam("subscription")`). Lectura: `useSubscriptions()` (`subscriptions/api.ts:12`) pasa a `useSubscriptions(chip = "")`, Receta L, 7. Un menú de sección nuevo con `SectionNav` (suscripciones, gasto, renovaciones), que hoy no existe y deja `SpendDashboard` y `RenewalCalendar` alcanzables solo por enlaces sueltos (va en un archivo nuevo del módulo, `subscriptions/subscriptionsNav.tsx`). `SubscriptionDetail`: `DetailHead` (servicio, ciclo, cliente), tiles (coste, asientos, renovación), `useRecordTitle`; la edición de la ficha (ocho campos) con `useDraft` + `SaveBar` en lugar de su botón propio. **`SpendDashboard`** (`/subscriptions/spend`, O): `PageHead` + `SectionNav`; sus paneles de hoy (`useSpend()`, `useIdleSeats()`) con cada total un `StatCard` con `to` a la lista que lo explica; los importes en su moneda, nunca sumando monedas distintas; prueba «leads each total to the list that explains it»; acepta: las mismas cifras. **`RenewalCalendar`** (`/subscriptions/renewals`, O): `PageHead` + `SectionNav`; la ventana (`withinDays`, hoy `useState`, `RenewalCalendar.tsx:21`) pasa a `?within=<días>` con `FilterChips` de los `WINDOWS` de hoy; `ListEmpty` con causa; prueba «keeps the window in the URL»; acepta: las mismas renovaciones para la misma ventana. Pruebas: `__tests__/subscriptions.test.tsx` adaptadas; recetas L (con la del chip), F (título con `{ page: "shell.nav.item.subscriptions" }`) y FO. Acepta: `/subscriptions?client=<id>` estrecha la lista.

**5.5 `SubnetTreePage.tsx` (`/ipam`, L).** `IpamNav` → `SectionNav`. Hoy: `filters` en `useState` (VRF, sede, tipo…), `?register=1` abre el alta, `?state` la vista, once selects; es una página del recorrido de «Añadir un equipo» (`useAdoptJourneyClient`, que desde la Task 5 lee `?journey_client=` y `?journey_site=`, así que `site` queda libre para el filtro). Los filtros pasan a `useUrlFilters(["q", "vrf", "site", "kind", "state", "client"])` con chips donde los valores son pocos (tipo, estado) y `.chip-select` donde son muchos (VRF, sede); `?register=1` pasa a `?new=subnet` con `useNewParam("subnet")`, y `?register=1` se sigue leyendo una vez para no romper un enlace guardado (redirige a `?new=subnet` con `replace`; lo escriben hoy `DriftPage.tsx:564` y `SiteServerPrerequisites.tsx:52`, de dos oleadas, que así no cambian). Lectura: `useIpamSubnets(filters)` (`ipam/api.ts:281`) pasa a `useIpamSubnets(filters, chip = "")`, Receta L, 7. El árbol de subredes se conserva. Pruebas: `__tests__/ipam.test.tsx`, `subnet-actions.test.tsx` adaptadas; la del chip; nuevas «still opens the register dialog from an old ?register=1 link» y «narrows with ?client= and leaves the console's client alone» (con `?client=<ANDES>` se lee por `forClient(ANDES)` y `setSelectedClient` no se llama: decisión D20). Acepta: `/ipam?client=<id>` (el enlace de la ficha 360) estrecha el árbol sin cambiar el cliente de la consola; `/ipam?journey_client=<id>` lo sigue cambiando, como hoy hacía `?client=`.

**5.6 `SubnetDetailPage.tsx` (`/ipam/subnets/:subnetId`, F).** La única página con pestañas en `useState` de toda la consola (`const [tab, setTab] = useState<string>("addresses")`). Pasan a `useUrlTab(["addresses", "ranges", "sweeps", "drift"], "addresses")` y `UrlTabs`. `DetailHead` (prefijo, tipo, VRF y sede como contexto, acciones de hoy: barrer, registrar dirección). `useRecordTitle(prefijo)`. Pruebas: receta F (era la que faltaba: «puts the tab in the URL») y la de título con `{ page: "shell.nav.item.ipAddresses" }`. La variante `/ipam/subnets/subnet-1?tab=sweeps` ya está en `routes.mjs` (Task 5) y en la lista de rutas de arriba. Acepta: `?tab=sweeps` recarga en Barridos; barrer sigue siendo el mismo trabajo firmado.

**5.7 `DriftPage.tsx` (`/ipam/drift`, L).** Hoy: `?class` (la tira, `DriftPage.tsx:96`) y `?subnet` (`:95`) en la URL, el hallazgo abierto (`openId`) en `useState`, cuatro tiles con los conteos. Pasa a: `FilterChips` «Clase» sobre `class` con los conteos de esos cuatro tiles (salen de las respuestas de `useIpamDrift` y `useIpamOrphans`, que la página ya pide enteras: son totales); `.chip-select` de subred sobre `subnet`; `ShowingCount`; el hallazgo abierto en su panel con `?finding=<id>`; nota y «hasta» en el formulario de exención como hoy; vacío «No hay desviaciones: lo registrado y lo visto coinciden.» o «nada con estos filtros». Sin chip «Cliente» (las subredes de la lista ya son del cliente de arriba). Pruebas: las dos de la receta L sobre `class`; «opens a finding from ?finding=<id>». Acepta: reconocer un hallazgo manda lo mismo que hoy; el enlace «registrar la subred» sigue siendo `/ipam?register=1`.

**5.8 `SearchPage.tsx` (`/ipam/search`, L).** `query` (hoy `useState` con un borrador) pasa a `?q=` al enviar (el borrador sigue local hasta pulsar «Buscar», porque la búsqueda va al servidor); resultados con `ShowingCount`. Pruebas: `search-page.test.tsx` adaptadas.

**5.9 `RegistryPage.tsx` (`/ipam/registry`, FO).** VRF, sedes y VLAN: tres `FormSection`, cada una una tabla con su alta en diálogo (`useNewParam("vrf")`, `"vlan"`). Sin `SaveBar` (cada fila se guarda al confirmar). Receta L en cada tabla.

**5.10 `ScanAuthorizationsPage.tsx` (`/ipam/scan-authorizations`, L).** Es una página del recorrido (`?journey_client=` desde la Task 5). «incluir revocadas» (`includeRevoked`, hoy `useState`, línea 88) → chip `revoked`; «Conceder» (`useNewParam("grant")`, hoy `granting`) con prefijo, VRF, nota y caducidad; revocar con confirmación; `ShowingCount`; vacío con causa. Sin chip «Cliente»: una autorización de barrido es del cliente de arriba (invariante 2a(d)). Pruebas: `scan-authorizations-picker.test.tsx` adaptadas; las dos de la receta L sobre `revoked`; «opens Grant from ?new=grant». Acepta: conceder y revocar mandan lo mismo que hoy y quedan en el registro.

**5.11 `ImportPage.tsx` (`/ipam/import`, O).** El asistente de importación (archivo, columnas, mapeo, vista previa, destino): `Steps` del kit arriba, una `FormSection` por paso, «Simulación» y «Importar» como hoy; las exportaciones como acciones secundarias de la cabecera. `UnsavedGuard` mientras hay un archivo cargado sin importar.

- [ ] **Step 1:** pruebas nuevas; `npx vitest run src/modules/licenses src/modules/subscriptions src/modules/ipam` falla en ellas.
- [ ] **Step 2:** aplicar página por página (`licenses` y `subscriptions` por un implementador, `ipam` por otro si el coordinador lo reparte).
- [ ] **Step 3:** textos en los tres catálogos; sus `*.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8 (las críticas incluyen las siete pantallas extra).
- [ ] **Step 5: Commit** (`git add frontend/src/modules/{licenses,subscriptions,ipam} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o5.ts .impeccable/critique`; mensaje `feat(ui): wave 5, assets: licenses, subscriptions and IP addresses`, con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; `/licenses?client=<id>`, `/subscriptions?client=<id>` e `/ipam?client=<id>` (los enlaces de la ficha 360) abren la lista estrechada a ese cliente, cada una con su prueba; ninguna pestaña de la oleada vive en `useState`; harmony en verde en las 19 rutas.

---

### Task 11: Oleada O6, Documentación, informes, cambios, presupuesto, personas y credenciales (9 páginas)

Atera: «More › Knowledge Base» (283-295), «Reports › Classic Reports» (242-266), «Advanced Reports» (267-278), «Admin › Users and security › Technicians» (384-393) como referencia de personas. Módulos: `docs`, `change`, `budget`, `people`, `credentials`, `reporting` (este último sin `*Page.tsx`: dos pantallas extra).

**Files:**
- Modify: `frontend/src/modules/{docs,change,budget,people,credentials,reporting}/**`
- Modify: `frontend/src/i18n/{en,es}/{docs,change,budget,people,credentials,reporting}.ts`, `frontend/src/i18n/identical-ok.o6.ts`

**Rutas de la oleada:** `/docs,/changes,/changes/approvals,/changes/calendar,/changes/chg-1,/budget,/budget/planner,/budget/expenses,/budget/confirm,/budget/admin,/people,/people/unmatched,/people/person-maria,/credentials,/reports,/reports/patch-compliance`

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 6.1 | `docs/DocsPage.tsx` | `/docs` | L + O |
| 6.2 | `change/ChangesPage.tsx` | `/changes` | L |
| 6.3 | `budget/BudgetPlannerPage.tsx` | `/budget/planner` | FO |
| 6.4 | `budget/ExpensesPage.tsx` | `/budget/expenses` | L |
| 6.5 | `budget/ConfirmationQueuePage.tsx` | `/budget/confirm` | L |
| 6.6 | `budget/BudgetAdminPage.tsx` | `/budget/admin` | FO |
| 6.7 | `people/PersonPage.tsx` | `/people/:personId` | F |
| 6.8 | `people/UnmatchedPage.tsx` | `/people/unmatched` | L |
| 6.9 | `credentials/CredentialsPage.tsx` | `/credentials` | L |
| + | `budget/BudgetDashboard.tsx` | `/budget` | O |
| + | `change/ApprovalQueue.tsx` | `/changes/approvals` | L |
| + | `change/ChangeCalendar.tsx` | `/changes/calendar` | O |
| + | `change/ChangeDetail.tsx` | `/changes/:changeId` | F |
| + | `reporting/ReportCatalogue.tsx` | `/reports` | L |
| + | `reporting/ReportView.tsx` | `/reports/:reportKey` | O |

**6.1 `DocsPage.tsx` (`/docs`).** Hoy: `?article`, `?document`, búsqueda y categoría en `useState`, cinco tiles, editor en línea, registros y documentos subidos, versiones. La lista de artículos es receta L: `search` y `category` → `useUrlFilters(["q", "category", "client"])` con chips de categoría y `ClientFilter`; el artículo abierto (`?article=`) en `DetailGrid` (lista a la izquierda, lector a la derecha en escritorio; el lector solo, con volver, en móvil); el editor de un artículo con `useDraft` + `SaveBar` + `UnsavedGuard` (hoy pierde el texto si se navega fuera); guardar selecciona el artículo guardado con `setSearchParams` (`DocsPage.tsx:221-231`), que solo cambia la query: la guarda no pregunta por eso (decisión D16), y sí al ir a otra ruta con cambios; «Nuevo artículo» ya llega por `?new=article` (Task 4). Lectura de la lista: los `useQuery` directos de los artículos pasan la clave con `narrowing.client`, leen con `useApiFor` y esperan a `narrowing.ready`; guardar y subir siguen con `useApi()`. **El hallazgo del detector que ya hay** (`impeccable detect --json frontend/src/modules/docs/` sale con código 2 hoy: `broken-image` en `DocumentViewer.tsx:255`) es un falso positivo: el `src={url}` está en la línea siguiente, y el `<img>` es el visor de imágenes de un documento, con su propio razonamiento de seguridad en el comentario de encima. La oleada **no** lo «corrige»: lo anota en la crítica y pide al coordinador la excepción (`impeccable ignores add-value broken-image "*" --file "frontend/src/modules/docs/DocumentViewer.tsx" --reason "False positive: the src is on the next line (DocumentViewer.tsx:256); the img is the document image viewer." --shared`). Pruebas: `DocsPage.test.tsx`, `docsClient.test.tsx` adaptadas; recetas L (con la del chip) y FO sobre el editor; nueva «selects the saved article without asking to leave». Acepta: guardar un artículo no pregunta; salir del editor con cambios sí; `/docs?article=<id>` enlazable.

**6.2 `ChangesPage.tsx` (`/changes`, L) y sus pantallas.** Cuatro tiles; filtros de estado como chips (hoy los da `?state` implícito en la tabla); `ClientFilter`; «Nuevo cambio» por `useNewParam("change")` (Task 4); «Calendario» y «Aprobaciones» en un `SectionNav` nuevo. Lectura: `useChanges()` (`change/api.ts:52`) pasa a `useChanges(chip = "")`, Receta L, 7; `ApprovalQueue` lo sigue llamando sin chip. `ChangeDetail`: `DetailHead` (título, estado, riesgo, cliente; acciones de aprobación según el permiso de hoy), `UrlTabs` si tiene más de una sección; **su `document.title` propio** (`ChangeDetail.tsx:180-197` y la clave `change.detail.tabTitle`) **se sustituye por `useRecordTitle(change.title)`** y la clave se borra de los dos catálogos (el título lo compone `documentTitle`). **`ApprovalQueue`** (`/changes/approvals`, L): `SectionNav`; los cambios que esperan la decisión de la persona, con `ShowingCount`; aprobar y rechazar en su diálogo como hoy; vacío «Nada espera tu aprobación.»; pruebas: «says why the list is empty» y las de hoy; acepta: quien pidió un cambio no puede aprobarlo (la regla del servidor y el texto de hoy, `ChangeDetail.tsx:175-178`). **`ChangeCalendar`** (`/changes/calendar`, O): `PageHead` + `SectionNav`; la ventana de hoy (`useChangeCalendar`, con sus campos «Desde» y «Hasta») pasa a `?from=` y `?to=`; el vacío con su causa de hoy («No hay cambios programados en esta ventana.»); prueba «keeps the window in the URL»; acepta: las mismas horas en la zona del perfil. **Las pruebas del título** (revisión previa, W-I3): `change.es.test.tsx:417-448` monta `ChangeDetail` con su propio `mount` y afirma `document.title`; como `useRecordTitle` no hace nada fuera del armazón, ese `mount` envuelve ahora la ruta en `TitleShell` (`src/test/renderPage.tsx`): `mount(<TitleShell page="shell.nav.item.changes"><Routes>…</Routes></TitleShell>, …)`, y las dos aserciones pasan a `expect(document.title).toBe("Actualizar firmware del conmutador · Cambios · Novieri RMM");` y, tras el cambio de idioma, `expect(document.title).toBe("Actualizar firmware del conmutador · Changes · Novieri RMM");` (la mitad de la página es la etiqueta del riel, `es/shell.ts:32` «Cambios», y sigue al idioma). En `__tests__/change.test.tsx`, la de título usa `renderPage(…, { page: "shell.nav.item.changes" })`. Pruebas: además, recetas L (con la del chip) y F. Acepta: `ChangeDetail` ya no escribe `document.title` por su cuenta; la clave `change.detail.tabTitle` no existe en ningún catálogo.

**6.3 `BudgetPlannerPage.tsx` (`/budget/planner`, FO).** La rejilla de planificación (celdas por línea y mes, `cells` y `lineIds` en estado): `PageHead` con el presupuesto elegido como `.chip-select` (`?budget=<id>`), la rejilla dentro de un `Panel bare`, y `useDraft` sobre las celdas + `SaveBar` + `UnsavedGuard`; «Cerrar presupuesto» con su confirmación como hoy; «Nuevo presupuesto» con `useNewParam("budget")`. Presupuesto trabaja con un cliente cada vez (decisión de la fase 3): con «Todos mis clientes» la página pide elegir uno arriba, como hoy. Pruebas: `__tests__/budget.test.tsx` adaptadas; receta FO.

**6.4 `ExpensesPage.tsx` (`/budget/expenses`, L).** `statusFilter`, `categoryFilter`, `fromDate`, `toDate` (hoy `useState`, `ExpensesPage.tsx:96-99`) → `useUrlFilters(["status", "category", "from", "to"])`, con los mismos nombres de campo hacia la API (`status`, `category_id`, y las fechas); chips de estado (sin conteo); `.chip-select` de categoría; fechas en dos campos `date` dentro de la barra; `SectionNav` del presupuesto; «Nuevo gasto» (`useNewParam("expense")`); editar en diálogo con `?edit=<id>`; vacío con causa. Sin chip «Cliente» (presupuesto: un cliente cada vez). Pruebas: las dos de la receta L sobre `status`; «keeps the date range in the URL»; «opens New expense from ?new=expense». Acepta: con «Todos mis clientes» la página sigue pidiendo elegir uno arriba, como hoy.

**6.5 `ConfirmationQueuePage.tsx` (`/budget/confirm`, L, sin filtros).** Cola de gastos por confirmar (`useExpenses({ status: "expected" })`, `ConfirmationQueuePage.tsx:43`): `SectionNav` del presupuesto; `ShowingCount`; confirmar y anular con su diálogo, como hoy; vacío «No hay gastos por confirmar.». Pruebas: solo «says why the list is empty» (Receta L, 8) y las de hoy en consultas. Acepta: confirmar y anular mandan lo mismo que hoy; con «Todos mis clientes» pide elegir uno arriba.

**6.6 `BudgetAdminPage.tsx` (`/budget/admin`, FO).** Moneda base, mes de inicio, centros de coste, categorías y tipos de cambio (16 campos, cuatro diálogos). `FormSection` «General» (moneda y mes) con `useDraft` + `SaveBar`; tres `FormSection` con su tabla y su alta en diálogo (`useNewParam("costCenter")`, `"category"`, `"rate"`); la edición en línea de una fila (`editingId`) pasa a su diálogo. Pruebas: receta FO sobre «General».

**`BudgetDashboard.tsx` (`/budget`, O).** `PageHead` con el cliente elegido; tiles (cada uno con `to` a la página que lo explica: planificación, gastos o confirmar); el gráfico de previsto contra real (`components/chart.tsx`) en un `Panel`, que desde la Task 1 pinta la serie «Real» en el gris de desconocido y no en un segundo cobalto (D2); `SectionNav` del presupuesto (resumen, planificación, gastos, confirmar, administración) que sustituye a la tira `BudgetPageNav` de `budgetState.tsx`, con su mismo nombre exportado y sus mismos permisos; con «Todos mis clientes», `BudgetPickClient` como hoy. Pruebas: `__tests__/budget.test.tsx` adaptadas; nueva «leads each tile to the page that explains it». Acepta: las mismas cifras de hoy; el gráfico distingue sus tres series sin depender del color.

**6.7 `PersonPage.tsx` (`/people/:personId`, F).** `DetailHead` (nombre, cargo y departamento como contexto, «Editar» como acción); tiles de lo que tiene (equipos, licencias, asientos); siete paneles de hoy agrupados en `UrlTabs` (`equipment`, `licenses`, `tickets`, `history`); `useRecordTitle`. Pruebas: `__tests__/people.test.tsx` adaptadas; receta F y la de título con `{ page: "shell.nav.page.person" }` (la ficha vive bajo Equipos: espera `"<nombre> · Person · Novieri RMM"`). Acepta: los siete paneles de hoy siguen, cada uno en su pestaña; `?tab=history` recarga ahí.

**6.8 `UnmatchedPage.tsx` (`/people/unmatched`, L).** Hoy: tres tablas seguidas de lo que no tiene persona (`useUnmatched()`, una sola respuesta con las tres listas). Pasa a: `FilterChips` «Tipo» sobre `?kind=` (equipos, el valor `""`; licencias; asientos) **con su conteo**, que es el tamaño de cada lista de esa respuesta entera (un total, no una página); una sola tabla, la del tipo elegido; `ShowingCount`; asignar en `PersonPickerDialog` como hoy (solo con `people.manage`); vacío «Todo tiene persona.» por tipo. Pruebas: las dos de la receta L sobre `kind`; «assigns a seat to a person as today». Acepta: los tres conteos suman lo mismo que las tres tablas de hoy; asignar manda lo mismo.

**6.9 `CredentialsPage.tsx` (`/credentials`, L; decisión D23).** Hoy: `Tabs` en `?state`, y **`?new=<tipo>` abre «Nueva credencial» con ese tipo elegido** (`CredentialsPage.tsx:218-222, 478-488`): las páginas de instalación remota de la O1 enlazan a `/credentials?new=push_install` (`agent-push/PushReadiness.tsx:59`) y a `` `/credentials?new=${kind}` `` (`PushInstallFlow.tsx:196`). Eso **no cambia** (revisión previa, W-I6): `useNewParam(kind)` solo abre con un valor exacto, así que convertirlo dejaría esos enlaces sin abrir nada. La tira → `FilterChips` con `state`; el botón «Nueva credencial» usa `const created = useNewParam("credential")` y el diálogo se abre con `open={createOpen || created.open || newType != null}` (`newType`, el tipo de `?new=` cuando es uno de `CREDENTIAL_TYPES`, preselecciona como hoy); su `onClose` borra `new` como hoy. Cuatro tiles; `ClientFilter` (las globales llevan su chip «Para todos los clientes» como hoy); lectura: los `useQuery` directos de la lista pasan la clave con `narrowing.client` y leen con `useApiFor`; retirar una credencial pide el motivo como hoy (invariante 3: el secreto se ve una vez, nunca en una lista). Pruebas: `CredentialsPage.test.tsx` adaptadas; las de hoy de `?new=push_install` y `?new=root_password` (líneas 264-272) **sin tocar**; receta L; la del chip; nueva «New credential puts ?new=credential in the URL and opens with no type chosen». Acepta: `/credentials?new=push_install` abre el diálogo con «Push install» elegido; ninguna lista enseña un secreto.

**`ReportCatalogue.tsx` (`/reports`, L) y `ReportView.tsx` (`/reports/:reportKey`, O).** El catálogo: tarjetas por informe con búsqueda y chips de familia; `?access` se conserva. El informe: `PageHead` con volver «Informes», sus parámetros en una `FormSection` («Ejecutar» y «Exportar» como hoy), el resultado debajo; `useRecordTitle(nombre del informe)`. Pruebas: `__tests__/reporting.test.tsx` adaptadas.

- [ ] **Step 1:** pruebas nuevas; `npx vitest run src/modules/docs src/modules/change src/modules/budget src/modules/people src/modules/credentials src/modules/reporting` falla en ellas.
- [ ] **Step 2:** aplicar página por página.
- [ ] **Step 3:** textos en los seis catálogos; sus `*.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8 (con las seis pantallas extra).
- [ ] **Step 5: Commit** (`git add frontend/src/modules/{docs,change,budget,people,credentials,reporting} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o6.ts .impeccable/critique`; mensaje `feat(ui): wave 6, documentation, reports, changes, budget, people and credentials`, con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; el editor de artículos y la planificación del presupuesto avisan antes de salir, y guardar un artículo no; `ChangeDetail` ya no escribe `document.title` por su cuenta y sus dos pruebas de título esperan el texto compuesto en los dos idiomas; `/credentials?new=push_install` sigue abriendo el alta con su tipo; el falso positivo del detector en `DocumentViewer.tsx` queda pedido como excepción, no «corregido»; harmony en verde en las 16 rutas.

---

### Task 12: Oleada O7, Administración y cuenta (6 páginas)

Atera: «Admin › My account › Account settings» (345-359), «My profile» (360-368), «Admin › Users and security › Access roles» (403-423), «Security and authentication» (424-435), «AI Center» (308-327). Módulos: `settings`, `profile`, `ai-admin`, `ai-automation`. Pocas páginas, pero Configuración tiene doce pestañas, nueve de ellas formularios: es la oleada de la barra de guardado.

**Files:**
- Modify: `frontend/src/modules/{settings,profile,ai-admin,ai-automation}/**`
- Delete: `frontend/src/modules/settings/SaveBar.tsx` (decisión D22; nada lo importa fuera de esta oleada: `ProfilePage.tsx:38`, `AutonomyDialPage.tsx` y nueve pestañas de Configuración, todos de la O7)
- Modify: `frontend/src/i18n/{en,es}/{settings,profile,aiAdmin,aiAutomation}.ts`, `frontend/src/i18n/identical-ok.o7.ts`

**Rutas de la oleada:** `/settings,/settings?tab=users,/settings?tab=roles,/settings?tab=email,/settings?tab=microsoft,/settings?tab=sessions,/settings?tab=ai-connections,/profile,/profile/security,/portal/security,/assistant,/assistant/settings,/assistant/autonomy,/assistant/actions,/assistant/actions/act-1`

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 7.1 | `settings/SettingsPage.tsx` (y sus pestañas `*Tab.tsx`) | `/settings` | FO |
| 7.2 | `profile/ProfilePage.tsx` | `/profile` | FO |
| 7.3 | `profile/SecurityPage.tsx` | `/profile/security`, `/portal/security` | FO |
| 7.4 | `ai-admin/AiSettingsPage.tsx` | `/assistant/settings` | FO |
| 7.5 | `ai-automation/AutonomyDialPage.tsx` | `/assistant/autonomy` | FO |
| 7.6 | `ai-automation/ActionsPage.tsx` | `/assistant/actions`, `/assistant/actions/:actionId` | L, F |
| + | `ai-admin/ChatPanel.tsx` | `/assistant` | O |

**7.1 `SettingsPage.tsx` (`/settings`, FO).** Ya tiene `?tab=` y una botonera de chips (diseño de ajustes de 2026-09-10). La botonera pasa a `UrlTabs` (una fila que se desplaza) con las mismas claves. Son **doce** pestañas (`settings/tabs.ts:14-27`): `organization`, `email`, `roles`, `users`, `sessions`, `microsoft`, `assistant`, `ai-connections`, `notifications`, `sla`, `health` y `budget`.
- **Las nueve que hoy guardan con `settings/SaveBar.tsx`** (`OrganizationTab`, `EmailTab`, `RolesTab`, `SessionsTab`, `MicrosoftTab`, `AssistantTab`, `ServiceLevelsTab`, `HealthTab`, `BudgetTab`) pasan a la `SaveBar` del kit, una por pestaña, con `UnsavedGuard dirty={dirty} search` (decisión D16: en Configuración la query **es** lo que se edita, porque cada pestaña es otro formulario, así que cambiar de pestaña con un cambio sin guardar pregunta). Lo que la barra de ajustes hacía y la del kit también hace desde la Task 3: «Guardado.» tras el 2xx (`saved`, el `mutation.isSuccess` de hoy, nunca optimista), los problemas del formulario (`problems`), Intro en un campo (`submit` dentro del `Form` de la pestaña) y la razón cuando no se puede guardar (`blockedReason`). **`requireDirty={false}`** de hoy (Organización tomaba el clic con el formulario limpio para decir «no hay cambios») desaparece: con la barra del kit no hay botón que pulsar sin cambios, que es la forma de no tener un control muerto. **`canEdit`/`disabledReason`** (una persona sin el permiso): sus campos siguen desactivados y la razón pasa a un `notice` encima de la sección, porque sin cambios no hay barra donde decirlo. Las que piden un segundo factor reciente para guardar (correo, roles: own-auth fase 2) lo siguen pidiendo desde `onSave` (`useStepUp`).
- **`notifications`** guarda hoy cada canal en su fila (`NotificationsTab.tsx:401-407`) y alterna las reglas en el sitio: la edición de un canal pasa a su diálogo (se guarda al confirmarlo) y el interruptor de una regla sigue guardando al pulsarlo, como hoy; sin `SaveBar`.
- **`users`** es una lista (receta L: búsqueda, chips de estado, `BulkBar` donde hoy hay acciones en lote, `?new=user` para invitar); **`ai-connections`** es una lista de clientes de IA registrados, con revocar en su confirmación (invariante 1c(d)), sin `SaveBar`.
- Los campos conservan su `help` fuera de la etiqueta (prueba que ya existe).
- Pruebas: los **dieciocho** archivos de `modules/settings/__tests__/` (17 pruebas y `harness.tsx`) adaptados: donde una prueba pulsa el «Save changes» de una pestaña, pulsa el «Save» de la barra; donde espera «Saved.», lo espera en la barra (`role="status"`); donde espera el botón desactivado con su razón para quien no puede editar, espera el `notice` con esa razón y ninguna barra (el diseño cambió dónde se dice, y el comentario lo cita); la de Organización que pulsaba «Save» sin cambios para oír «no hay cambios» pasa a afirmar que sin cambios no hay barra. Nuevas: receta FO en organización y correo («asks before leaving with an unsaved organization name», «switching tab with an unsaved change asks first»).
- Acepta: ninguna pestaña tiene un botón «Guardar» propio; cambiar de pestaña con cambios pregunta; el segundo factor se sigue pidiendo donde hoy.

**7.2 `ProfilePage.tsx` (`/profile`, FO).** Nombre, idioma y datos de contacto: `FormPage` sin volver (se llega del menú de la cuenta; la miga dice «Tu cuenta»), una `FormSection`, la `SaveBar` del kit en lugar de `settings/SaveBar` (`ProfilePage.tsx:38, 206`; decisión D22), con `saved`. El idioma se aplica al guardar, como hoy (`languagePicked`). Pruebas: `ProfilePage.test.tsx`, `ProfilePage.language.test.tsx`, `profile.es.test.tsx` adaptadas (la línea 100 de `ProfilePage.test.tsx` habla de `requireDirty={false}` en Configuración: su comentario pasa a decir que ninguna de las dos lo tiene ya); receta FO. Acepta: el idioma cambia al guardar y no antes; «Guardado.» tras el 2xx.

**7.3 `SecurityPage.tsx` (`/profile/security` y, en el portal, `/portal/security`, FO).** Contraseña, llaves de acceso, segundo factor, códigos de recuperación, sesiones (diez paneles, siete diálogos, cuatro primarios). `FormSection` por tema, en este orden: cómo inicias sesión (contraseña, llaves), segundo factor, recuperación, sesiones abiertas; cada acción conserva su diálogo y su confirmación, y los primarios bajan a uno por sección. En el portal la página va en `PortalShell` como hoy. Pruebas: `SecurityPage.test.tsx`, `.contact.test.tsx`, `.microsoft.test.tsx`, `.passkeyGesture.test.tsx` adaptadas (solo consultas).

**7.4 `AiSettingsPage.tsx` (`/assistant/settings`, FO) y `ChatPanel.tsx` (`/assistant`, O).** Proveedor, salida a internet y marcas de inyección: `FormSection` por grupo con `useDraft` + `SaveBar`. El chat conserva su panel; `PageHead` «Asistente» con «Ajustes» como acción secundaria para quien tiene `ai.admin`; si el asistente está apagado, el estado vacío dice la causa y a quién pedirlo (spec §10.6). Pruebas: `__tests__/aiAdmin.test.tsx` adaptadas; receta FO.

**7.5 `AutonomyDialPage.tsx` (`/assistant/autonomy`, FO).** El dial de autonomía ya tiene borrador, confirmación y «guardado» (con `settings/SaveBar`, `AutonomyDialPage.tsx:198`): pasa a `useDraft` + la `SaveBar` del kit (decisión D22); «Guardar» abre la `ConfirmDialog` de hoy (subir la autonomía es un acto que se confirma). Pruebas: `__tests__/aiAutomationPrimitives.test.tsx` adaptadas. Acepta: subir la autonomía sigue pidiendo confirmación y queda en el registro (invariante 4); ninguna IA conectada por MCP puede hacerlo por su cuenta más allá de lo que 1c permite a la persona.

**7.6 `ActionsPage.tsx` (`/assistant/actions`, L; `/assistant/actions/:actionId`, F).** La lista de acciones del asistente: chips de estado (propuestas, aprobadas, ejecutadas, rechazadas), `ShowingCount`. La acción: `DetailHead` con su estado, la vista previa (`ActionPreview`) y aprobar / rechazar con confirmación como hoy; `useRecordTitle`. Pruebas: `__tests__/actionsPage.test.tsx` adaptadas; recetas L y F.

- [ ] **Step 1:** pruebas nuevas; `npx vitest run src/modules/settings src/modules/profile src/modules/ai-admin src/modules/ai-automation` falla en ellas.
- [ ] **Step 2:** aplicar página por página, Configuración pestaña por pestaña.
- [ ] **Step 3:** textos en los cuatro catálogos; sus `*.es.test.tsx` en verde.
- [ ] **Step 4:** «Cierre de cada oleada», pasos 1 a 8.
- [ ] **Step 5: Commit** (`git add -A frontend/src/modules/{settings,profile,ai-admin,ai-automation} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o7.ts .impeccable/critique`, con `-A` para que el borrado de `settings/SaveBar.tsx` entre; mensaje `feat(ui): wave 7, settings, your account and the assistant on one save bar`, con las dos líneas de atribución).

**Criterios de aceptación:** cada página cumple su «Acepta:»; ninguna pantalla de la oleada tiene más de una forma de guardar, y `settings/SaveBar.tsx` ya no existe (`grep -rn "settings/SaveBar\|from \"./SaveBar\"\|from \"../settings/SaveBar\"" frontend/src` sin resultados); cambiar de pestaña en Configuración con cambios pide confirmación; harmony en verde en las 15 rutas.

---

### Task 13: Oleada O8, Acceso, consentimiento y alta del dueño (12 páginas)

Diseño §4: «las pantallas de acceso y el consentimiento OAuth reciben la misma marca dentro de su propio marco». Atera no sirve aquí (sus pantallas de acceso no son parte del recorrido). Módulos: `auth`, `setup`. Las doce páginas comparten un marco, `AuthCard.tsx`: casi todo el cambio está en él.

**Files:**
- Modify: `frontend/src/modules/auth/**`, `frontend/src/modules/setup/**`
- Modify: `frontend/src/i18n/{en,es}/{auth,setup}.ts`, `frontend/src/i18n/identical-ok.o8.ts`
- `frontend/src/preview/PreviewPage.tsx` **no** se toca: la Task 3 (Step 10) le dejó la sección «AuthCard embedded», que dibuja el marco y lo dibujará nuevo en cuanto cambie `AuthCard.tsx`.

**Rutas de la oleada:** `/sign-in,/sign-in/factor,/sign-in/enroll,/sign-in/recovery-codes,/sign-in/microsoft,/forgot-password,/reset-password,/invite,/recover,/portal/sign-in` (y `/oauth/authorize`, que la puerta no recorre: `EXCLUDED`, porque necesita la petición de una aplicación de IA; se prueba en jsdom y se fotografía a mano con el servidor de desarrollo)

| # | Archivo | Ruta | Receta |
|---|---|---|---|
| 8.1 | `auth/SignInPage.tsx` | `/sign-in` | O (marco) |
| 8.2 | `auth/FactorPage.tsx` | `/sign-in/factor` | O |
| 8.3 | `auth/EnrollPage.tsx` | `/sign-in/enroll` | O |
| 8.4 | `auth/RecoveryCodesPage.tsx` | `/sign-in/recovery-codes` | O |
| 8.5 | `auth/MicrosoftCallbackPage.tsx` | `/sign-in/microsoft` | O |
| 8.6 | `auth/ForgotPasswordPage.tsx` | `/forgot-password` | O |
| 8.7 | `auth/ResetPasswordPage.tsx` | `/reset-password` | O |
| 8.8 | `auth/InvitePage.tsx` | `/invite` | O |
| 8.9 | `auth/RecoverPage.tsx` | `/recover` | O |
| 8.10 | `auth/PortalSignInPage.tsx` | `/portal/sign-in` | O |
| 8.11 | `auth/ConsentPage.tsx` | `/oauth/authorize` | O |
| 8.12 | `setup/SetupPage.tsx` | (sin ruta: `SetupGate` la dibuja en cualquier dirección mientras la instalación no tiene dueño) | O |

**El marco (`AuthCard.tsx`).** Hoy: una tarjeta de 480 px sobre el fondo, el titular, la introducción, el contenido y los dos botones de idioma; sin marca. Pasa a:
- arriba de la tarjeta, fuera de ella, `NovieriLockup height={24}` centrado (el conjunto «NOVIERI | RMM» en tinta y su descriptor en la tinta secundaria);
- la tarjeta igual (10 px, la sombra, 32 px de relleno), su titular en el escalón H1;
- el título de la pestaña: cada pantalla de acceso se llama «`<titular>` · Novieri RMM» (hoy todas se llaman igual que `index.html`). Un componente interno `function TabTitle({ page }: { page: string }) { useDocumentTitle(page); return null; }` se dibuja dentro de `AuthCard` solo cuando no es `embedded` (en `/__preview` la tarjeta no es la página y no debe renombrar la pestaña);
- abajo, el selector de idioma de hoy y una línea en la tinta secundaria con el nombre del producto (`PRODUCT_NAME`), nada más (Evidence on Hand: sin lemas ni afirmaciones);
- los estilos en línea de hoy (`GROUND`, `CARD`, `HEADLINE`, `SECTION_HEADING`) **se quedan en línea**, con tokens (`var(--…)`), y no pasan a un `auth.css` (revisión previa, W-M3): `jsx-tokens.test.ts` los guarda donde están, mientras que `contrast.test.ts` y las demás guardas de CSS solo leen `src/shell/*.css` e `index.css`, así que moverlos a `modules/auth/` quitaría una guarda sin poner otra. La puerta de armonía los mide igual en el navegador.

Cada página conserva sus textos, sus llamadas y sus errores; solo usa el marco. Detalles por página:
- **8.1 `SignInPage`**: correo y contraseña en un `Field` cada uno; «Iniciar sesión» primario a lo ancho; «Iniciar sesión con Microsoft» secundario debajo con su separador «o»; «¿Olvidaste la contraseña?» como enlace.
- **8.2 `FactorPage`**: el código de seis cifras con `inputMode="numeric"` como hoy; «recordar este equipo» y «usar un código de recuperación» como enlaces de texto, no botones. Prueba: «names the screen in the tab» (`"<titular de hoy> · Novieri RMM"`, el titular que la página ya pasa a `AuthCard`); las de hoy en consultas (un enlace de texto se busca como `link` donde hoy era `button`, con el comentario que cita el diseño §4). Acepta: el código se envía igual y un error se dice igual.
- **8.3 `EnrollPage`**: los dos caminos (llave de acceso, app de autenticación) como dos `AuthSection` con su titular H2; el QR y la clave como hoy. Prueba: «names the screen in the tab»; «draws the two ways to enrol as two sections, each with its own heading». Acepta: el secreto TOTP y el QR se enseñan como hoy, una vez.
- **8.4 `RecoveryCodesPage`**: los códigos en una rejilla monoespaciada; «Copiar» y «Ya los guardé» (dos primarios hoy: «Ya los guardé» queda como único primario y «Copiar» pasa a secundario). Prueba: «names the screen in the tab»; «keeps one primary action». Acepta: los códigos se ven una vez y se copian igual.
- **8.5 `MicrosoftCallbackPage`**: el estado de espera y el error con `AuthAlert`. Prueba: «names the screen in the tab»; la de hoy del error, en consultas. Acepta: el código de Microsoft se consume una vez, como hoy.
- **8.6 `ForgotPasswordPage`**, **8.7 `ResetPasswordPage`**, **8.8 `InvitePage`**, **8.9 `RecoverPage`**: formularios cortos en el marco; `NewPasswordFields` sin cambios. Prueba en cada una: «names the screen in the tab»; las de hoy en consultas. Acepta en cada una: el mismo envío, el mismo texto de error, y el token del enlace se sigue leyendo del fragmento (`#<token>`), nunca de la ruta.
- **8.10 `PortalSignInPage`**: igual que 8.1, con el titular del portal. Prueba: «names the screen in the tab» con el titular del portal. Acepta: entra al portal, no a la consola, como hoy.
- **8.11 `ConsentPage`** (`RequireAuth bare`): la aplicación que pide acceso, lo que podrá hacer («todo lo que permite tu rol», texto de la Task 2 con «Novieri RMM»), «Aprobar» primario y «Rechazar» secundario; el aviso de seguridad (invariante 1c) en un `notice` de advertencia, en la tarjeta, junto al control (copy voice: la afirmación de seguridad va en el control, no recortada).
- **8.12 `SetupPage`**: el alta del primer dueño en el marco, con el conjunto arriba; `useDocumentTitle`.

- [ ] **Step 1:** pruebas nuevas en `modules/auth/__tests__/authCard.test.tsx` (nuevo):

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthCard } from "../AuthCard";

describe("the sign-in frame (redesign design §4)", () => {
  it("carries the Novieri RMM lockup above the card", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(screen.getByRole("img", { name: "Novieri RMM" })).toBeInTheDocument();
  });

  it("names the screen in the browser tab", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(document.title).toBe("Sign in · Novieri RMM");
  });

  it("keeps one headline, at the page-title rung", () => {
    render(<AuthCard title="Sign in">x</AuthCard>);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});
```

y en `modules/setup/__tests__/setup.test.tsx`, «draws the lockup and names the page in the tab». Ejecutar `npx vitest run src/modules/auth src/modules/setup`: fallan las nuevas.
- [ ] **Step 2:** el marco (`AuthCard.tsx`), y después las doce páginas una a una; `npx vitest run src/modules/auth src/modules/setup src/App.routing.test.tsx` en verde tras cada una.
- [ ] **Step 3:** textos (si alguno cambia) en `auth` y `setup`, en los dos idiomas; `auth.es.test.tsx`, `setup.es.test.tsx` en verde.
- [ ] **Step 4:** `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs` en verde (mide el marco nuevo en la sección `AuthCard embedded` de `/__preview`).
- [ ] **Step 5:** «Cierre de cada oleada», pasos 1 a 8, con las diez rutas de arriba; `/oauth/authorize` se fotografía con `npx vite` y una petición de prueba (`/oauth/authorize?client_id=…` con los valores de `modules/auth/__tests__/consent.test.tsx`) y se adjunta al informe.
- [ ] **Step 6: Commit** (`git add frontend/src/modules/{auth,setup} frontend/src/i18n/en frontend/src/i18n/es frontend/src/i18n/identical-ok.o8.ts .impeccable/critique`; mensaje `feat(ui): wave 8, sign-in, consent and owner setup in the Novieri frame`, con las dos líneas de atribución).

**Criterios de aceptación:** las doce pantallas llevan el conjunto «Novieri RMM» y su título de pestaña; ninguna cambia una llamada ni un texto de error; harmony en verde en las diez rutas en los dos idiomas.

---

### Task 14: Revisión de toda la rama y camino a `main`

Diseño §5 («al final hay una revisión de toda la rama») y la decisión del operador «Las dos líneas en paralelo… llegan a `main` en una sola PR».

**Files:**
- Ninguno nuevo. Esta tarea solo corrige lo que la revisión encuentre, en el archivo que lo tenga, con su prueba.

- [ ] **Step 1: Las puertas, enteras, sobre la rama con las ocho oleadas fusionadas**

```bash
cd frontend
npx vitest run
npm run typecheck
npm run build
node scripts/harmony.mjs --strict-fixtures --lang en
node scripts/harmony.mjs --strict-fixtures --lang es
node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs
```

Expected: vitest con **más de 365 archivos y más de 7200 pruebas** y 0 fallos (la línea base del 2026-11-23 más las de este plan); las dos pasadas de armonía terminan en `ok …` sobre todas las rutas de `routes.mjs`: **127** (las 117 de hoy y las diez variantes de la Task 5: cuatro de la ficha 360, dos de la ficha del equipo, tres de Alertas y una de la subred), y ninguna con `drew no shell` ni con un 5xx del servidor de desarrollo (Task 1, Step 12); `a11y-browser` con código 0.

- [ ] **Step 2: Las guardas de alcance**

```bash
git diff --stat origin/main...HEAD -- backend agent deploy .github
git diff --name-only origin/main...HEAD | grep -v -E '^(frontend/|DESIGN\.md$|PRODUCT\.md$|\.impeccable/|docs/product\.md$|docs/superpowers/(plans|specs)/2026-11-23-rediseno-novieri)'
cd frontend && npx vitest run src/test/no-itms.test.ts src/lib/noVendorNames.test.ts
for d in src/modules/*/; do ../.claude/skills/impeccable/scripts/impeccable detect --json "$d" > /dev/null || echo "findings: $d"; done
```

Expected: los dos primeros comandos no imprimen nada (este proyecto no tocó el territorio del proyecto 4, y todo lo que tocó es de su territorio: `frontend/`, `DESIGN.md`, `PRODUCT.md`, `.impeccable/` y `docs/product.md`, revisión previa C-1); las dos guardas en verde; el detector de impeccable sin hallazgos. Un falso positivo que la crítica de su oleada justificó no se arregla en el código ni se apunta en `.impeccable/critique/ignore.md` (que solo filtra la crítica): el coordinador lo registra en `.impeccable/config.json` con `impeccable ignores add-value <regla> <valor> --file <ruta> --reason "<por qué>" --shared` (revisión previa, W-I14), que es lo que el detector lee. El que ya existe hoy (`broken-image` en `docs/DocumentViewer.tsx:255`) es de la O6 (Task 11, 6.1); hasta que su excepción esté, la línea `findings: src/modules/docs/` es la esperada y ninguna otra.

- [ ] **Step 3: El censo de páginas y de sus críticas**

Las críticas se buscan por su nombre, que sale de la ruta relativa (`critique-storage slug`), y no con `critique-storage latest`: una crítica escrita en `../rmm-w<N>` lleva en su cabecera la ruta absoluta de esa worktree, y `latest` sale con código 2 desde otro checkout (comprobado el 2026-11-24), igual que para una página que nunca tuvo crítica; un 2 no prueba nada. Desde la raíz del repositorio:

```bash
cd frontend && find src -name "*Page.tsx" -not -name "*.test.tsx" | wc -l && cd ..
IMP=.claude/skills/impeccable/scripts/impeccable
for f in $(cd frontend && find src -name "*Page.tsx" -not -name "*.test.tsx" | sort) \
  src/modules/email-intake/OAuthCallback.tsx src/modules/patch/PatchPolicyEditor.tsx src/modules/remote/ScreenViewer.tsx \
  src/modules/remote/RecordingPlayer.tsx src/modules/backup/BackupPolicyEditor.tsx src/modules/bcp/PlanDetail.tsx \
  src/modules/bcp/TestRecorder.tsx src/modules/bcp/BcpReport.tsx src/modules/licenses/LicenseDetail.tsx \
  src/modules/licenses/ComplianceDashboard.tsx src/modules/licenses/ReviewQueue.tsx src/modules/licenses/RenewalCalendar.tsx \
  src/modules/subscriptions/SubscriptionDetail.tsx src/modules/subscriptions/SpendDashboard.tsx src/modules/subscriptions/RenewalCalendar.tsx \
  src/modules/budget/BudgetDashboard.tsx src/modules/change/ApprovalQueue.tsx src/modules/change/ChangeCalendar.tsx \
  src/modules/change/ChangeDetail.tsx src/modules/reporting/ReportCatalogue.tsx src/modules/reporting/ReportView.tsx \
  src/modules/ai-admin/ChatPanel.tsx src/shell/Sidebar.tsx src/shell/AppShell.tsx; do
  s=$("$IMP" critique-storage slug "frontend/$f")
  last=$(ls .impeccable/critique/*__"$s".md 2>/dev/null | sort | tail -1)
  if [ -z "$last" ]; then echo "no critique: frontend/$f"; elif ! grep -q '^closed: true' "$last"; then echo "not closed: frontend/$f"; fi
done
```

Expected: la primera línea imprime `86`; el bucle no imprime nada: cada una de las 86 páginas, de las 22 pantallas extra (Apéndice B) y de los dos archivos del armazón (Task 4) tiene una crítica versionada, y **la última** de cada una está cerrada por su pulido. `PreviewPage.tsx` entra en el censo como página; su crítica es la de la Task 3. Una línea `no critique:` o `not closed:` es un defecto de la oleada de esa página. No se cuenta `ls .impeccable/critique | wc -l`: hay un archivo por ejecución, no por página, y además `ignore.md`.

- [ ] **Step 4: Revisión de la rama** (superpowers:requesting-code-review), contra el diseño, punto por punto:
  - §1.1 el orden del riel, las carpetas, los anclados, Administración en acordeón con volver y búsqueda, los permisos iguales (`nav.test.ts` «keeps every code the rail had»);
  - §1.2 «Nuevo» con destinos reales, la búsqueda ensanchada, el selector sin cambio de comportamiento (`ClientSelector.test.tsx` sin cambios y en verde), el indicador de alertas, la cuenta, y el Asistente fuera de la barra, anclado y en su botón redondo (D12);
  - §1.3 la ficha 360, las pestañas de una línea, el chip «Cliente» (cada lista que lo lleva tiene su prueba de estrechado, y `?client=` no cambia el cliente de la consola en ninguna página: D20);
  - §2 que ninguna página dibuja su propia lista, ficha, pestañas o barra de guardado (`grep -rn "className=\"tabs\"" frontend/src/modules` vacío; `grep -rln "<Tabs" frontend/src/modules` solo donde la pestaña es de contenido; `settings/SaveBar.tsx` ya no existe);
  - §3 los tokens (`tokens.test.ts`), el segundo portador de «en curso» (`second-carrier.test.tsx`), el logotipo, el favicon, el título;
  - §4 las 86 páginas y las 22 pantallas extra (Apéndice B), el portal, el acceso y el consentimiento;
  - §5 las pruebas nuevas del Apéndice C, y que ninguna prueba se borró: `git log --diff-filter=DR --name-status --format= origin/main..HEAD -- frontend/src | grep '\.test\.'` imprime solo renombrados (`R…`), ninguna `D`, y el revisor lee el diff de cada archivo de prueba cambiado **y de cada renombrado** buscando un `it(` quitado o un `.skip` (revisión previa, W-M5: un archivo renombrado y vaciado no sale con `D`).

- [ ] **Step 5: Camino a `main`** (el operador despliega siempre desde `main`, CLAUDE.md 2026-09-11)
  1. `git fetch origin && git merge origin/main` en la rama del proyecto 3 (no `rebase`: la rama son ocho fusiones de oleada, y un `rebase` las aplanaría y obligaría a resolver de nuevo lo que cada fusión ya resolvió; revisión previa, W-M1). Resolver los choques: no debería haber en código, porque el proyecto 4 no toca `frontend/` salvo lo que lista el Apéndice E.
  2. El coordinador une la rama del proyecto 4 y la de este en una sola rama de PR (decisión del operador); `npx vitest run`, `npm run typecheck`, `npm run build` y las puertas del proyecto 4 en verde sobre la unión.
  3. Abrir **una** PR hacia `main` con las dos líneas, con el cuerpo que termina en la línea de atribución de PR de esta sesión. La CI completa (`frontend`, `a11y`, `harmony` en y es, `harmony-gate`, y los trabajos del proyecto 4) en verde.
  4. El operador revisa las capturas de `harmony-en` y `harmony-es` (artefactos de la CI) y fusiona.

- [ ] **Step 6: Lo que el informe de despliegue debe decir de este proyecto**

Este proyecto **no añade** migraciones, claves de `.env`, cambios de OpenBao ni servicios: cambia la imagen `web` (la consola) y nada más. El informe de despliegue de la PR conjunta lo escribe el coordinador, con la secuencia completa del proyecto 4 (el VPS nuevo, `rmm.novieri.com`) en su orden. Si la consola se desplegara antes en el servidor de hoy, la secuencia completa es esta, desde `main` ya fusionado (la ruta es la de `deploy/README.md`):

```sh
cd /opt/it-suite/deploy
docker compose exec openbao bao status
# Si la línea "Sealed" dice "true", desellar con tres claves distintas (cada orden pide una):
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao status          # "Sealed false"
docker images --format '{{.Repository}}:{{.Tag}}' | grep '^itsuite/web:' | head -5   # las etiquetas a las que se podría volver
git -C .. pull --ff-only
./scripts/deploy.sh --plan                      # debe decir que reconstruye web; migrate no encuentra nada nuevo de este proyecto
./scripts/deploy.sh
```

Qué comprobar después: `./scripts/deploy.sh` termina diciendo que la etiqueta desplegada es `HEAD`; en el navegador, la pestaña de `/dashboard` dice «Inicio · Novieri RMM» en español, el favicon es la N cobalto, el riel está en el orden del diseño, el Asistente es el botón redondo de abajo a la derecha y «Nuevo» abre sus entradas. **Vuelta atrás:** la línea `docker images … | grep '^itsuite/web:'` de arriba, ejecutada **antes** del despliegue, lista las etiquetas que ya existen en ese servidor; si no imprime nada, no hay etiqueta anterior a la que volver y la única vuelta atrás es restaurar desde copia (`deploy/README.md`, «Rollback»). No se usa `./scripts/build-images.sh` para saberlo: construye las dos imágenes, y su «(none yet)» no puede salir nunca, porque el estado de esa tubería es el de `head` (revisión previa, W-M2). Si hay etiquetas, el informe del coordinador nombra la anterior con la orden que la calcula, nunca con un marcador para rellenar.

**Criterios de aceptación:** todas las puertas en verde sobre la unión, con las 127 rutas de harmony; cada página y cada pantalla extra con su crítica versionada y cerrada; ninguna línea fuera de `frontend/`, `DESIGN.md`, `PRODUCT.md`, `.impeccable/` y `docs/product.md` (y este plan y su diseño); el revisor de la rama aprueba punto por punto.

---

## Apéndice A: decisiones del plan

| # | Decisión | Por qué | Dónde |
|---|---|---|---|
| D1 | La autoridad de los tokens pasa del prototipo v3 a `DESIGN.md` v4 (frontmatter con los nombres de las propiedades CSS), y `novieri-tokens.json` fija la marca | El diseño §3.2: «Una regla que la marca obligue a cambiar se cambia primero en `DESIGN.md`»; el prototipo v3 no tiene la marca | Task 1 |
| D2 | `--ink-2` → `--ink`, `--line-2` → `--line`, `--sev-run` → el acento (con su icono propio); donde no hay etiqueta con icono, un segundo portador: la serie 2 de los gráficos en `--sev-mut`, la celda de calor de información con anillo, el donut con el icono de su tipo (la línea de tiempo ya lo tenía) | La marca tiene dos tintas, un borde y la información en el tono del acento; el operador confirmó el 2026-11-24 que «en curso» se funde en el cobalto; sin segundo portador, una dirección DHCP y una asignada serían el mismo cuadro (WCAG 1.4.1; revisión previa, I-3) | Task 1 |
| D3 | El contador de alertas del riel es una etiqueta crítica con texto blanco; el activo lleva `--rail-mark` y un punto, no un borde de acento | Tabla de contraste de la marca; el acento mide 2,32:1 sobre el riel | Task 1 |
| D4 | `--r-pill` solo para el interruptor y la barra de progreso | La marca retira la píldora de chips y etiquetas; esas dos formas se reconocen por su silueta | Task 1 |
| D5 | `--accent-2` = hover de la marca; nuevo `--accent-3` = presionado | Valores de la marca | Task 1 |
| D6 | El chip «Cliente» y la ficha 360 leen con `X-Client-Id` (`ApiClient.forClient`), sin parámetros nuevos en el servidor; solo las lecturas, las escrituras siguen con `useApi()` | Estrechar es lo que la base de datos ya impone (5a); evita once cambios de backend del proyecto 4 | Task 3 |
| D7 | Router de datos (`createBrowserRouter`) | `useBlocker`, necesario para avisar antes de salir, solo existe ahí | Task 3 |
| D8 | `?new=<tipo>` abre el diálogo de alta de una lista | Cada entrada de «Nuevo» es una URL real; un alta se enlaza | Task 3 |
| D9 | «Activos» y «Más» son carpetas que se abren en el sitio; plegado el riel, sus páginas son iconos | Diseño §1.1; ningún destino a más de un clic | Task 4 |
| D10 | «Administración» cambia el riel por su panel sin navegar; `/admin` sigue siendo la portada | Diseño §1.1 y Atera; la miga sigue teniendo destino | Task 4 |
| D11 | «Añadir un equipo» sale de la barra y entra en «Nuevo → Equipo» y en la cabecera de Equipos; «Nuevo» es el único primario de la barra | Diseño §1.2 | Task 4 |
| D12 | El Asistente sale de la barra superior, queda anclado al pie del riel y es además un botón redondo flotante (48 px) abajo a la derecha en toda página, con `ai.use`, que enlaza a `/assistant`, no se dibuja en `/assistant` y nunca tapa la barra de guardado (la barra le deja 72 px, 64 en un teléfono) | Respuesta del operador del 2026-11-24, «Como queda en las capturas de atera» (`atera 1.png`; el recorrido, líneas 22, 82, 87 y el defecto de la 639); sustituye la regla del 2026-09-08 | Task 4 |
| D13 | En español, «Inicio», «Equipos» y «Documentación» en el riel y en sus páginas; el glosario lo anota; los textos que nombraban «Dispositivos →» pasan a «Equipos →» | Diseño §1.1 | Task 4 y O1 |
| D14 | La pestaña Activos de la ficha 360 son tres tarjetas (licencias, suscripciones, redes) con su número y su enlace a la lista con el chip puesto | No existe un inventario de activos como el de Atera; los activos del riel son esos tres; resolución del coordinador del 2026-11-24 | Task 5 |
| D15 | Las claves `itms.*` de `localStorage` y las cabeceras `X-ITMS-*` se quedan; «ITMS» desaparece de todo lo demás | La regla del proyecto 4 para identificadores internos y contratos (su diseño §1.2) | Task 2 |
| D16 | Salir de una página con cambios es cambiar de ruta (la query sola no, salvo `search`); un guardado que se va llama a `release()` y no pregunta | `useBlocker` lee su condición de un efecto: guardar y navegar en un paso quedaba bloqueado (revisión previa, I-1, reproducido con react-router 7.18.1) | Task 3 |
| D17 | El chip solo estrecha con un cliente de la persona y espera a `/me` (`useNarrowing`); `ClientFilter` quita de la URL un cliente ajeno | Un identificador ajeno mostraría «Todos los clientes» sobre una lista vacía, y leer antes de `/me` mostraría las filas de todos bajo el chip de uno (revisión previa, I-4 e I-5) | Task 3 |
| D18 | El router de datos tiene su pantalla de error traducida (`RouteError`) en la ruta de diseño | Sin ella se ve la de React Router, en inglés y con estilos en línea; resolución del coordinador del 2026-11-24 | Task 3 |
| D19 | La pestaña Alertas de la ficha 360 lista las activas y dice cuántas hay abiertas en total, con el conteo del servidor | La lista toma un solo `state` y «abierta» no se puede pedir; resolución del coordinador del 2026-11-24 | Task 5 |
| D20 | `?client=` significa «estrecha esta lista» en toda la consola; el recorrido de «Añadir un equipo» pasa a `journey_client` y `journey_site` | Una clave, un significado; nadie más (ni el backend ni un correo) escribe ese parámetro; resolución del coordinador del 2026-11-24 | Task 5 |
| D21 | `/health` se parte en vistas en la URL (`?view=profiles`, `?view=actions` y `?view=settings`; por defecto la cola con su informe); las anclas de antes abren su vista; la puerta mide cada vista | La cola de alertas sin la configuración mezclada, a la manera de Atera, en la misma ruta; resolución del coordinador del 2026-11-24 (pregunta abierta 3, valor por defecto) | O3 |
| D22 | `settings/SaveBar.tsx` se borra; Configuración, Tu cuenta y el dial de autonomía usan la `SaveBar` del kit, que gana `saved`, `problems`, `blockedReason` y `submit` | Una sola barra de guardado para toda la consola (diseño §2), sin perder lo que la de ajustes hacía | Task 3 y O7 |
| D23 | En Credenciales, `?new=<tipo>` conserva su significado (abre la alta con ese tipo); el botón usa `?new=credential` | Las páginas de instalación remota enlazan a `?new=push_install` (revisión previa, W-I6) | O6 |
| D24 | El riel plegado se queda en 76 px | Ni la marca ni el diseño piden otro ancho (revisión previa, S-M-4) | Task 4 |

**Preguntas abiertas:** ninguna. Las cuatro del borrador se contestaron el 2026-11-24 (`decisiones-p34.md`): (1) el Asistente, como en Atera: D12; (2) las métricas de CPU y memoria: la ficha del equipo enseña las últimas lecturas y las series quedan fuera de este proyecto, en otro (por el presupuesto de reposo del agente; Apéndice E, B1); (3) Alertas en vistas: D21; (4) las preguntas de la crítica de impeccable: las contesta el coordinador con `DESIGN.md`, el diseño y las decisiones del operador, en `.impeccable/critica-respuestas.md` versionado, y solo sube al operador lo que esas fuentes no zanjan («Cierre de cada oleada», paso 3). `PRODUCT.md` lo confirmó el operador tal cual el mismo día (Task 1, Step 1).

---

## Apéndice B: las 86 páginas y su oleada

Censo: `cd frontend && find src -name "*Page.tsx" -not -name "*.test.tsx" | sort` (86 el 2026-11-23). Recetas: L lista, F ficha, FO formulario, O otra.

| # | Archivo (`frontend/…`) | Ruta(s) | Receta | Tarea |
|---|---|---|---|---|
| 1 | `src/preview/PreviewPage.tsx` | `/__preview` | O | T3 |
| 2 | `src/shell/AreaPage.tsx` | `/admin` | O | T4 |
| 3 | `src/modules/clients/ClientsPage.tsx` | `/clients` | L | T5 |
| 4 | `src/modules/clients/ClientDetailPage.tsx` | `/clients/:clientId` | F | T5 |
| 5 | `src/modules/devices/DevicesPage.tsx` | `/devices` | L | T6 · O1 |
| 6 | `src/modules/inventory/DeviceDetailPage.tsx` | `/devices/:deviceId` | F | T6 · O1 |
| 7 | `src/modules/inventory/SoftwarePage.tsx` | `/software` | L | T6 · O1 |
| 8 | `src/modules/devices/CandidatesPage.tsx` | `/discovered` | L | T6 · O1 |
| 9 | `src/modules/devices/GroupsPage.tsx` | `/device-groups` | L | T6 · O1 |
| 10 | `src/modules/onboarding/OnboardPage.tsx` | `/devices/onboard` | O | T6 · O1 |
| 11 | `src/modules/agent-push/PushRunsPage.tsx` | `/devices/push-runs` | L | T6 · O1 |
| 12 | `src/modules/agent-push/PushRunPage.tsx` | `/devices/push-runs/:runId` | F | T6 · O1 |
| 13 | `src/modules/agent-push/PushAuthorizationsPage.tsx` | `/agent-push/authorizations` | L | T6 · O1 |
| 14 | `src/modules/agent-push/PushCredentialScopesPage.tsx` | `/agent-push/credential-scopes` | L | T6 · O1 |
| 15 | `src/modules/agent-releases/AgentReleasesPage.tsx` | `/agent-packages` | O | T6 · O1 |
| 16 | `src/modules/dashboard/DashboardPage.tsx` | `/dashboard` | O | T7 · O2 |
| 17 | `src/modules/service-desk/TicketListPage.tsx` | `/service-desk` | L | T7 · O2 |
| 18 | `src/modules/service-desk/TicketDetailPage.tsx` | `/service-desk/tickets/:ticketId` | F | T7 · O2 |
| 19 | `src/modules/service-desk/MyWorkPage.tsx` | `/service-desk/my-work` | L | T7 · O2 |
| 20 | `src/modules/service-desk/ServiceDeskAdminPage.tsx` | `/service-desk/admin` | FO | T7 · O2 |
| 21 | `src/modules/sla/SlaAdminPage.tsx` | `/sla` | FO | T7 · O2 |
| 22 | `src/modules/email-intake/EmailIntakeAdminPage.tsx` | `/email-intake` | FO | T7 · O2 |
| 23 | `src/modules/notification/NotificationAdminPage.tsx` | `/notifications` | FO | T7 · O2 |
| 24 | `src/modules/portal/MyTicketsPage.tsx` | `/portal` | L | T7 · O2 |
| 25 | `src/modules/portal/NewTicketPage.tsx` | `/portal/new` | FO | T7 · O2 |
| 26 | `src/modules/portal/PortalTicketDetailPage.tsx` | `/portal/tickets/:ticketId` | F | T7 · O2 |
| 27 | `src/modules/health/HealthPage.tsx` | `/health` | L | T8 · O3 |
| 28 | `src/modules/patch/FleetPatchPage.tsx` | `/patch` | L | T8 · O3 |
| 29 | `src/modules/deploy/DeployPage.tsx` | `/deploy` | L | T8 · O3 |
| 30 | `src/modules/deploy/PackagesPage.tsx` | `/deploy/packages` | L | T8 · O3 |
| 31 | `src/modules/deploy/ScriptsPage.tsx` | `/deploy/scripts` | L | T8 · O3 |
| 32 | `src/modules/remote/SessionsPage.tsx` | `/remote` | L | T8 · O3 |
| 33 | `src/modules/jobs/JobsPage.tsx` | `/jobs` | L | T8 · O3 |
| 34 | `src/modules/error-monitor/ErrorMonitorPage.tsx` | `/error-monitor` | L | T8 · O3 |
| 35 | `src/modules/platform-status/PlatformStatusPage.tsx` | `/platform-status` | O | T8 · O3 |
| 36 | `src/modules/security/SecurityPage.tsx` | `/security` | L | T8 · O3 |
| 37 | `src/modules/audit/AuditPage.tsx` | `/audit` | L | T8 · O3 |
| 38 | `src/modules/backup/BackupPointsPage.tsx` | `/backup` | L | T9 · O4 |
| 39 | `src/modules/backup/RepositoriesPage.tsx` | `/backup/repositories` | L | T9 · O4 |
| 40 | `src/modules/backup/CoveragePage.tsx` | `/backup/coverage` | L | T9 · O4 |
| 41 | `src/modules/backup/OffsitePage.tsx` | `/backup/offsite` | FO | T9 · O4 |
| 42 | `src/modules/bcp/PlansPage.tsx` | `/bcp` | L | T9 · O4 |
| 43 | `src/modules/provisioning/ProvisioningRunsPage.tsx` | `/provisioning` | L | T9 · O4 |
| 44 | `src/modules/provisioning/ImagesPage.tsx` | `/provisioning/images` | L | T9 · O4 |
| 45 | `src/modules/provisioning/ProfilesPage.tsx` | `/provisioning/profiles` | L | T9 · O4 |
| 46 | `src/modules/site-servers/SiteServersPage.tsx` | `/site-servers` | L | T9 · O4 |
| 47 | `src/modules/site-servers/SiteServerDetailPage.tsx` | `/site-servers/:siteServerId` | F | T9 · O4 |
| 48 | `src/modules/enrollment/EnrollmentPage.tsx` | `/enrollment` | L | T9 · O4 |
| 49 | `src/modules/licenses/LicensesPage.tsx` | `/licenses` | L | T10 · O5 |
| 50 | `src/modules/licenses/ProductsPage.tsx` | `/licenses/products` | L | T10 · O5 |
| 51 | `src/modules/licenses/VendorsPage.tsx` | `/licenses/vendors` | L | T10 · O5 |
| 52 | `src/modules/subscriptions/SubscriptionsPage.tsx` | `/subscriptions` | L | T10 · O5 |
| 53 | `src/modules/ipam/SubnetTreePage.tsx` | `/ipam` | L | T10 · O5 |
| 54 | `src/modules/ipam/SubnetDetailPage.tsx` | `/ipam/subnets/:subnetId` | F | T10 · O5 |
| 55 | `src/modules/ipam/DriftPage.tsx` | `/ipam/drift` | L | T10 · O5 |
| 56 | `src/modules/ipam/SearchPage.tsx` | `/ipam/search` | L | T10 · O5 |
| 57 | `src/modules/ipam/RegistryPage.tsx` | `/ipam/registry` | FO | T10 · O5 |
| 58 | `src/modules/ipam/ScanAuthorizationsPage.tsx` | `/ipam/scan-authorizations` | L | T10 · O5 |
| 59 | `src/modules/ipam/ImportPage.tsx` | `/ipam/import` | O | T10 · O5 |
| 60 | `src/modules/docs/DocsPage.tsx` | `/docs` | L + O | T11 · O6 |
| 61 | `src/modules/change/ChangesPage.tsx` | `/changes` | L | T11 · O6 |
| 62 | `src/modules/budget/BudgetPlannerPage.tsx` | `/budget/planner` | FO | T11 · O6 |
| 63 | `src/modules/budget/ExpensesPage.tsx` | `/budget/expenses` | L | T11 · O6 |
| 64 | `src/modules/budget/ConfirmationQueuePage.tsx` | `/budget/confirm` | L | T11 · O6 |
| 65 | `src/modules/budget/BudgetAdminPage.tsx` | `/budget/admin` | FO | T11 · O6 |
| 66 | `src/modules/people/PersonPage.tsx` | `/people/:personId` | F | T11 · O6 |
| 67 | `src/modules/people/UnmatchedPage.tsx` | `/people/unmatched` | L | T11 · O6 |
| 68 | `src/modules/credentials/CredentialsPage.tsx` | `/credentials` | L | T11 · O6 |
| 69 | `src/modules/settings/SettingsPage.tsx` | `/settings` | FO | T12 · O7 |
| 70 | `src/modules/profile/ProfilePage.tsx` | `/profile` | FO | T12 · O7 |
| 71 | `src/modules/profile/SecurityPage.tsx` | `/portal/security`, `/profile/security` | FO | T12 · O7 |
| 72 | `src/modules/ai-admin/AiSettingsPage.tsx` | `/assistant/settings` | FO | T12 · O7 |
| 73 | `src/modules/ai-automation/AutonomyDialPage.tsx` | `/assistant/autonomy` | FO | T12 · O7 |
| 74 | `src/modules/ai-automation/ActionsPage.tsx` | `/assistant/actions`, `/assistant/actions/:actionId` | L, F | T12 · O7 |
| 75 | `src/modules/auth/SignInPage.tsx` | `/sign-in` | O | T13 · O8 |
| 76 | `src/modules/auth/FactorPage.tsx` | `/sign-in/factor` | O | T13 · O8 |
| 77 | `src/modules/auth/EnrollPage.tsx` | `/sign-in/enroll` | O | T13 · O8 |
| 78 | `src/modules/auth/RecoveryCodesPage.tsx` | `/sign-in/recovery-codes` | O | T13 · O8 |
| 79 | `src/modules/auth/MicrosoftCallbackPage.tsx` | `/sign-in/microsoft` | O | T13 · O8 |
| 80 | `src/modules/auth/ForgotPasswordPage.tsx` | `/forgot-password` | O | T13 · O8 |
| 81 | `src/modules/auth/ResetPasswordPage.tsx` | `/reset-password` | O | T13 · O8 |
| 82 | `src/modules/auth/InvitePage.tsx` | `/invite` | O | T13 · O8 |
| 83 | `src/modules/auth/RecoverPage.tsx` | `/recover` | O | T13 · O8 |
| 84 | `src/modules/auth/PortalSignInPage.tsx` | `/portal/sign-in` | O | T13 · O8 |
| 85 | `src/modules/auth/ConsentPage.tsx` | `/oauth/authorize` | O | T13 · O8 |
| 86 | `src/modules/setup/SetupPage.tsx` | `(SetupGate, sin ruta)` | O | T13 · O8 |

**Las 22 pantallas enrutadas que no se llaman `*Page.tsx`** (entran en la oleada de su módulo; el diseño cuenta 86 porque cuenta archivos, pero estas también son páginas que una persona abre):

| # | Archivo (`frontend/src/modules/…`) | Ruta(s) | Receta | Tarea |
|---|---|---|---|---|
| E1 | `email-intake/OAuthCallback.tsx` | `/settings/email-intake/oauth/callback` | O | T7 · O2 |
| E2 | `patch/PatchPolicyEditor.tsx` | `/patch/policies` | FO | T8 · O3 |
| E3 | `remote/ScreenViewer.tsx` | `/remote/:sessionId` | O | T8 · O3 |
| E4 | `remote/RecordingPlayer.tsx` | `/remote/:sessionId/recording` | O | T8 · O3 |
| E5 | `backup/BackupPolicyEditor.tsx` | `/backup/policies` | FO | T9 · O4 |
| E6 | `bcp/PlanDetail.tsx` | `/bcp/plans/:planId` | F | T9 · O4 |
| E7 | `bcp/TestRecorder.tsx` | `/bcp/tests/:testId` | FO | T9 · O4 |
| E8 | `bcp/BcpReport.tsx` | `/bcp/report`, `/bcp/tests/:testId/report` | O | T9 · O4 |
| E9 | `licenses/LicenseDetail.tsx` | `/licenses/:licenseId` | F | T10 · O5 |
| E10 | `licenses/ComplianceDashboard.tsx` | `/licenses/compliance` | O | T10 · O5 |
| E11 | `licenses/ReviewQueue.tsx` | `/licenses/review-queue` | L | T10 · O5 |
| E12 | `licenses/RenewalCalendar.tsx` | `/licenses/renewals` | O | T10 · O5 |
| E13 | `subscriptions/SubscriptionDetail.tsx` | `/subscriptions/:subscriptionId` | F | T10 · O5 |
| E14 | `subscriptions/SpendDashboard.tsx` | `/subscriptions/spend` | O | T10 · O5 |
| E15 | `subscriptions/RenewalCalendar.tsx` | `/subscriptions/renewals` | O | T10 · O5 |
| E16 | `budget/BudgetDashboard.tsx` | `/budget` | O | T11 · O6 |
| E17 | `change/ApprovalQueue.tsx` | `/changes/approvals` | L | T11 · O6 |
| E18 | `change/ChangeCalendar.tsx` | `/changes/calendar` | O | T11 · O6 |
| E19 | `change/ChangeDetail.tsx` | `/changes/:changeId` | F | T11 · O6 |
| E20 | `reporting/ReportCatalogue.tsx` | `/reports` | L | T11 · O6 |
| E21 | `reporting/ReportView.tsx` | `/reports/:reportKey` | O | T11 · O6 |
| E22 | `ai-admin/ChatPanel.tsx` | `/assistant` | O | T12 · O7 |

Fuera de las dos listas, a propósito: `Landing` (la ruta `*`, en `App.tsx`, redirige y no dibuja nada), las redirecciones `/people`, `/agent-push`, `/governance` y `/assets`, y los marcos `PortalShell` (O2) y `AuthCard` (O8), que no son páginas pero sí reciben la marca.

**Reparto por tarea:** base 4 (T3: 1, T4: 1, T5: 2) + O1 11 + O2 11 + O3 11 + O4 11 + O5 11 + O6 9 + O7 6 + O8 12 = **86**; pantallas extra 22.

---

## Apéndice C: pruebas añadidas y cambiadas

**Nuevas:**

| Archivo | Qué fija | Tarea |
|---|---|---|
| `frontend/src/test/tokens.test.ts` (reescrita) | `index.css` = `DESIGN.md` v4 = `novieri-tokens.json`; los cuatro alias retirados; sin `--rail-hot`; `--sans` empieza por Inter y `--mono` es la pila de código de `DESIGN.md` (lo que la de v3 ya fijaba) | 1 |
| `frontend/src/test/second-carrier.test.tsx` | «En curso» e «información» son el mismo cobalto, y en las series, la celda de calor, el donut y la línea de tiempo algo más los distingue (D2) | 1 |
| `frontend/src/test/harmony-client.test.ts` (casos nuevos) | `servedError`, `failedLoad`, `missingShell` y `FRAMELESS`: la puerta falla una página que no se dibujó (revisión previa, C1) | 1 |
| `frontend/src/shell/title.test.tsx` | «Página · Novieri RMM», el registro delante, el producto solo | 2 |
| `frontend/src/shell/brand.test.tsx` | El símbolo y el logotipo son los del operador, trazo a trazo; el favicon y el `<title>` | 2 |
| `frontend/src/test/no-itms.test.ts` | «ITMS» fuera de `frontend/src`, `scripts` e `index.html`, salvo las cabeceras `X-ITMS-*`, `ITMSRSP1` y los comentarios fechados (una cadena con fecha no se excusa) | 2 |
| `frontend/src/test/renderPage.tsx`, `frontend/src/test/narrowing.ts` (ayudas) | Una página bajo un router de datos, con el título del armazón si se pide (`TitleShell`); un doble de la API que anota a qué cliente se estrechó cada lectura | 3 |
| `frontend/src/shell/listKit.test.tsx` | Filtros en la URL (Atrás deshace un chip; teclear no llena el historial), conteos sin ceros inventados, «Mostrando x de y», vacío con causa, barra de lote, chip «Cliente» con forma de chip y sin clientes ajenos, `useNarrowing` (espera a `/me`, vuelve a leer al cambiar), `useApiFor`, `?new=` | 3 |
| `frontend/src/shell/detailKit.test.tsx` | Cabecera de ficha, pestañas en la URL (una fila, historial, por defecto sin parámetro), `SectionNav` | 3 |
| `frontend/src/shell/formKit.test.tsx` | Una barra de guardado, descartar, error, «Guardado.», razón para no guardar, Intro en un campo, aviso al salir (navegación y `beforeunload`), un guardado que se va no pregunta, la query sola no es salir salvo `search`, secciones plegables | 3 |
| `frontend/src/lib/api.forClient.test.ts` | `X-Client-Id` del cliente pedido; el 401 de siempre | 3 |
| `frontend/src/App.router.test.tsx` | Router de datos sobre las mismas rutas, con su pantalla de error traducida | 3 |
| `frontend/src/i18n/catalogue.identical.test.ts` (caso nuevo) | Cada oleada tiene sus entradas idénticas en su propio archivo | 3 |
| `frontend/src/shell/newMenu.test.ts` | Las entradas de «Nuevo», cada una con su permiso | 4 |
| `frontend/src/shell/NewMenu.test.tsx` | Botón de menú primario, foco, flechas, Escape | 4 |
| `frontend/src/shell/AdminPanel.test.tsx` | Administración: botón, volver, búsqueda sin acentos, sección abierta | 4 |
| `frontend/src/shell/newDestinations.test.tsx` | Cada entrada de «Nuevo» abre lo que promete | 4 |
| `frontend/src/modules/clients/__tests__/client360.test.tsx` | Las ocho pestañas, la URL, el título de pestaña, los datos del cliente (estrechados), las alertas activas, los tres activos, los tiles | 5 |
| `frontend/src/modules/auth/__tests__/authCard.test.tsx` | El marco de acceso con la marca y su título | 13 |
| En cada módulo de las oleadas | Las pruebas mínimas de su receta (L, F, FO) por página; la del chip en cada lista que lo lleva; la del título en cada ficha; y las que nombra cada bloque | 6 a 13 |

**Cambiadas a propósito** (ninguna se borra; cada cambio con un comentario que cita el diseño):

| Archivo | Cambio | Tarea |
|---|---|---|
| `frontend/src/test/contrast.test.ts` | Filas nuevas (blanco sobre crítico, hover y presionado del primario, marca del riel); sin `--rail-hot`; `--ink` en `NON_TEXT`; el acento por debajo de 3:1 en el riel | 1 |
| `frontend/src/test/preview.test.ts:169`, `frontend/src/i18n/coverage.test.ts:115`, `frontend/src/modules/audit/auditVocabulary.coverage.test.ts:38-43`, `frontend/src/modules/settings/settings.es.test.tsx:315,349` | La sonda XSS con su nombre nuevo; `ALLOWED_LITERALS.ITMS` fuera; la variable `ITS_AUDIT_BACKEND_APP`; «Novieri RMM» en las listas de lo que no es inglés | 2 |
| Las seis pruebas de `modules/agent-releases/` que nombran binarios | `its-agent-*` → `novieri-agent-*`, y un caso nuevo: el nombre viejo ya no se reconoce | 2 |
| `frontend/src/test/radius.test.ts` | `--r-badge` en la escalera y `--radius-xs` | 1 |
| `frontend/src/test/base-styles.test.ts` | `V4_OVERRIDES` y `.nav-item.sub` | 3, 4 |
| `frontend/src/shell/nav.test.ts` | Tres casos de «two layers» → «the Atera order» y tres conservados tal cual; Clientes en el riel; migas de carpeta; las guardas de iconos y de vacíos ven las carpetas | 4 |
| `frontend/src/shell/icons.test.tsx` | Los iconos de las carpetas entran en la lista | 4 |
| `frontend/src/shell/Sidebar.test.tsx` | `renderAt` mira todo el riel (los seis casos del ítem actual, intactos); «the second layer» → carpetas, anclados, punto, marca, y dos casos conservados sobre el panel | 4 |
| `frontend/src/shell/AppShell.test.tsx` | El título con «Novieri RMM»; «Añadir un equipo» → «Nuevo»; las tres pruebas de títulos de grupo → carpetas; el Asistente fuera de la barra, en el riel y en su botón redondo, que no está en `/assistant` y aparta la barra de guardado | 2, 4 |
| `frontend/src/test/responsive.test.ts` | El Asistente sobrevive como botón redondo, nunca en la barra; el botón mide 48 px y la barra de guardado le deja su esquina | 4 |
| `frontend/src/shell/AreaPage.test.tsx` | Tarjetas en lugar de tablas (buscadas por su `h2`); búsqueda | 4 |
| `frontend/src/shell/shell.es.test.tsx` | «Inicio», «Equipos», «Documentación», «Activos», «Más», «Nuevo», «Preguntar al Asistente» | 4 |
| `service-desk.es.test.tsx:1019`, `deploy.es.test.tsx:807`, `backup.es.test.tsx:1257` | «Dispositivos → Añadir un equipo» → «Equipos → Añadir un equipo» | 4 |
| `frontend/src/App.crumb.test.tsx`, `App.shell.test.tsx` | La raíz de la miga es «Novieri» | 2 |
| Las ~25 que esperaban «ITMS» | El texto o el dato nuevo | 2 |
| `frontend/src/modules/clients/__tests__/clients.test.tsx`, `clients.es.test.tsx` | El interruptor de archivados → el chip; un caso nuevo de lista | 5 |
| `journeyTarget.test.tsx`, `journey.test.ts`, `OnboardPage.test.tsx`, `ipam/__tests__/clients.test.tsx`, `enrollment/clientSelection.test.tsx`, `agent-releases/clientSelection.test.tsx` | `?client=` del recorrido → `?journey_client=` (y `site` → `journey_site`) | 5 |
| `frontend/src/modules/health/__tests__/alertTriage.test.tsx:295-313` | Las anclas, en sus vistas; un ancla de antes abre su vista (D21) | 8 |
| `frontend/src/modules/settings/__tests__/*` (17 pruebas), `profile/ProfilePage.test.tsx`, `ai-automation/__tests__/aiAutomationPrimitives.test.tsx` | El «Save» de la barra del kit; «Guardado.» en la barra; la razón de solo lectura en un aviso (D22) | 12 |
| Las de cada módulo de las oleadas | Consultas (`tab` → `button` con `aria-pressed`, el «Save» de la barra, `link` con `aria-current`) | 6 a 13 |
| `frontend/src/modules/change/change.es.test.tsx` y `__tests__/change.test.tsx` | El título de la ficha lo compone `documentTitle` bajo `TitleShell`: «… · Cambios · Novieri RMM» y, tras cambiar de idioma, «… · Changes · Novieri RMM» | 11 |

---

## Apéndice D: reglas de `DESIGN.md` que cambian, y por qué

| Regla en v3 | En v4 | Por qué |
|---|---|---|
| Autoridad: `docs/design/itms-prototype-v3.html` | `DESIGN.md` v4 (frontmatter con los nombres de las propiedades CSS) y la marca en `docs/design/marca/` | Diseño §3.1-3.2; el prototipo v3 no tiene la marca elegida |
| North Star «The Instrument Rail» | «The Survey Plan» | La dirección elegida es «Cordillera: la sobriedad de un plano topográfico» |
| Riel `#0f1a20`, acento `#1f5fd1`, página `#f5f6f9`… | Los valores de `novieri-tokens.json` | La marca (A2, dirección A) |
| Tres tintas (ink, ink-2, muted), dos líneas (line, line-2) | Dos tintas, una línea; `--ink-2` y `--line-2` son alias | La marca tiene dos tonos de texto y un borde (D2) |
| Cinco severidades: crit, warn, ok, run, mut (+ info = acento) | crit, warn, ok, mut (desconocido) e información = acento; «en curso» comparte el tono de información con su icono | La marca nombra ok, info, warning, critical, unknown (D2); el operador lo confirmó el 2026-11-24 |
| (no existía: el tono de «en curso» bastaba para distinguirlo) | Donde no hay icono, otro portador: las series de los gráficos usan `--sev-mut` en lugar del cobalto repetido, la celda «información» del mapa de calor lleva un anillo interior, el donut repite el icono del tipo en su etiqueta y la barra de la línea de tiempo lleva su texto | Al fundir «en curso» en el cobalto, los cuatro sitios sin icono perderían la distinción (D2; `second-carrier.test.tsx`) |
| Velo de diálogo `rgba(15,26,32,.38)` | `rgba(16,27,48,.38)`: la misma opacidad sobre el tono del riel nuevo | La marca no define velo; sigue al riel como la sombra (revisión previa, S-M-5) |
| Contador de alertas del riel en `--rail-hot` | Etiqueta crítica con texto blanco | Tabla de contraste de la marca (D3) |
| Activo del riel: borde izquierdo de 2 px en el acento | Fondo elevado, texto blanco, icono y punto en `--rail-mark` | «Gesto propio» de la dirección A; el acento mide 2,32:1 sobre el riel (D3) |
| Radios 6 / 10 / 999 (píldora para chips, etiquetas, barras e interruptor) | 4 etiquetas / 6 controles y chips / 10 paneles; 999 solo interruptor y barra | La marca retira la píldora de chips y etiquetas (D4) |
| Riel con títulos de grupo (Daily, Governance, Assets & Spend, Configure) | Sin títulos, orden de Atera, carpetas Activos y Más, Asistente y Administración anclados | Diseño §1.1 |
| «Don't ship a dead control in the chrome. No bell, no "+ New".» | «No bell; no New entry without a real destination» | El diseño §1.2 añade «Nuevo» con destinos reales; la campana sigue fuera (§6) |
| «One primary per page body; the chrome's "Install agent" is the standing exception» | «Nuevo» es el único primario de la barra | Diseño §1.2: «Instalar agente» pasa a «Nuevo» y a Equipos (D11) |
| La barra: búsqueda, «Install agent», glifos de salud y plataforma, IA, cuenta | «Nuevo», búsqueda (hasta 640 px), selector, glifos, cuenta; sin icono de IA | Diseño §1.2 y la respuesta del operador del 2026-11-24 («como queda en las capturas de Atera», `docs/design/referencias-atera/atera 1.png`) (D12) |
| El Asistente, un icono de la barra (decisión del 2026-09-08) | Anclado al pie del riel y, en toda página salvo `/assistant`, un botón redondo de 48 px abajo a la derecha que lleva a `/assistant`, con `ai.use` | La captura de Atera; el botón de Atera tapa el de guardar (recorrido, línea 639), así que la barra de guardado le deja su esquina (D12) |
| El contenido termina en su relleno de siempre | Con el botón del Asistente presente, el contenido deja 88 px al pie | Que el botón no tape la última fila de una lista ni el último campo (D12) |
| Riel plegado de 76 px | Sin cambio: 76 px | Ni la marca ni el diseño piden otro ancho: las propuestas no dibujan un riel plegado y el diseño §1.1 solo dice «queda solo con iconos» (D24; revisión previa, S-M-4) |
| Sin patrones de página escritos | Listas, fichas y formularios del kit; una barra de guardado; todo en la URL | Diseño §2 |
| Marca: un escudo dibujado sobre el acento, sin favicon | Logotipo, símbolo, favicon y conjunto «Novieri RMM» | Diseño §3.1 |

Lo que **no** cambia: un acento; el contraste AA medido por `contrast.test.ts`; la escala de tipos 22/15/13,5/12/10,5/26 e Inter autoalojada; los tokens como única fuente (`tokens.test`, `radius.test`, `jsx-tokens.test`); solo tema claro; ningún control muerto; la puerta de armonía en los dos idiomas (diseño §3.2).

---

## Apéndice E: backend que una pantalla necesitaría, y ediciones cruzadas con el proyecto 4

**Cambios de backend que una pantalla necesitaría y no existen** (diseño §6: «se nombran en el plan»). Ninguno es necesario para cumplir el diseño; ninguno se hace en este proyecto.

| # | Qué | Pantalla | Qué hace el plan sin él |
|---|---|---|---|
| B1 | Series de CPU y memoria de un equipo (24 h), como el gráfico de la captura «atera 8» | Ficha del equipo (O1) | Enseña la última lectura de cada comprobación (`getDeviceHealth().checks[].last_value`) y la actividad; nada de gráficos vacíos. **Decidido por el operador el 2026-11-24**: la última lectura ahora; la serie temporal queda fuera de este proyecto (es un proyecto aparte, y toca el presupuesto de reposo del agente) |
| B2 | Conteos por estado en las respuestas de las listas que hoy no los traen (trabajos, sesiones, despliegues, imágenes…) | Chips de las listas de O3, O4 | Chips sin número donde el servidor no lo da (regla «Absent Zero»); con número donde ya lo da (equipos, alertas, tickets) |
| B3 | Un filtro `?client_id=` en las listas que no lo tienen (alertas, licencias, suscripciones, trabajos…) | Chip «Cliente» | No hace falta: `X-Client-Id` estrecha igual, y lo impone la base de datos (D6) |

**Ediciones cruzadas con el proyecto 4** (los dos llegan a `main` en la misma PR):

| # | Qué | Quién | Nota |
|---|---|---|---|
| X1 | Nombre del MSI de descarga: el servidor lo manda (`backend/app/modules/agent_distribution/msi.py:292`, hoy `ITMS-Agent-<host>[@<port>]-<arch>.msi`); la consola lo usa de reserva (`lib/api.ts:4344`), lo describe en un comentario (`:1472`) y lo fija en su fixture y en tres pruebas | P4 cambia el servidor a `NovieriRMM-Agent-<host>[@<port>]-<arch>.msi` (su diseño §1.1); P3 cambia la reserva a `` `NovieriRMM-Agent-${body.arch}.msi` ``, el comentario, el fixture y las pruebas (Task 2 Step 5) | **Conciliado** con el Apéndice F del plan del proyecto 4 (F.1, F.2, F.3), que da exactamente estos valores. Si el proyecto 4 cambiara el nombre antes de fusionar, el coordinador lo trae a la Task 2 |
| X9 | Nombres del binario del agente: `its-agent-<os>-<arch>[.exe]` → `novieri-agent-<os>-<arch>[.exe]`, y el paquete `.deb` `novieri-agent` | P4 cambia el agente, la CI de publicación y `release-sign`; P3 cambia `publish.ts` (`inferPlatform`, líneas 6, 57 y 63), `agent-releases/fixtures.ts:104-128`, `deploy/fixtures.ts:47` y las seis pruebas que nombran binarios (Task 2 Step 5) | Resolución del coordinador del 2026-11-24: sigue el diseño del proyecto 4 §1.1 al pie de la letra. Conciliado con su Apéndice F: las ocho pruebas de F.3 y los dos `fixtures.ts` de F.2 están en la Task 2. El nombre viejo deja de reconocerse (no hay flota). **Queda fuera** lo que F.4 atribuye a este proyecto y su diseño no pide: «Toeshee» en los datos de prueba (`console.toeshee.example`, `toeshee.example`, «Toeshee Operations») es el nombre de la organización de ejemplo, no la marca; se queda, salvo que el coordinador decida otra cosa |
| X2 | Emisor TOTP y `rp_name` de WebAuthn «Novieri RMM» (`app/core/auth/notices.py`) | P4 | P3 refleja el valor en `modules/auth/fixtures.ts` y en el harness de pruebas de `auth` |
| X3 | Cabeceras `X-ITMS-Request`, `X-ITMS-Background` y la marca `ITMSRSP1` | Nadie las cambia | Contrato (diseño de P4 §1.2); la guarda de P3 las permite por nombre |
| X4 | `docs/product.md` (nombre del producto, §9, §11) | P3 | El proyecto 4 no debe editarlo en paralelo; si necesita, lo pide al coordinador |
| X5 | `DESIGN.md`, `PRODUCT.md`, `.impeccable/` | P3 | Archivos de la raíz que solo toca este proyecto |
| X6 | La CI | P4 es su dueño; P3 **no necesita cambiarla** | Los trabajos `frontend`, `a11y` y `harmony` corren igual; las diez variantes nuevas de `routes.mjs` (127 rutas en total) alargan cada pasada de armonía unos 150 s (40 capturas más por idioma). La puerta reforzada (Task 1 Step 12) falla donde antes daba un verde falso; un fallo de entorno se vuelve a correr |
| X7 | Los correos «[Novieri RMM] …» | P4 (backend) | La consola no los dibuja |
| X8 | `frontend/` | P4 no lo toca (su diseño, «El proyecto 3 toca `frontend/`») | Si P4 necesitara un cambio en la consola, lo pide al coordinador y entra por la base de este plan |

---

## Apéndice F: comandos

Desde `frontend/` salvo que se diga:

| Para | Comando | Esperado |
|---|---|---|
| Una prueba | `npx vitest run <ruta>` | `Test Files … passed` |
| La suite | `npx vitest run` | 0 fallos (365 archivos y 7200 pruebas o más; ~5 min) |
| Tipos | `npm run typecheck` | sin salida, código 0 |
| Build | `npm run build` | `dist/` con `favicon.svg` |
| Armonía | `node scripts/harmony.mjs --strict-fixtures --lang es --routes <lista>` (y `--lang en`) | `ok    every route on the tokens, no overflow, the top bar in bounds, no serious axe violation, no fixture miss` |
| Navegador sobre el kit | `node scripts/a11y-browser.mjs --falsify && node scripts/a11y-browser.mjs` | código 0 |
| Contexto de impeccable (raíz) | `.claude/skills/impeccable/scripts/impeccable context --target <archivo>` | sin `NO_PRODUCT_MD` |
| Detector (raíz) | `.claude/skills/impeccable/scripts/impeccable detect --json frontend/src/modules/<m>` | `[]` o hallazgos revisados |
| Crítica persistida (raíz) | `.claude/skills/impeccable/scripts/impeccable critique-storage write "<archivo>" <cuerpo>` | la ruta escrita |
| Cerrar una crítica (raíz) | `.claude/skills/impeccable/scripts/impeccable critique-storage close "<archivo>" "<snapshot_file>"` | código 0 |
| Censo de páginas | `find src -name "*Page.tsx" -not -name "*.test.tsx" \| wc -l` | `86` |

---

## Autorrevisión del plan (2026-11-23; revisada el 2026-11-24)

Contra el diseño, sección por sección:

- **§1.1** riel en orden de Atera sin títulos, Clientes fuera de Administración, Activos y Más (este solo con módulos activos), Asistente y Administración anclados, Administración en acordeón con volver y búsqueda, permisos sin cambios, plegado a 76 px con iconos (D24) y cajón bajo 1024 px: Task 4 (Steps 1, 4, 6, 9), con la prueba de que los códigos son los mismos.
- **§1.2** «Nuevo» con seis destinos reales por permiso (Task 4, Steps 1, 5, 8 y la prueba de destinos), búsqueda ensanchada a 640 px (`.topbar-find`, Task 4 Step 9; la paleta no cambia de comportamiento), selector sin cambio de comportamiento (sus pruebas no se tocan, solo sus tokens), indicador de alertas y de plataforma, cuenta; «Instalar agente» en «Nuevo» y en Equipos (Task 4 y O1). El Asistente sale de la barra y queda como en la captura de Atera que el operador eligió el 2026-11-24: anclado al pie del riel y botón redondo abajo a la derecha (D12, Task 4 Steps 7, 9 y 10).
- **§1.3** ficha 360 con ocho pestañas en una línea y URL propia; Alertas con solo las activas y su conteo, Activos con tres tiles (D14); chip «Cliente» en las listas de un cliente con «Todos mis clientes», que solo ofrece los clientes de la persona y espera a `/me` (D6, D17): Tasks 3 y 5, y cada oleada, cada lista con su prueba del chip.
- **§2** lista, ficha y formulario con su código completo (Task 3), recetas para las oleadas, y cada página con sus cambios, pruebas y criterio de aceptación concretos en su bloque (ninguna remite solo a una receta); ficha de equipo según «atera 8», con la última lectura (O1, B1 decidido); una sola barra de guardado, también en Configuración (D22); la query sola no es salir de un formulario (D16); piezas existentes restiladas solo por tokens (Task 3).
- **§3** marca, tokens, contraste, radios, sombra, velo, tipografía (Task 1); «en curso» fundido en el cobalto con un segundo portador donde no hay icono (D2, `second-carrier.test.tsx`); logotipo, símbolo, favicon (Task 2); `DESIGN.md` v4 (Task 1) con sus cambios en el Apéndice D; `PRODUCT.md` confirmado por el operador el 2026-11-24 («Sí, tal cual»).
- **§4** las 86 páginas una a una (Apéndice B) y las 22 pantallas extra; cada oleada con crítica versionada (una por pasada, rutas relativas al repositorio, cerrada con `closed: true`) y pulido de impeccable, y capturas en los dos idiomas; portal (O2), acceso y consentimiento (O8); rutas sin mover salvo las vistas de Salud (`?view=`, D21, con las anclas de antes) y el parámetro del recorrido (`journey_client`, D20).
- **§5** pruebas nuevas pedidas por el diseño: «Nuevo» (Task 4), pestañas con URL (Task 3), barra de guardado con aviso (Task 3), filtros en la URL (Task 3), título de pestaña (Task 2 y una prueba por ficha), «ITMS» fuera (Task 2); `nav.test`, `routes-covered`, armazón y selector actualizados sin borrar ninguna prueba; revisión por oleada y de la rama (Task 14), que falla en una página sin crítica.
- **§6** sin tema oscuro, sin campana, sin vistas guardadas en el servidor, sin panel de chat superpuesto (el botón del Asistente abre su página); los cambios de backend que una pantalla querría, nombrados (Apéndice E).

Lo que la revisión previa del 2026-11-24 cambió en el plan (informe: `.git/sdd/preflight/p3-fix-report.md`):

- **Aislamiento de las copias de trabajo.** Cada worktree tiene su propio directorio `node_modules` de enlaces por paquete (Global Constraints), porque la caché de Vite vive dentro y su hash incluye la raíz: reproducido en una copia el 2026-11-24, dos servidores sobre un `node_modules` compartido se invalidan uno a otro y la puerta de armonía de hoy daba `ok` con una página sin dibujar. La puerta reforzada (Task 1 Step 12) falla con un 5xx o un módulo que no carga fuera de `/api`, y con una ruta de sesión que no dibuja la barra.
- **Archivos compartidos** en tres clases (base, `identical-ok.oN.ts` por oleada, territorio de la oleada) y un procedimiento para pedir una pieza del kit; las oleadas no editan nada de la base.
- **Estrechamiento por cliente** con su ayuda de pruebas (`recordingNarrowing`), y el parámetro del recorrido renombrado para que `?client=` signifique una sola cosa (D20).
- **Firmas nuevas o cambiadas**, todas definidas en la Task 3 y usadas con el mismo nombre y tipo después: `useNarrowing(clientId): { client: string | null; ready: boolean }`, `SaveBar({ dirty, saving, error?, onDiscard, onSave?, submit?, saved?, savedMessage?, problems?, blockedReason? })`, `useUnsavedGuard(dirty, options?: { search? }): { guard, release }`, `UnsavedGuard({ dirty, search? })`, `renderPage(path, element, pattern?, options?: { page?: MessageKey })`, `TitleShell({ page, children })`, `recordingNarrowing(api): { api, narrowedTo }`, `RouteError()`; cada hook de lectura de una oleada toma `chip` y lo pone en su clave.

Marcadores: el plan no deja «TBD», «implementar después» ni valores por sustituir en una orden; comprobado con `grep -n "TBD\|TODO\|<sha\|pregunta abierta"` sobre este archivo el 2026-11-24: no hay ningún «TBD», «TODO» ni `<sha`, y «pregunta abierta» solo aparece donde se dice que la 3 quedó resuelta (D21) y en esta línea. Los `<ruta>`, `<N>` y `<Página>` que quedan son de las plantillas de las recetas y del cierre, que cada bloque de oleada concreta; ninguno está en una orden para copiar y pegar. Donde una oleada debe leer un nombre exacto del catálogo antes de escribir una prueba (los títulos de diálogo de la prueba de destinos de la Task 4), el paso dice qué clave leer y con qué orden. Firmas: `useUrlFilters`, `FilterChips`, `ShowingCount`, `BulkBar`, `ListEmpty`, `ClientFilter`, `useNarrowing`, `useApiFor`, `useNewParam`, `ListToolbar`, `SectionNav`, `DetailHead`, `useUrlTab`, `UrlTabs`, `DetailGrid`, `ClientContext`, `FormPage`, `FormSection`, `SaveBar`, `UnsavedGuard`, `useUnsavedGuard`, `useDraft`, `documentTitle`, `useRecordTitle`, `useDocumentTitle`, `newMenuEntries`, `navFolders` y `ApiClient.forClient` se usan en las tareas posteriores con los nombres y tipos con que se definen.
