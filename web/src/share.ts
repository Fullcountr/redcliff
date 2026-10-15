// Share a pipeline or a data model as a link: it is compressed into the URL hash, never uploaded.
import type { DataModel, Spec } from "./model";

function toBase64Url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(text: string): Uint8Array {
  const s = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function pipe(bytes: Uint8Array, stream: CompressionStream | DecompressionStream): Promise<Uint8Array> {
  const out = await new Response(new Blob([bytes as BlobPart]).stream().pipeThrough(stream)).arrayBuffer();
  return new Uint8Array(out);
}

async function pack(value: unknown, key: string): Promise<string> {
  const packed = await pipe(new TextEncoder().encode(JSON.stringify(value)), new CompressionStream("deflate-raw"));
  const url = new URL(window.location.href);
  url.hash = `${key}=${toBase64Url(packed)}`;
  return url.toString();
}

async function unpack<T>(hash: string, key: string): Promise<T | null> {
  const match = hash.match(new RegExp(`[#&]${key}=([A-Za-z0-9_-]+)`));
  if (!match) return null;
  try {
    const raw = await pipe(fromBase64Url(match[1]), new DecompressionStream("deflate-raw"));
    return JSON.parse(new TextDecoder().decode(raw)) as T;
  } catch {
    return null;
  }
}

export const shareLink = (spec: Spec) => pack(spec, "p");
export const specFromHash = (hash: string) => unpack<Spec>(hash, "p");
/** A data model's link opens the playground in Model mode. The data isn't in it. */
export const shareModelLink = (model: DataModel) => pack(model, "m");
export const modelFromHash = (hash: string) => unpack<DataModel>(hash, "m");
