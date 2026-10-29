import type { Shape } from "../types";
import type { components as en } from "../en/components";

export const components: Shape<typeof en> = {
  ui: {
    actions: "Acciones",
    scrollForMore: "Desplázate para ver más columnas",
  },
  confirm: {
    typeToConfirm: "Escribe <strong>{subject}</strong> para confirmar",
  },
  attributes: {
    yes: "Sí",
    no: "No",
    viewRaw: "Ver sin formato",
  },
  picker: {
    search: "Buscar…",
    loading: "Cargando…",
    noMatch: "Sin coincidencias.",
  },
  rowMenu: {
    moreActionsFor: "Más acciones para {subject}",
    moreActions: "Más acciones",
    actionsFor: "Acciones para {subject}",
  },
  duration: {
    unit: "Unidad",
    seconds: "segundos",
    minutes: "minutos",
    hours: "horas",
    days: "días",
  },
  chart: {
    category: "Categoría",
  },
  forEveryClient: {
    label: "Para todos los clientes",
    hint: "Lo verán los técnicos de todos los clientes. Sin marcar, es del cliente propio de tu organización.",
  },
  prerequisites: {
    checking: "Comprobando…",
    cannotCheckNeeds: "No se puede comprobar desde esta cuenta: hace falta {permissions}.",
    cannotCheck: "No se puede comprobar desde esta cuenta.",
    couldNotCheck: "No se pudo comprobar. {reason}",
  },
  weekPainter: {
    day: {
      sunday: "domingo",
      monday: "lunes",
      tuesday: "martes",
      wednesday: "miércoles",
      thursday: "jueves",
      friday: "viernes",
      saturday: "sábado",
    },
    dayShort: {
      sunday: "Dom",
      monday: "Lun",
      tuesday: "Mar",
      wednesday: "Mié",
      thursday: "Jue",
      friday: "Vie",
      saturday: "Sáb",
    },
    dayColumn: "Día",
    rangeTo: "{from} a {to}",
    and: "y",
    nothingSelected: "No hay nada seleccionado. Pinta las horas en que debe abrirse la ventana, o déjalo vacío para que esté siempre abierta.",
    custom: "Expresión personalizada: la cuadrícula no puede dibujarla. Edítala en Avanzado.",
    selected: "Seleccionado: {summary}.",
    advanced: "Avanzado",
    cronExpression: "Expresión cron",
    cronHelp:
      "Cinco campos: minuto, hora, día del mes, mes y día de la semana. La cuadrícula dibuja las expresiones cuyo minuto es 0 y cuyo día del mes y mes son ambos *.",
  },
};
