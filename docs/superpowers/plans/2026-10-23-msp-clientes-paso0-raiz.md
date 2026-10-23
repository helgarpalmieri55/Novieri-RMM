# Paso 0 — Mover el proyecto a la raíz del repositorio. Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que la raíz del repositorio sea la raíz del proyecto, para que GitHub
Actions ejecute `.github/workflows/ci.yml` en cada PR.

**Architecture:** Un solo `git mv` de todo `RMS-main/` a la raíz, más la fusión
de las dos carpetas `.claude/`. No cambia ni una línea de código del producto.
La CI de ese PR es la línea base contra la que se medirán las fases siguientes
del proyecto de clientes
(`docs/superpowers/specs/2026-10-23-msp-clientes-design.md` §0).

**Tech Stack:** git, GitHub Actions.

## Global Constraints

- Ningún archivo de `backend/`, `frontend/`, `agent/`, `deploy/` ni `scripts/` cambia de contenido; solo de ruta.
- `UI/` pasa a `docs/design/referencias-atera/`.
- Se conserva impeccable 4.7.0, que está en la raíz desde el PR #1. La copia 4.1.1 de `RMS-main/.claude/skills/impeccable/` se elimina.
- Ninguna prueba se borra, se salta ni se debilita para obtener verde (`CLAUDE.md`).

---

### Task 1: Mover y fusionar

**Files:**
- Move: `RMS-main/*` y sus archivos ocultos (`.github`, `.gitignore`, `.gitattributes`, `.impeccable`) → raíz
- Move: `RMS-main/.claude/skills/*` (salvo `impeccable`) → `.claude/skills/`
- Delete: `RMS-main/.claude/skills/impeccable/` (4.1.1, reemplazada por la 4.7.0 de la raíz)
- Delete: `RMS-main/.claude/settings.json` (ver Step 2)
- Move: `UI/*` → `docs/design/referencias-atera/`
- Modify: `.claude/.gitignore` (unir con las reglas de `.claude/` de `RMS-main/.gitignore`, que siguen valiendo desde la raíz)

- [ ] **Step 1: Comprobar que el árbol está limpio y en la rama correcta**

Run: `git status --porcelain && git branch --show-current`
Expected: sin cambios pendientes; rama `claude/compassionate-bardeen-vgtwus`.

- [ ] **Step 2: Confirmar que el hook de `RMS-main/.claude/settings.json` apunta a un script que no existe**

Run: `git ls-files RMS-main/.claude/hooks | wc -l && grep -o '\.claude/hooks/session-start' RMS-main/.claude/settings.json`
Expected: `0` y luego `.claude/hooks/session-start`. El archivo de hooks no está en el repo, así que al mover `settings.json` a la raíz, cada sesión de Claude Code ejecutaría un script inexistente. Se elimina `settings.json` y el motivo va en el mensaje del commit.

- [ ] **Step 3: Mover**

```bash
set -euo pipefail
git rm -r -q RMS-main/.claude/skills/impeccable RMS-main/.claude/settings.json
for d in RMS-main/.claude/skills/*/; do git mv "$d" .claude/skills/; done
for f in RMS-main/* RMS-main/.github RMS-main/.gitignore RMS-main/.gitattributes RMS-main/.impeccable; do git mv "$f" .; done
mkdir -p docs/design/referencias-atera
for f in UI/*; do git mv "$f" docs/design/referencias-atera/; done
git status --porcelain | grep -v '^R ' | grep -v '^D ' || true
ls RMS-main UI 2>&1 | head -2
```

Expected: el `grep` final no imprime nada, porque todo es rename (`R`) o borrado (`D`), y `ls` responde que `RMS-main` y `UI` no existen.

- [ ] **Step 4: Comprobar que ningún contenido cambió**

Run: `git diff --cached -M --stat | tail -1 && git diff --cached -M --diff-filter=M --name-only`
Expected: la segunda orden no imprime nada, porque ningún archivo aparece como modificado, solo renombrado o borrado.

- [ ] **Step 5: Comprobar que las rutas de la CI resuelven desde la raíz**

Run: `for d in backend frontend agent deploy scripts; do test -d $d && echo ok $d; done; test -f .github/workflows/ci.yml && echo ok ci`
Expected: seis líneas `ok`.

- [ ] **Step 6: Commit y push**

```bash
git commit -q -m "Move the project from RMS-main/ to the repository root

GitHub Actions only reads .github/workflows/ at the repository root, so
no CI job has run since the first commit. Contents are unchanged: every
entry is a rename, apart from two deletions:

- RMS-main/.claude/skills/impeccable (4.1.1), superseded by the 4.7.0
  install already at the root.
- RMS-main/.claude/settings.json, whose SessionStart hook runs
  .claude/hooks/session-start, a script that is not in the repository.

UI/ (Atera reference screenshots and walkthrough) moves to
docs/design/referencias-atera/."
git push -u origin claude/compassionate-bardeen-vgtwus
```

### Task 2: Línea base de la CI

- [ ] **Step 1: Esperar a que termine la CI del PR.** El aviso de GitHub llega solo; no se consulta en bucle.
- [ ] **Step 2: Registrar el resultado de cada job** de `ci.yml` (`changes`, `backend`, `migrations`, `deploy`, `frontend`, `harmony`, `harmony-gate`, `agent`, `agent-windows`, `agent-macos`, `agent-winrm`) en `docs/superpowers/plans/2026-10-23-msp-clientes-paso0-raiz.md`, sección "Línea base", con el enlace de la ejecución.
- [ ] **Step 3: Por cada job en rojo, encontrar la causa raíz.** Si un job falla por el movimiento (una ruta que asumía `RMS-main/`), se corrige en este PR. Si falla por algo que ya estaba roto antes del movimiento, se documenta en la línea base con la causa, sin arreglarlo aquí: se arregla en un PR aparte antes de la fase 1.

## Línea base

**Hallazgo 1 (2026-10-23): ningún archivo del primer commit era ejecutable.**
`git ls-tree -r 2e1ff7b` muestra los 3.122 archivos con modo `100644`, así que
la subida inicial perdió el bit de ejecución. La primera ejecución de la CI
(run 39981024034) falló en `agent-update-e2e (ubuntu-latest)` con
`./internal/update/e2e/build.sh: Permission denied`. El propio repo exige ese
bit (`agent/packaging/packaging_test.go:300-333`).

Se corrige en este mismo PR, en un commit aparte: se marcan como ejecutables
los 49 archivos versionados que empiezan con `#!`. Es una excepción a la regla
de la Task 2 de arreglar en un PR aparte, porque esta sesión solo puede
publicar en la rama `claude/compassionate-bardeen-vgtwus`, y porque sin esta
corrección ninguna línea base sería útil. Después de la corrección,
`go test ./packaging/ -run Executable` pasa en local.

El resto de la línea base se completa cuando termine la CI con la corrección.
