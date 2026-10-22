export const common = {
  actions: { save: "Save", cancel: "Cancel", close: "Close", retry: "Retry" },
  count: {
    selected: { one: "{count} selected", other: "{count} selected" },
    items: { one: "{count} item", other: "{count} items" },
  },
  help: { docsLink: "See <link>the guide</link> for details.", twoLinks: "<a>One</a><b>Two</b>" },
  language: { label: "Language", english: "English", spanish: "Español" },
} as const;
