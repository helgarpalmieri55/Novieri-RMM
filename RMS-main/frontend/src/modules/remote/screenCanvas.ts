import type { Op } from "./screenModel";

/** Decodes a JPEG tile. `createImageBitmap` decodes off the main thread. */
export type DecodeJpeg = (data: Uint8Array) => Promise<ImageBitmap>;

const decodeJpeg: DecodeJpeg = (data) => createImageBitmap(new Blob([new Uint8Array(data)], { type: "image/jpeg" }));

/**
 * Draws screen updates on a canvas, one whole frame at a time and in arrival
 * order: a frame's JPEG tiles are decoded together, then every operation is
 * drawn, so COPYRECT always reads the picture the previous frame left.
 */
export class CanvasRenderer {
  private chain: Promise<void> = Promise.resolve();

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly decode: DecodeJpeg = decodeJpeg,
  ) {}

  resize(width: number, height: number): void {
    if (this.canvas.width !== width) this.canvas.width = width;
    if (this.canvas.height !== height) this.canvas.height = height;
  }

  /** Queues a frame; resolves once it is on the canvas (or failed to decode). */
  apply(ops: Op[]): Promise<void> {
    this.chain = this.chain.then(() => this.draw(ops)).catch(() => undefined);
    return this.chain;
  }

  private async draw(ops: Op[]): Promise<void> {
    const ctx = this.canvas.getContext("2d");
    if (!ctx) return;
    const images = await Promise.all(ops.map((op) => (op.kind === "jpeg" ? this.decode(op.data) : null)));
    ops.forEach((op, i) => {
      switch (op.kind) {
        case "fill":
          ctx.fillStyle = `rgb(${op.rgb[0]}, ${op.rgb[1]}, ${op.rgb[2]})`;
          ctx.fillRect(op.rect.x, op.rect.y, op.rect.w, op.rect.h);
          break;
        case "jpeg": {
          const image = images[i];
          if (image) {
            ctx.drawImage(image, op.rect.x, op.rect.y);
            image.close();
          }
          break;
        }
        case "copy":
          // Drawing a canvas onto itself copies the source region first.
          ctx.drawImage(this.canvas, op.srcX, op.srcY, op.dst.w, op.dst.h, op.dst.x, op.dst.y, op.dst.w, op.dst.h);
          break;
      }
    });
  }
}
