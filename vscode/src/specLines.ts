// Pure helpers, kept apart from the vscode API so they can be tested with node --test.

/** Does this YAML look like a Redcliff pipeline spec (a version and a list of nodes)? */
export function looksLikeSpec(text: string): boolean {
  return /^version\s*:\s*\d+\s*$/m.test(text) && /^nodes\s*:/m.test(text);
}

/** The 0-based line declaring step ``id`` (``- id: orders``, in block or flow style), or -1. */
export function lineOfNode(text: string, id: string): number {
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const block = new RegExp(`^\\s*-\\s*id\\s*:\\s*["']?${escaped}["']?\\s*(#.*)?$`);
  const anywhere = new RegExp(`(^|[{,\\s])id\\s*:\\s*["']?${escaped}["']?\\s*([,}]|$)`);
  const lines = text.split(/\r?\n/);
  let found = lines.findIndex((l) => block.test(l));
  if (found < 0) found = lines.findIndex((l) => anywhere.test(l));
  return found;
}

/** The lines a step spans: from its ``- id:`` line to the line before the next list item at its indent. */
export function rangeOfNode(text: string, id: string): [number, number] {
  const lines = text.split(/\r?\n/);
  const start = lineOfNode(text, id);
  if (start < 0) return [-1, -1];
  const indent = (lines[start].match(/^\s*/) ?? [""])[0].length;
  let end = start;
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    const ind = (line.match(/^\s*/) ?? [""])[0].length;
    if (ind < indent || (ind === indent && /^\s*-\s/.test(line))) break;
    end = i;
  }
  return [start, end];
}

/** A command setting (``redcliff``, ``python3 -m redcliff.cli``, a quoted path) as program and arguments. */
export function splitCommand(command: string): [string, string[]] {
  const parts: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(command.trim())) !== null) parts.push(m[1] ?? m[2] ?? m[3]);
  if (!parts.length) return ["redcliff", []];
  return [parts[0], parts.slice(1)];
}

export type Issue = { node: string | null; message: string };

/** Where to show each issue: the step's line, or the top of the file for pipeline-wide ones. */
export function placeIssues(text: string, issues: Issue[]): { line: number; message: string }[] {
  return issues.map((i) => {
    const line = i.node ? lineOfNode(text, i.node) : -1;
    return { line: Math.max(line, 0), message: i.node && line < 0 ? `${i.node}: ${i.message}` : i.message };
  });
}
