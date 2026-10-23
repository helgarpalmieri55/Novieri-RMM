import type { Shape } from "../types";
import type { validation as en } from "../en/validation";

export const validation: Shape<typeof en> = {
  required: "El campo {label} es obligatorio",
  saved: "Guardado.",
  unexplained: "El servidor rechazó la solicitud y no dio ningún motivo. Inténtalo de nuevo.",
  unauthorized: "no autorizado",
  serviceUnavailable: "Ese servicio no está disponible en este momento. No se cambió nada: inténtalo de nuevo en breve.",
  forbidden: "No tienes permiso para hacer eso.",
  notFound: "Eso ya no existe. Puede que se haya eliminado.",
  conflict: "Eso entra en conflicto con algo que ya existe.",
  serverFailed: "El servidor falló con esta solicitud. No se cambió nada: inténtalo de nuevo en breve.",
  type: {
    missing: "Este campo es obligatorio.",
    stringTooShort: {
      one: "Demasiado corto (mínimo {min_length} carácter).",
      other: "Demasiado corto (mínimo {min_length} caracteres).",
    },
    stringTooLong: {
      one: "Demasiado largo (máximo {max_length} carácter).",
      other: "Demasiado largo (máximo {max_length} caracteres).",
    },
    notANumber: "Debe ser un número.",
    atLeast: "Debe ser al menos {ge}.",
    atMost: "Debe ser como máximo {le}.",
    notAllowed: "No es uno de los valores permitidos.",
  },
};
