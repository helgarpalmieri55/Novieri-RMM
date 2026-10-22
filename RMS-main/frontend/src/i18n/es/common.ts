import type { Shape } from "../types";
import type { common as en } from "../en/common";

export const common: Shape<typeof en> = {
  actions: { save: "Guardar", cancel: "Cancelar", close: "Cerrar", retry: "Reintentar" },
  count: {
    selected: { one: "{count} seleccionado", other: "{count} seleccionados" },
    items: { one: "{count} elemento", other: "{count} elementos" },
  },
  help: { docsLink: "Consulte <link>la guía</link> para más detalles.", twoLinks: "<a>Uno</a><b>Dos</b>" },
  language: { label: "Idioma", english: "English", spanish: "Español" },
};
