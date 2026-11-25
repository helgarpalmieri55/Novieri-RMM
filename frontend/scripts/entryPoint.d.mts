/** Types for `entryPoint.mjs`, so a vitest guard can import it under `strict`. */
/** `argv1` is `process.argv[1]`; `moduleUrl` the caller's `import.meta.url`. */
export declare function isEntryPoint(argv1: string | undefined, moduleUrl: string): boolean;
