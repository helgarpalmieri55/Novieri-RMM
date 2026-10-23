import type { Shape } from "../types";
import type { onboarding as en } from "../en/onboarding";

export const onboarding: Shape<typeof en> = {
  page: {
    title: "Añadir un equipo",
    description: "Instala el agente en un equipo o encuentra todos los equipos de una red.",
    oneComputer: {
      title: "Un equipo",
      subtitle: "Descarga el instalador de su plataforma o copia un comando de instalación de una sola línea.",
      getInstaller: "Obtener el instalador",
      enrollmentTokens: "Tokens de inscripción",
      note: "De cualquier forma, el equipo genera su propia clave, que nunca sale de él, y recibe su propio certificado. Aparece en Dispositivos en cuanto se conecta por primera vez.",
    },
    manyNote:
      "Muchos equipos en una red: esta plataforma nunca se conecta a un equipo, así que el primero de un segmento nuevo siempre se instala a mano. Ese equipo pasa a ser el que barre la red y encuentra el resto.",
    manyTitle: "Muchos equipos en una red",
    needsAdmin: "Esto debe hacerlo un administrador. Requiere {permissions}, que tu cuenta no tiene.",
    youAreHere: "Estás aquí",
    networks: {
      title: "Tus redes",
      subtitle: "Elige una para ver si su último barrido pudo ejecutarse.",
      empty: "Aún no hay ninguna red registrada. Es el paso 1, arriba.",
      network: "Red",
      lastSwept: "Último barrido",
      unnamed: "sin nombre",
      selected: "Seleccionada",
      checkThisOne: "Revisar esta",
      selectedAria: "{prefix} seleccionada",
      checkAria: "Revisar {prefix}",
    },
  },
  state: {
    done: "Hecho",
    partial: "Hecho en parte",
    blocked: "Bloqueado",
    unknown: "Comprobando…",
  },
  journey: {
    register: {
      title: "Registra la red",
      toLabel: "Abrir la administración de direcciones IP",
      loading: "Cargando tus redes registradas…",
      none: "No hay ninguna red registrada.",
      noneRemedy:
        "Un barrido se dirige a una subred registrada, así que todo empieza aquí. Registra el prefijo en el que quieres encontrar equipos.",
      done: { one: "{count} red registrada.", other: "{count} redes registradas." },
    },
    authorize: {
      title: "Autoriza aquí el escaneo",
      toLabel: "Abrir las autorizaciones de escaneo",
      loading: "Cargando las autorizaciones de escaneo…",
      nothingYet: "Aún no hay nada que autorizar: no hay ninguna red registrada.",
      finishStep1: "Termina antes el paso 1.",
      remedy:
        "El escaneo está denegado por defecto: registrar una subred no autoriza sondearla, y un barrido sin una autorización que lo cubra se rechaza antes de crear ninguna tarea. Una persona con este permiso pone su nombre en cada paquete que emite esta plataforma.",
      none: "Ninguna red registrada está autorizada para el escaneo.",
      partial: "{covered} de {total} redes están autorizadas para el escaneo.",
      done: { one: "{count} red registrada está autorizada.", other: "Las {count} redes registradas están autorizadas." },
    },
    sweep: {
      title: "Barre la red para ver qué hay",
      loading: "Cargando el historial de barridos…",
      noneAuthorized: "No hay ninguna red autorizada para barrer.",
      noneAuthorizedRemedy: "Termina antes el paso 2. Un barrido fuera de una autorización se rechaza, no se intenta.",
      thisNetwork: "esta red",
      noAgent: "El último barrido de {network} no pudo ejecutarse: no hay ningún agente inscrito en ese segmento.",
      noAgentRemedy:
        "Un barrido lo realiza un agente que está dentro del segmento. Esta plataforma nunca sondea una red por sí misma. Instala el agente a mano en un equipo de esa red (el paso 5 tiene el comando) y ese equipo se encargará de barrer el resto.",
      noAgentToLabel: "Obtener un comando de instalación",
      chooseNetwork: "Elegir una red",
      open: "Abrir {prefix}",
      noneSwept: "No se ha barrido ninguna red autorizada.",
      noneSweptRemedy: "Ejecuta un barrido desde la página de la subred. Envía ICMP/ARP desde un agente que ya está dentro del segmento.",
      partial: "Se han barrido {swept} de {total} redes autorizadas.",
      partialRemedy: "Barre el resto o déjalas como están: un barrido mantiene la documentación al día, no es monitorización.",
      done: { one: "Se ha barrido {count} red autorizada.", other: "Se han barrido las {count} redes autorizadas." },
    },
    review: {
      title: "Revisa lo que respondió",
      toLabel: "Abrir Descubiertos",
      loading: "Cargando lo que encontraron los últimos barridos…",
      blind: {
        one: "No se pudo barrer {count} red en absoluto. Nada ha mirado allí, lo cual no es lo mismo que no haber nada.",
        other: "No se pudieron barrer {count} redes en absoluto. Nada ha mirado allí, lo cual no es lo mismo que no haber nada.",
      },
      alsoInFleet:
        "Un barrido también puede escribir lo que encontró directamente en la lista de equipos; esas filas se marcan como registradas por un barrido y nadie ha confirmado que sean equipos. Revisa ambas.",
      nothingWaiting: "No hay nada pendiente de revisión en Descubiertos.",
      goBackStep3: "Vuelve al paso 3 para ver las redes que nadie pudo examinar.",
      answered: {
        one: "{count} dirección respondió y el inventario no la tiene registrada.",
        other: "{count} direcciones respondieron y el inventario no las tiene registradas.",
      },
      answeredRemedy:
        "Compruébalas antes de instalar un agente en nada. Una respuesta a un ping no es un equipo: un barrido devuelve impresoras, teléfonos, el portátil de un compañero en la VLAN equivocada y la cafetera, y Descubiertos indica lo sólida que es la evidencia de cada uno.",
    },
    agent: {
      title: "Lleva el agente a esos equipos",
      toLabel: "Abrir Paquetes del agente",
      loading: "Cargando el catálogo de paquetes…",
      none: "Ninguna versión publicada tiene un artefacto cargado.",
      noneRemedy:
        "Esta plataforma nunca compila ni descarga el agente. El catálogo contiene los bytes que alguien publicó. No se puede instalar nada hasta que haya uno.",
      ready: { one: "{count} versión lista para instalar.", other: "{count} versiones listas para instalar." },
      readyRemedy:
        "Un equipo cada vez: un comando de instalación con un token de un solo uso. Para un segmento entero a la vez, usa el despliegue administrado: GPO o Intune, sin ninguna credencial en el paquete.",
    },
  },
};
