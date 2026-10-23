import { describeSubmitError } from "../../components/form";
import { useT } from "../../i18n/context";

/**
 * The one way this module announces a failed request: a `role="alert"`
 * sentence in the form kernel's error class, never an empty string.
 *
 * `ApiError` carries the response body as its message, and a 503 from a proxy
 * has no body, so every `{error && <p>{error.message}</p>}` rendered blank on
 * the failure that most needs a sentence. `describeSubmitError` is the single
 * translation; this component is where the module goes through it.
 */
export function ErrorLine({ error }: { error: unknown }) {
  useT();
  if (!error) return null;
  return (
    <p role="alert" className="form-error">
      {describeSubmitError(error)}
    </p>
  );
}
