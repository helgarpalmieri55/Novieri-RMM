/**
 * AN API DOUBLE THAT RECORDS WHICH CLIENT EACH READ WAS NARROWED TO (decision
 * D6): every `forClient(id)` is noted and answers with this same double, so a
 * page test can assert that a list behind the Client chip read through
 * `forClient(<the chip's client>)`. Anything else is the wrapped API's own.
 */
export function recordingNarrowing<T extends object>(api: T): { api: T; narrowedTo: string[] } {
  const narrowedTo: string[] = [];
  const wrapped: T = new Proxy(api, {
    get(target, prop, receiver) {
      if (prop === "forClient") {
        return (clientId: string) => {
          narrowedTo.push(clientId);
          return wrapped;
        };
      }
      return Reflect.get(target, prop, receiver);
    },
  });
  return { api: wrapped, narrowedTo };
}
