export const time = {
  justNow: "just now",
  ago: {
    minute: { one: "{count} minute ago", other: "{count} minutes ago" },
    hour: { one: "{count} hour ago", other: "{count} hours ago" },
    day: { one: "{count} day ago", other: "{count} days ago" },
  },
  in: {
    minute: { one: "in {count} minute", other: "in {count} minutes" },
    hour: { one: "in {count} hour", other: "in {count} hours" },
    day: { one: "in {count} day", other: "in {count} days" },
  },
  units: {
    second: { one: "{count} second", other: "{count} seconds" },
    minute: { one: "{count} minute", other: "{count} minutes" },
    hour: { one: "{count} hour", other: "{count} hours" },
    day: { one: "{count} day", other: "{count} days" },
  },
} as const;
