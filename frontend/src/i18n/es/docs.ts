import type { Shape } from "../types";
import type { docs as en } from "../en/docs";

export const docs: Shape<typeof en> = {
  page: {
    title: "Documentación de TI",
    description: "Manuales de procedimiento, registros y archivos, vinculados a los dispositivos que describen.",
    newArticle: "Artículo nuevo",
    tiles: {
      articles: "Artículos",
      articlesZero: "todavía no hay nada escrito",
      records: "Registros",
      recordsZero: "todavía no hay registros de redes, servicios, proveedores ni ubicaciones",
      linked: "Referencias vinculadas",
      linkedZero: "todavía no hay ningún artículo vinculado a un objeto",
      documents: "Documentos",
      documentsZero: "todavía no se ha subido ningún archivo",
    },
    foot: {
      categories: { one: "En {count} categoría", other: "En {count} categorías" },
      linkedFrom: {
        one: "De {linked} de {count} artículo",
        other: "De {linked} de {count} artículos",
      },
      everyDocumentHasFile: "Todos los documentos tienen archivo",
      documentsAwaiting: {
        one: "{count} documento sin archivo subido todavía",
        other: "{count} documentos sin archivo subido todavía",
      },
      recordType: {
        network: { one: "{count} red", other: "{count} redes" },
        service: { one: "{count} servicio", other: "{count} servicios" },
        vendor: { one: "{count} proveedor", other: "{count} proveedores" },
        location: { one: "{count} ubicación", other: "{count} ubicaciones" },
        other: { one: "{count} {type}", other: "{count} {type}" },
      },
    },
    linksLine: {
      one: "vinculado a {count} objeto",
      other: "vinculado a {count} objetos",
    },
    list: {
      searchLabel: "Buscar en la documentación",
      searchPlaceholder: "Buscar artículos, hosts, IP",
      clearSearch: "Borrar la búsqueda",
      clear: "Borrar",
      filterByCategory: "Filtrar por categoría",
      all: "Todas",
      count: { one: "{count} artículo", other: "{count} artículos" },
      searchNote:
        "Busca en títulos, descripciones, categorías y en el nombre del archivo actual. No busca dentro de los archivos almacenados ni en los nombres de archivo de versiones anteriores.",
      noMatch: "Ninguna documentación coincide con esa búsqueda.",
      noArticles: "Todavía no hay artículos.",
      noneInCategory: "No hay ningún artículo en {category}.",
      searchResults: "Resultados de la búsqueda",
      groupArticles: "Artículos",
      groupRecords: "Registros",
      groupDocuments: "Documentos",
      name: "Nombre",
      updated: "Actualizado",
    },
    records: {
      title: "Registros",
      newRecord: "Registro nuevo",
      empty: "Todavía no hay registros.",
    },
  },
  editor: {
    editTitle: "Editar artículo",
    newTitle: "Artículo nuevo",
    title: "Título",
    category: "Categoría",
    markdown: "Markdown",
    preview: "Vista previa",
    cancel: "Cancelar",
    save: "Guardar",
  },
  article: {
    updated: "actualizado {date}",
    history: "Historial",
    edit: "Editar",
    archive: "Archivar",
    archiveTitle: "Archivar artículo",
    archiveBody:
      "El artículo sale de la biblioteca y de la búsqueda. Su historial de versiones se conserva, pero solo un administrador puede restaurarlo.",
    archiveConfirm: "Archivar artículo",
    archiveFailed: "No se archivó este artículo. {reason}",
    linkedTo: "Vinculado a: {types}",
    historyFailed:
      "No se pudo cargar el historial de versiones, así que esto no es una lista de las ediciones de este artículo. {reason}",
    loading: "Cargando…",
    noEarlier: "No hay versiones anteriores.",
    version: "v{version} — {date}",
    loadFailed: "No se pudo cargar el artículo. {reason}",
  },
  portal: {
    visibleNote: "Visible en el portal.",
    toggle: "Visible en el portal",
    explain:
      "Quienes crean tickets pueden encontrarlo y leerlo cuando comunican un problema. Déjalo desactivado para todo lo que sea interno.",
    notSaved: "No se guardó. {reason}",
  },
  record: {
    title: "Registro nuevo",
    type: "Tipo",
    movedOption: "{type}: ahora en la gestión de direcciones IP",
    networkNote:
      "Los registros de red ahora están en la gestión de direcciones IP. <link>Crea allí una subred</link>. Los registros de red existentes se pueden seguir leyendo, editando y archivando aquí.",
    name: "Nombre",
    fields: "Campos (JSON)",
    cancel: "Cancelar",
    create: "Crear",
  },
  validation: {
    tooLong: "{label}: máximo {max} caracteres",
    notJson: "Campos (JSON): no es un JSON válido",
    notObject: 'Campos (JSON): debe ser un objeto JSON, como {"vlan": 10}',
    targetNotUuid: "ID de destino: debe ser un UUID, tal como aparece en la URL de ese objeto",
    chooseFile: "Elige un archivo",
    fileTooBig: "{name} ocupa {size}. El límite es de {limit} por archivo.",
    fileEmpty: "{name} está vacío",
  },
  documents: {
    title: "Documentos",
    upload: "Subir documento",
    empty: "Todavía no hay documentos.",
    noFileYet: "sin archivo todavía",
    links: { one: "{count} vínculo", other: "{count} vínculos" },
    loading: "Cargando…",
    loadFailed: "No se pudo cargar este documento, así que nada de lo que hay debajo lo describe. {reason}",
    archivedPill: "archivado",
    history: "Historial",
    linksButton: "Vínculos",
    uploadNewVersion: "Subir versión nueva",
    editDetails: "Editar detalles",
    archive: "Archivar",
    purgeVersion: "Purgar v{version}",
    archivedNotice:
      "Este documento está archivado. Está fuera de la biblioteca y de la búsqueda, y no admite versiones nuevas. Sus archivos siguen intactos y se pueden descargar.",
    archiveFailed: "No se archivó este documento. {reason}",
    purgeFailed: "No se destruyó nada. {reason}",
    noFile:
      "Todavía no se ha subido ningún archivo. Este documento tiene un título, una categoría y sus vínculos; el archivo es un segundo paso.",
    uploadFile: "Subir un archivo",
    archiveTitle: "Archivar documento",
    archiveBody:
      "El documento sale de la biblioteca y de la búsqueda. Sus archivos se pueden seguir descargando y solo un administrador puede restaurarlo. No se destruye nada.",
    archiveConfirm: "Archivar documento",
    purgeTitle: "Destruir estos bytes",
    purgeSubject: "Versión {version} — {filename}, {size}",
    purgeBody:
      "El archivo se elimina del almacenamiento y no se puede recuperar: sin deshacer y sin ninguna copia de seguridad a la que llegue esta consola. El nombre del archivo, el tamaño, el hash y quién lo hizo se conservan en el historial de versiones y en el registro de auditoría.",
    purgeConfirm: "Destruir los bytes",
    typeText: "Texto",
    versions: {
      failed:
        "No se pudo cargar el historial de versiones, así que esto no es una lista de los archivos de este documento. {reason}",
      none: "Todavía no se ha subido ningún archivo.",
      version: "Versión",
      file: "Archivo",
      size: "Tamaño",
      type: "Tipo",
      uploaded: "Subida",
      current: "actual",
      shown: "mostrada",
      purged: "purgada {date}",
      view: "Ver",
      purge: "Purgar",
    },
    linkEditor: {
      title: "Vínculos",
      none: "Todavía no está vinculado a nada. Un documento sin vínculos es un documento de nivel de organización, que es lo que muestra la lista sin filtrar.",
      unlink: "Desvincular",
      unlinkFailed: "No se desvinculó nada. {reason}",
      linkTo: "Vincular a",
      targetId: "ID de destino",
      link: "Vincular",
      linkFailed: "No se vinculó nada.",
      note: "Un vínculo a una credencial nombra la credencial y nunca su secreto. Un destino de otra organización no se distingue de uno que no existe; ambos se rechazan del mismo modo.",
    },
    dialog: {
      uploadTitle: "Subir documento",
      title: "Título",
      category: "Categoría",
      description: "Descripción",
      file: "Archivo",
      accepted:
        "Se aceptan: PDF; imágenes PNG, JPEG, GIF y WebP; archivos ZIP y de Office; y texto UTF-8 de cualquier tipo, incluidos Markdown, CSV, HTML, SVG y XML, que se almacenan y se muestran como texto fuente, nunca interpretados. El tipo se lee de los bytes del propio archivo, así que la extensión no lo decide. Cualquier otra cosa: comprímela en un ZIP. Los archivos no se analizan en busca de malware.",
      createdWithoutFile:
        "Se creó el documento, pero no el archivo. Está en la biblioteca sin archivo; ábrelo y sube uno.",
      cancel: "Cancelar",
      upload: "Subir",
      uploadRefused: "Se rechazó la subida.",
      newVersionTitle: "Subir versión nueva",
      newVersionNote:
        "El archivo anterior nunca se toca: se queda en el historial y se puede seguir descargando. Para restaurar una versión anterior, descárgala y vuelve a subirla.",
      editTitle: "Editar detalles",
      editNote:
        "Esto cambia el título, la categoría y la descripción del documento. No toca ningún archivo subido.",
      save: "Guardar",
      saveFailed: "No se guardó nada.",
    },
  },
  viewer: {
    download: "Descargar",
    tryAgain: "Reintentar",
    notDownloaded: "No se descargó {filename}. {reason}",
    auditNote:
      "Abrir un documento queda registrado en el registro de auditoría como una descarga, igual que al pulsar Descargar.",
    destroyed:
      "<b>Estos bytes fueron destruidos.</b> La versión {version} ({filename}, {size}) se purgó y no se puede recuperar desde esta plataforma. El registro de la destrucción, quién la hizo y cuándo, se conserva en el registro de auditoría.",
    noPreview:
      "Esta consola no muestra una vista previa de los archivos {type}. Descarga {filename} para abrirlo con una aplicación que pueda.",
    loading: "Cargando {filename}…",
    readFailed: "No se pudo leer {filename}. {reason}",
    embedHint:
      "Si la vista previa no carga, descarga el archivo; algunos navegadores no insertan PDF.",
    noPreviewShort:
      "Esta consola no muestra una vista previa de los archivos {type}. Descarga {filename} para abrirlo.",
  },
  markdown: {
    linkRemoved: "Vínculo eliminado: esquema de URL no admitido.",
  },
};
