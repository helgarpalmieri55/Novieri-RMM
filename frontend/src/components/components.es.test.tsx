/**
 * The shared components in Spanish: the copy they carry themselves (empty
 * states, confirmation, form messages, menus, prerequisites, pickers, the
 * week painter, charts, dates). Each `it` renders a component with Spanish
 * props and asserts no English catalogue value is on the page, plus the
 * Spanish phrases that component owns, so an empty render cannot pass.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { setActiveLanguage } from "../i18n/core";
import { expectNoEnglish, renderInSpanish } from "../i18n/test-utils";
import { ApiError } from "../lib/api";
import type { MePermissions } from "../lib/mePermissions";
import { Attributes } from "./attributes";
import { BarChart, LineChart } from "./chart";
import { ConfirmDialog } from "./confirm";
import { DurationInput } from "./duration";
import { EmptyState, NoValue } from "./emptystate";
import { describeSubmitError, Form, FormActions, requiredProblems } from "./form";
import { EntityPicker } from "./picker";
import { prerequisiteFromQuery, Prerequisites, type Prerequisite } from "./prerequisites";
import { RelTime } from "./reltime";
import { RowMenu } from "./rowmenu";
import { Dialog, Table, Td, Th } from "./ui";
import { WeekPainter } from "./weekpainter";

const unprocessable = (detail: unknown[]) => new ApiError(422, JSON.stringify({ detail }));

describe("empty states and dialogs in Spanish", () => {
  it("shows an empty state and an absent value with the copy their caller gives", () => {
    const { container } = renderInSpanish(
      <div>
        <EmptyState icon="devices" title="Aún no hay dispositivos" body="Agrega un equipo para empezar." />
        <NoValue label="Sin dato" />
      </div>,
    );
    expect(screen.getByRole("heading", { name: "Aún no hay dispositivos" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("asks for a typed confirmation in Spanish, and Cancel is Cancelar", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const { container } = renderInSpanish(
      <ConfirmDialog
        open
        title="Eliminar repositorio"
        body="Esto no se puede deshacer."
        subject="nightly"
        confirmLabel="Eliminar"
        destructive
        requireTyping
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    expect(container.querySelector(".confirm-typing span")?.textContent).toBe("Escribe nightly para confirmar");
    expect(screen.getByText("nightly", { selector: "strong" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Eliminar" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByRole("textbox"), "nightly");
    expect(confirm).toBeEnabled();
    expectNoEnglish(container);
  });

  it("names a dialog's close button in Spanish", () => {
    const { container } = renderInSpanish(
      <Dialog open onClose={vi.fn()} title="Detalle">
        <p>cuerpo</p>
      </Dialog>,
    );
    expect(screen.getByRole("button", { name: "Cerrar" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("names an unlabelled table column Acciones", () => {
    const { container } = renderInSpanish(
      <Table label="Dispositivos">
        <thead>
          <tr>
            <Th>Nombre</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          <tr>
            <Td>uno</Td>
            <Td>x</Td>
          </tr>
        </tbody>
      </Table>,
    );
    expect(screen.getByRole("columnheader", { name: "Acciones" })).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("form messages in Spanish", () => {
  it("renders the required-field problems, the saved note and the pending label", () => {
    setActiveLanguage("es");
    const problems = requiredProblems([{ name: "name", label: "nombre" }], { name: " " });
    const { container } = render(
      <Form onSubmit={vi.fn()}>
        <FormActions label="Guardar" pending={false} problems={problems} saved />
      </Form>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("El campo nombre es obligatorio.");
    expect(screen.getByRole("status")).toHaveTextContent("Guardado.");
    expectNoEnglish(container);
  });

  it("maps Pydantic error types to Spanish sentences, naming the field", () => {
    setActiveLanguage("es");
    const message = describeSubmitError(
      unprocessable([
        { type: "missing", loc: ["body", "name"], msg: "Field required" },
        { type: "string_too_short", loc: ["body", "password"], msg: "String should have at least 12 characters", ctx: { min_length: 12 }, input: "hunter2" },
        { type: "string_too_short", loc: ["body", "code"], msg: "String should have at least 1 character", ctx: { min_length: 1 } },
        { type: "string_too_long", loc: ["body", "title"], msg: "String should have at most 300 characters", ctx: { max_length: 300 } },
        { type: "int_parsing", loc: ["body", "ttl_hours"], msg: "Input should be a valid integer" },
        { type: "float_parsing", loc: ["body", "ratio"], msg: "Input should be a valid number" },
        { type: "greater_than_equal", loc: ["body", "count"], msg: "Input should be greater than or equal to 1", ctx: { ge: 1 } },
        { type: "less_than_equal", loc: ["body", "limit"], msg: "Input should be less than or equal to 50", ctx: { le: 50 } },
        { type: "enum", loc: ["body", "kind"], msg: "Input should be 'a' or 'b'" },
        { type: "literal_error", loc: ["body", "mode"], msg: "Input should be 'x'" },
        { type: "value_error", loc: ["body", "prefix"], msg: "Value error, el prefijo no es un CIDR válido" },
      ]),
    );
    expect(message).toBe(
      [
        "Name: Este campo es obligatorio.",
        "Password: Demasiado corto (mínimo 12 caracteres).",
        "Code: Demasiado corto (mínimo 1 carácter).",
        "Title: Demasiado largo (máximo 300 caracteres).",
        "Ttl hours: Debe ser un número.",
        "Ratio: Debe ser un número.",
        "Count: Debe ser al menos 1.",
        "Limit: Debe ser como máximo 50.",
        "Kind: No es uno de los valores permitidos.",
        "Mode: No es uno de los valores permitidos.",
        "Prefix: el prefijo no es un CIDR válido",
      ].join(". "),
    );
    expect(message).not.toContain("hunter2");
  });

  it("falls back to the server's message when it cannot fill a sentence", () => {
    setActiveLanguage("es");
    expect(
      describeSubmitError(
        unprocessable([
          { type: "string_too_short", loc: ["body", "name"], msg: "too short" },
          { type: "some_future_type", loc: ["body", "name"], msg: "algo nuevo" },
          { type: "greater_than_equal", loc: ["body", "n"], msg: "Input should be at least 3", ctx: {} },
        ]),
      ),
    ).toBe("Name: too short. Name: algo nuevo. N: Input should be at least 3");
  });

  it("keeps the server's own English message while the console is in English", () => {
    expect(
      describeSubmitError(
        unprocessable([
          { type: "missing", loc: ["body", "name"], msg: "Field required" },
          { type: "string_too_long", loc: ["body", "title"], msg: "String should have at most 300 characters", ctx: { max_length: 300 } },
          { type: "int_parsing", loc: ["body", "ttl_hours"], msg: "Input should be a valid integer" },
          { type: "value_error", loc: ["body", "prefix"], msg: "Value error, not a valid CIDR" },
        ]),
      ),
    ).toBe(
      "Name: Field required. Title: String should have at most 300 characters. Ttl hours: Input should be a valid integer. Prefix: not a valid CIDR",
    );
  });

  it("explains a status the server gave no words for, in Spanish", () => {
    setActiveLanguage("es");
    expect(describeSubmitError(new ApiError(503, "{}"))).toBe(
      "Ese servicio no está disponible en este momento. No se cambió nada: inténtalo de nuevo en breve.",
    );
    expect(describeSubmitError(new ApiError(403, "{}"))).toBe("No tienes permiso para hacer eso.");
    expect(describeSubmitError(new ApiError(404, "{}"))).toBe("Eso ya no existe. Puede que se haya eliminado.");
    expect(describeSubmitError(new ApiError(409, "{}"))).toBe("Eso entra en conflicto con algo que ya existe.");
    expect(describeSubmitError(new ApiError(500, "{}"))).toBe(
      "El servidor falló con esta solicitud. No se cambió nada: inténtalo de nuevo en breve.",
    );
    expect(describeSubmitError(new ApiError(400, "{}"))).toBe(
      "El servidor rechazó la solicitud y no dio ningún motivo. Inténtalo de nuevo.",
    );
    expect(describeSubmitError(new ApiError(401, "unauthorized"))).toBe("no autorizado");
    expect(describeSubmitError(new Error("sin red"))).toBe("sin red");
    expect(describeSubmitError("???")).toBe(
      "El servidor rechazó la solicitud y no dio ningún motivo. Inténtalo de nuevo.",
    );
  });

  it("prints a server sentence as the server wrote it", () => {
    setActiveLanguage("es");
    expect(describeSubmitError(new ApiError(400, "El repositorio ya existe."))).toBe("El repositorio ya existe.");
  });
});

describe("menus, pickers and prerequisites in Spanish", () => {
  it("names a row menu and its actions in Spanish", async () => {
    const user = userEvent.setup();
    const { container } = renderInSpanish(
      <RowMenu subject="nightly" items={[{ label: "Eliminar", onSelect: vi.fn(), destructive: true }]} />,
    );
    const button = screen.getByRole("button", { name: "Más acciones para nightly" });
    expect(button).toHaveAttribute("title", "Más acciones");
    await user.click(button);
    expect(screen.getByRole("menu", { name: "Acciones para nightly" })).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("speaks Spanish in an entity picker that is loading, empty, or has no match", async () => {
    const user = userEvent.setup();
    const options = [{ id: "a", label: "Alfa" }];
    const { container, rerender } = renderInSpanish(
      <EntityPicker label="Grupo" options={[]} value="" onChange={vi.fn()} loading />,
    );
    const field = screen.getByRole("combobox", { name: "Grupo" });
    expect(field).toHaveAttribute("placeholder", "Buscar…");
    await user.click(field);
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
    rerender(<EntityPicker label="Grupo" options={options} value="" onChange={vi.fn()} />);
    await user.type(field, "zzz");
    expect(screen.getByText("Sin coincidencias.")).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("lays out a prerequisite panel with Spanish pills, permission sentences and failures", () => {
    const me = (codes: readonly string[]): MePermissions => ({
      status: "ready",
      codes,
      has: (code) => codes.includes(code),
      lacks: (code) => !codes.includes(code),
    });
    const bare = { data: undefined, error: null, isSuccess: false, isError: false };
    setActiveLanguage("es");
    const items: Prerequisite[] = [
      { label: "Hay un equipo en el inventario", state: "done", detail: "3 equipos están registrados." },
      {
        label: "Existe un grupo de dispositivos",
        state: "missing",
        severity: "crit",
        detail: "Aún no existe ningún grupo de dispositivos.",
        cta: { to: "/device-groups", label: "Crear un grupo de dispositivos" },
      },
      prerequisiteFromQuery({ label: "Reglas", query: bare, permission: { code: "devices.read", me: me([]) }, isDone: () => true, done: "ok", missing: { detail: "x", severity: "warn" } }),
      prerequisiteFromQuery({ label: "Comprobando", query: bare, isDone: () => true, done: "ok", missing: { detail: "x", severity: "warn" } }),
      prerequisiteFromQuery({
        label: "Falla",
        query: { ...bare, isError: true, error: new ApiError(500, "{}") },
        isDone: () => true,
        done: "ok",
        missing: { detail: "x", severity: "warn" },
      }),
      prerequisiteFromQuery({
        label: "Otro",
        query: { ...bare, isError: true, error: new ApiError(403, "{}") },
        isDone: () => true,
        done: "ok",
        missing: { detail: "x", severity: "warn" },
      }),
    ];
    const { container } = render(
      <MemoryRouter>
        <Prerequisites title="Antes de empezar" items={items} />
      </MemoryRouter>,
    );
    expect(screen.getByText("No se puede comprobar desde esta cuenta: hace falta permiso para ver dispositivos.")).toBeInTheDocument();
    expect(screen.getByText("Comprobando…")).toBeInTheDocument();
    expect(screen.getByText(/No se pudo comprobar\. El servidor falló con esta solicitud/)).toBeInTheDocument();
    expect(screen.getByText("No se puede comprobar desde esta cuenta.")).toBeInTheDocument();
    const rows = screen.getAllByRole("listitem");
    expect(within(rows[0] as HTMLElement).getByText("Hecho")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByRole("link", { name: "Crear un grupo de dispositivos" })).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getByText("Pendiente")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});

describe("inputs and displays in Spanish", () => {
  it("names the duration unit and offers the units in Spanish", () => {
    const { container } = renderInSpanish(<DurationInput value="3600" label="Caducidad" onChange={vi.fn()} />);
    const unit = screen.getByRole("combobox", { name: "Unidad" });
    expect(within(unit).getAllByRole("option").map((o) => o.textContent)).toEqual(["segundos", "minutos", "horas", "días"]);
    expect(unit).toHaveValue("hours");
    expectNoEnglish(container);
  });

  it("describes the painted window, the grid and the cron box in Spanish", () => {
    const { container } = renderInSpanish(<WeekPainter value="0 2-5 * * 1-5" onChange={vi.fn()} label="Ventana" />);
    expect(screen.getByText("Seleccionado: lunes a viernes, 02:00 a 06:00.")).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "lunes" })).toHaveTextContent("Lun");
    expect(screen.getByRole("gridcell", { name: "martes 03:00" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Avanzado")).toBeInTheDocument();
    expect(screen.getByLabelText("Expresión cron")).toBeInTheDocument();
    expect(screen.getByText(/Cinco campos: minuto, hora/)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("says nothing selected, and says a custom expression cannot be drawn, in Spanish", () => {
    const { container, rerender } = renderInSpanish(<WeekPainter value={null} onChange={vi.fn()} label="Ventana" />);
    expect(screen.getByText(/^No hay nada seleccionado\./)).toBeInTheDocument();
    rerender(<WeekPainter value="*/5 * * * *" onChange={vi.fn()} label="Ventana" />);
    expect(screen.getByText(/^Expresión personalizada: la cuadrícula no puede dibujarla\./)).toBeInTheDocument();
    expectNoEnglish(container);
  });

  it("joins two painted runs with y", () => {
    renderInSpanish(<WeekPainter value="0 1,3 * * 0" onChange={vi.fn()} label="Ventana" />);
    expect(screen.getByText("Seleccionado: domingo, 01:00 a 02:00 y 03:00 a 04:00.")).toBeInTheDocument();
  });

  it("gives charts a Spanish data table", () => {
    const { container } = renderInSpanish(
      <div>
        <BarChart title="Gasto" categories={["ene", "feb"]} series={[{ label: "Plan", values: [1, 2] }]} unit="EUR" />
        <LineChart title="Tendencia" categories={["ene", "feb"]} series={[{ label: "Real", values: [1, 2] }]} unit="EUR" />
      </div>,
    );
    expect(screen.getAllByRole("columnheader", { name: "Categoría" })).toHaveLength(2);
    expectNoEnglish(container);
  });

  it("writes attributes and relative times in Spanish", () => {
    const now = Date.parse("2026-10-09T14:05:00Z");
    const { container } = renderInSpanish(
      <div>
        <Attributes value={{ estado: "in_progress", activo: true, bloqueado: false }} empty="Sin atributos" />
        <RelTime iso="2026-10-09T14:00:00Z" now={now} />
        <RelTime iso={null} />
      </div>,
    );
    expect(screen.getByText("En curso")).toBeInTheDocument();
    expect(screen.getByText("Sí")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByText("Ver sin formato")).toBeInTheDocument();
    expect(screen.getByText("hace 5 minutos")).toBeInTheDocument();
    expectNoEnglish(container);
  });
});
