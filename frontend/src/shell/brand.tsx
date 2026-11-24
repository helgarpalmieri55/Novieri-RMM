/**
 * THE BRAND (DESIGN.md v4, "Brand"), drawn from the operator's own SVGs in
 * `docs/design/marca/` (direction A, "Cordillera", 2026-11-23).
 *
 * INLINE, NOT `<img>`: the wordmark takes its colour from the ground it sits
 * on (`currentColor`: white on the rail, ink on a light frame), and an inline
 * SVG is measured by the harmony gate and the a11y pass like any other paint.
 * The paths are the files' own, and `brand.test.tsx` compares them.
 */
import "./brand.css";

/** The product's name (decision A3): in the tab title, the sign-in frame, the portal. */
export const PRODUCT_NAME = "Novieri RMM";
/** What the console's own bar says (decision A3). */
export const BRAND_NAME = "Novieri";

const SYMBOL_PATH = "M4.73 25L8.37 25L14.67 7L11.03 7ZM11.03 7L14.67 7L20.97 25L17.33 25ZM17.33 25L20.97 25L27.27 7L23.63 7Z";

/** The wordmark's seven letters, each at its offset in the 112.9-wide view box. */
const WORDMARK: readonly { x: number; d: string; evenOdd?: boolean }[] = [
  { x: 0, d: "M0 0h2.7v20h-2.7ZM13.7 0h2.7v20h-2.7ZM0 0L3.233 0L16.4 20L13.167 20Z" },
  {
    x: 20.4,
    d: "M0 10a10.35 10.35 0 1 0 20.7 0a10.35 10.35 0 1 0 -20.7 0ZM2.889 10a7.461 7.461 0 1 1 14.922 0a7.461 7.461 0 1 1 -14.922 0Z",
    evenOdd: true,
  },
  { x: 44.5, d: "M0 0L2.818 0L10.209 20L7.391 20ZM14.782 0L17.6 0L10.209 20L7.391 20Z" },
  { x: 66.6, d: "M0 0h2.7v20h-2.7Z" },
  { x: 74.2, d: "M0 0h2.7v20h-2.7ZM0 0h12.4v2.7h-12.4ZM0 8.65h11.1v2.7h-11.1ZM0 17.3h12.4v2.7h-12.4Z" },
  { x: 91, d: "M0 0h2.7v20h-2.7ZM0 0H7.5A5.6 5.6 0 0 1 7.5 11.2H0V8.5H7.5A2.9 2.9 0 0 0 7.5 2.7H0ZM5.1 8.5L8.122 8.5L13.9 20L10.878 20Z" },
  { x: 110, d: "M0 0h2.7v20h-2.7Z" },
];

const R_PATH = WORDMARK[5]?.d ?? "";
const M_PATH = "M0 0h2.7v20h-2.7ZM16.9 0h2.7v20h-2.7ZM0 0L2.952 0L11.276 15.5L8.324 15.5ZM16.648 0L19.6 0L11.276 15.5L8.324 15.5Z";

function Letters() {
  return (
    <>
      {WORDMARK.map((letter) => (
        <path
          key={letter.x}
          className="nv-letter"
          transform={`translate(${letter.x} 0)`}
          d={letter.d}
          fillRule={letter.evenOdd ? "evenodd" : undefined}
        />
      ))}
    </>
  );
}

/**
 * The zigzag N. `tile`: on its cobalt tile with a white stroke, as the favicon
 * and the rail draw it. Decorative unless `label` is given, because it always
 * sits beside the wordmark that already names the product.
 */
export function NovieriSymbol({ size = 28, tile = false, label }: { size?: number; tile?: boolean; label?: string }) {
  return (
    <svg
      className={tile ? "nv-symbol nv-on-tile" : "nv-symbol"}
      viewBox="0 0 32 32"
      width={size}
      height={size}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      {tile ? <rect className="nv-tile" width="32" height="32" rx="7" /> : null}
      <path className="nv-stroke" d={SYMBOL_PATH} />
    </svg>
  );
}

/** "NOVIERI", in the colour of the text around it. */
export function NovieriWordmark({ height = 16, label = BRAND_NAME, className }: { height?: number; label?: string; className?: string }) {
  return (
    <svg
      className={className ? `nv-wordmark ${className}` : "nv-wordmark"}
      viewBox="-0.1 -0.6 112.9 21.2"
      width={(height * 112.9) / 21.2}
      height={height}
      role="img"
      aria-label={label}
      focusable="false"
    >
      <Letters />
    </svg>
  );
}

/** "NOVIERI | RMM": the sign-in frame, the portal, and wherever the full name is the subject. */
export function NovieriLockup({ height = 22 }: { height?: number }) {
  return (
    <svg
      className="nv-lockup"
      viewBox="-0.1 -0.6 156.18 21.2"
      width={(height * 156.18) / 21.2}
      height={height}
      role="img"
      aria-label={PRODUCT_NAME}
      focusable="false"
    >
      <Letters />
      <rect className="nv-rule" x="117.9" y="0" width="0.9" height="20" />
      <g className="nv-descriptor" transform="translate(124 9.6) scale(0.52)">
        <path d={R_PATH} />
        <path transform="translate(18.1 0)" d={M_PATH} />
        <path transform="translate(41.9 0)" d={M_PATH} />
      </g>
    </svg>
  );
}
