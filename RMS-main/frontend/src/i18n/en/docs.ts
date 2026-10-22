/**
 * IT documentation: the library page (articles, records, documents), the
 * article editor, the document view with its version history, links and
 * dialogs, the viewer, and the portal switch. Article and document titles,
 * categories, bodies, descriptions, filenames and record names are user data
 * and are never catalogued; only the interface around them is. The two link /
 * record vocabularies read through `enums.DocLinkTarget` and
 * `enums.DocRecordType`. The English link-target entries are the wire words
 * themselves, because that is what the page has always printed.
 */
export const docs = {
  page: {
    title: "IT documentation",
    description: "Runbooks, records and files, linked to the devices they describe.",
    newArticle: "New article",
    tiles: {
      articles: "Articles",
      articlesZero: "nothing written yet",
      records: "Records",
      recordsZero: "no network, service, vendor or location records yet",
      linked: "Linked references",
      linkedZero: "no article is linked to an object yet",
      documents: "Documents",
      documentsZero: "no files uploaded yet",
    },
    foot: {
      categories: { one: "In {count} category", other: "In {count} categories" },
      linkedFrom: {
        one: "From {linked} of {count} article",
        other: "From {linked} of {count} articles",
      },
      everyDocumentHasFile: "Every document has a file",
      documentsAwaiting: {
        one: "{count} document with no file uploaded yet",
        other: "{count} documents with no file uploaded yet",
      },
      recordType: {
        network: { one: "{count} network", other: "{count} networks" },
        service: { one: "{count} service", other: "{count} services" },
        vendor: { one: "{count} vendor", other: "{count} vendors" },
        location: { one: "{count} location", other: "{count} locations" },
        other: { one: "{count} {type}", other: "{count} {type}s" },
      },
    },
    linksLine: {
      one: "linked to {count} object",
      other: "linked to {count} objects",
    },
    list: {
      searchLabel: "Search documentation",
      searchPlaceholder: "Search articles, hosts, IPs",
      clearSearch: "Clear search",
      clear: "Clear",
      filterByCategory: "Filter by category",
      all: "All",
      count: { one: "{count} article", other: "{count} articles" },
      searchNote:
        "Searches titles, descriptions, categories and the current file's name. It does not search inside stored files, and it does not search older versions' filenames.",
      noMatch: "No documentation matches that search.",
      noArticles: "No articles yet.",
      noneInCategory: "No article in {category}.",
      searchResults: "Search results",
      groupArticles: "Articles",
      groupRecords: "Records",
      groupDocuments: "Documents",
      name: "Name",
      updated: "Updated",
    },
    records: {
      title: "Records",
      newRecord: "New record",
      empty: "No records yet.",
    },
  },
  editor: {
    editTitle: "Edit article",
    newTitle: "New article",
    title: "Title",
    category: "Category",
    markdown: "Markdown",
    preview: "Preview",
    cancel: "Cancel",
    save: "Save",
  },
  article: {
    updated: "updated {date}",
    history: "History",
    edit: "Edit",
    archive: "Archive",
    archiveTitle: "Archive article",
    archiveBody:
      "The article leaves the library and search. Its version history is kept, but only an administrator can restore it.",
    archiveConfirm: "Archive article",
    archiveFailed: "This article was not archived. {reason}",
    linkedTo: "Linked to: {types}",
    historyFailed:
      "The version history could not be loaded, so this is not a list of the edits to this article. {reason}",
    loading: "Loading…",
    noEarlier: "No earlier versions.",
    version: "v{version} — {date}",
    loadFailed: "The article could not be loaded. {reason}",
  },
  portal: {
    visibleNote: "Visible in the portal.",
    toggle: "Visible in the portal",
    explain:
      "People who raise tickets can find and read it when they report a problem. Leave it off for anything internal.",
    notSaved: "Not saved. {reason}",
  },
  record: {
    title: "New record",
    type: "Type",
    movedOption: "{type} (moved to IP address management)",
    networkNote:
      "Network records now live in IP address management. <link>Create a subnet there</link>. Existing network records stay readable, editable and archivable here.",
    name: "Name",
    fields: "Fields (JSON)",
    cancel: "Cancel",
    create: "Create",
  },
  validation: {
    tooLong: "{label} must be {max} characters or fewer",
    notJson: "Fields (JSON) is not valid JSON",
    notObject: 'Fields (JSON) must be a JSON object, like {"vlan": 10}',
    targetNotUuid: "Target id must be a UUID, as it appears in that object's URL",
    chooseFile: "Choose a file",
    fileTooBig: "{name} is {size}. The limit is {limit} per file.",
    fileEmpty: "{name} is empty",
  },
  documents: {
    title: "Documents",
    upload: "Upload document",
    empty: "No documents yet.",
    noFileYet: "no file yet",
    links: { one: "{count} link", other: "{count} links" },
    loading: "Loading…",
    loadFailed: "This document could not be loaded, so nothing below describes it. {reason}",
    archivedPill: "archived",
    history: "History",
    linksButton: "Links",
    uploadNewVersion: "Upload new version",
    editDetails: "Edit details",
    archive: "Archive",
    purgeVersion: "Purge v{version}",
    archivedNotice:
      "This document is archived. It is out of the library and out of search, and it accepts no new versions. Its files are untouched and still downloadable.",
    archiveFailed: "This document was not archived. {reason}",
    purgeFailed: "Nothing was destroyed. {reason}",
    noFile:
      "No file uploaded yet. This document has a title, a category and its links; the file is a second step.",
    uploadFile: "Upload a file",
    archiveTitle: "Archive document",
    archiveBody:
      "The document leaves the library and search. Its files stay downloadable, and only an administrator can restore it. Nothing is destroyed.",
    archiveConfirm: "Archive document",
    purgeTitle: "Destroy these bytes",
    purgeSubject: "Version {version} — {filename}, {size}",
    purgeBody:
      "The file is deleted from storage and cannot be recovered: no undo, and no backup this console can reach. The filename, size, digest, and who did it survive in the version history and the audit log.",
    purgeConfirm: "Destroy the bytes",
    typeText: "Text",
    versions: {
      failed:
        "The version history could not be loaded, so this is not a list of this document's files. {reason}",
      none: "No files uploaded yet.",
      version: "Version",
      file: "File",
      size: "Size",
      type: "Type",
      uploaded: "Uploaded",
      current: "current",
      shown: "shown",
      purged: "purged {date}",
      view: "View",
      purge: "Purge",
    },
    linkEditor: {
      title: "Links",
      none: "Not linked to anything yet. A document with no links is the organisation-level document, which is what the unfiltered list shows.",
      unlink: "Unlink",
      unlinkFailed: "Nothing was unlinked. {reason}",
      linkTo: "Link to",
      targetId: "Target id",
      link: "Link",
      linkFailed: "Nothing was linked.",
      note: "A credential link names the credential and never its secret. A target in another organisation is indistinguishable from one that does not exist; both are refused the same way.",
    },
    dialog: {
      uploadTitle: "Upload document",
      title: "Title",
      category: "Category",
      description: "Description",
      file: "File",
      accepted:
        "Accepted: PDF; PNG, JPEG, GIF and WebP images; ZIP and Office files; and UTF-8 text of any kind, including Markdown, CSV, HTML, SVG and XML, which are stored and shown as source text, never rendered. The type is read from the file's own bytes, so the extension does not decide it. Anything else: zip it. Files are not scanned for malware.",
      createdWithoutFile:
        "The document was created and the file was not. It is in the library with no file; open it and upload one.",
      cancel: "Cancel",
      upload: "Upload",
      uploadRefused: "The upload was refused.",
      newVersionTitle: "Upload new version",
      newVersionNote:
        "The previous file is never touched: it stays in the history and stays downloadable. To restore an older version, download it and upload it again.",
      editTitle: "Edit details",
      editNote:
        "This changes the document's title, category and description. It does not touch any uploaded file.",
      save: "Save",
      saveFailed: "Nothing was saved.",
    },
  },
  viewer: {
    download: "Download",
    tryAgain: "Try again",
    notDownloaded: "{filename} was not downloaded. {reason}",
    auditNote:
      "Opening a document is recorded in the audit log as a download, the same as pressing Download.",
    destroyed:
      "<b>These bytes were destroyed.</b> Version {version} ({filename}, {size}) was purged and cannot be recovered from this platform. The record of the destruction — who did it and when — is kept in the audit log.",
    noPreview:
      "This console does not preview {type} files. Download {filename} to open it in an application that can.",
    loading: "Loading {filename}…",
    readFailed: "{filename} could not be read. {reason}",
    embedHint: "If the preview does not load, download the file; some browsers do not embed PDFs.",
    noPreviewShort: "This console does not preview {type} files. Download {filename} to open it.",
  },
  markdown: {
    linkRemoved: "Link removed — unsupported URL scheme.",
  },
} as const;
