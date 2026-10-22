export const validation = {
  required: "{label} is required",
  saved: "Saved.",
  // What a submit error says when the server gave nothing better.
  unexplained: "The server refused this and gave no reason. Please try again.",
  unauthorized: "unauthorized",
  serviceUnavailable: "That service is unavailable right now. Nothing was changed — try again shortly.",
  forbidden: "You do not have permission to do that.",
  notFound: "That no longer exists. It may have been deleted.",
  conflict: "That conflicts with something that already exists.",
  serverFailed: "The server failed on this request. Nothing was changed — try again shortly.",
  // Pydantic 422 errors by `type` (components/form.tsx `describeValidation`).
  // While the console is in English it shows the server's own message, which is
  // already English and which existing screens and tests pin; these sentences
  // are what every other language shows in its place.
  type: {
    missing: "This field is required.",
    stringTooShort: {
      one: "Too short (at least {min_length} character).",
      other: "Too short (at least {min_length} characters).",
    },
    stringTooLong: {
      one: "Too long (at most {max_length} character).",
      other: "Too long (at most {max_length} characters).",
    },
    notANumber: "Must be a number.",
    atLeast: "Must be at least {ge}.",
    atMost: "Must be at most {le}.",
    notAllowed: "Not one of the allowed values.",
  },
} as const;
