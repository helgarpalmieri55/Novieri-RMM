import type { Shape } from "../types";
import type { setup as en } from "../en/setup";

export const setup: Shape<typeof en> = {
  page: {
    title: "Configura esta instalación",
    intro:
      "Todavía nadie administra esta instalación. Introduce el código de instalación del registro del servidor y se creará para ti la primera cuenta de administrador.",
    installCode: "Código de instalación",
    yourName: "Tu nombre",
    yourEmail: "Tu correo electrónico",
    organizationName: "Nombre de la organización",
    settingUp: "Configurando…",
    finish: "Finalizar configuración",
    passwordHint: "El primer administrador inicia sesión con este correo y esta contraseña, y añade un segundo factor al entrar por primera vez.",
    unreachable: "No se pudo contactar con el servidor, así que no se creó nada. {reason}",
  },
};
