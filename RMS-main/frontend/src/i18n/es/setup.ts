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
    passwordHint: "Recibirás un enlace para elegir una contraseña. Aquí no se introduce ninguna contraseña.",
    unreachable: "No se pudo contactar con el servidor, así que no se creó nada. {reason}",
  },
  claim: {
    title: "Configuración completada",
    belongsTo: "Esta instalación ahora pertenece a tu organización, y <b>{email}</b> es su administrador.",
    checkInbox: "Revisa esa bandeja de entrada: encontrarás un enlace para elegir una contraseña. Después, inicia sesión.",
    invitationFailed: "No se pudo enviar el correo de invitación, así que no ha llegado ningún enlace de inicio de sesión.",
    goToSignIn: "Ir a iniciar sesión",
    signInFailed: "No se pudo iniciar sesión. {reason}",
  },
};
