// Share a pipeline as a link: the spec is compressed into the URL hash, never uploaded.
import type { Spec } from "./model";

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

export async function shareLink(spec: Spec): Promise<string> {
  const packed = await pipe(new TextEncoder().encode(JSON.stringify(spec)), new CompressionStream("deflate-raw"));
  const url = new URL(window.location.href);
  url.hash = `p=${toBase64Url(packed)}`;
  return url.toString();
}

export async function specFromHash(hash: string): Promise<Spec | null> {
  const match = hash.match(/[#&]p=([A-Za-z0-9_-]+)/);
  if (!match) return null;
  try {
    const raw = await pipe(fromBase64Url(match[1]), new DecompressionStream("deflate-raw"));
    return JSON.parse(new TextDecoder().decode(raw)) as Spec;
  } catch {
    return null;
  }
}
