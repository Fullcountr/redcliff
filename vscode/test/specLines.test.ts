import assert from "node:assert/strict";
import { test } from "node:test";
import { lineOfNode, looksLikeSpec, placeIssues, rangeOfNode, splitCommand } from "../src/specLines.ts";

const SPEC = `version: 0
name: orders_revenue
nodes:
  - id: orders
    type: source
    params:
      format: csv
      path: ../data/orders.csv
  - id: "shipped"   # only shipped orders
    type: filter
    inputs: [orders]
    params:
      condition: status = 'shipped'
  - {id: out, type: target, inputs: [shipped], params: {format: csv, path: out/x.csv}}
`;

test("recognises specs", () => {
  assert.equal(looksLikeSpec(SPEC), true);
  assert.equal(looksLikeSpec("name: x\nitems: []\n"), false);
  assert.equal(looksLikeSpec("on: push\njobs:\n  nodes: 1\n"), false);
});

test("finds a step's lines", () => {
  assert.equal(lineOfNode(SPEC, "orders"), 3);
  assert.equal(lineOfNode(SPEC, "shipped"), 8);
  assert.equal(lineOfNode(SPEC, "out"), 13);
  assert.equal(lineOfNode(SPEC, "missing"), -1);
  assert.deepEqual(rangeOfNode(SPEC, "orders"), [3, 7]);
  assert.deepEqual(rangeOfNode(SPEC, "shipped"), [8, 12]);
  assert.deepEqual(rangeOfNode(SPEC, "out"), [13, 13]);
});

test("places issues on their steps", () => {
  assert.deepEqual(placeIssues(SPEC, [
    { node: "shipped", message: "unknown column 'statu'" },
    { node: null, message: "two targets write out/x.csv" },
    { node: "ghost", message: "no such step" },
  ]), [
    { line: 8, message: "unknown column 'statu'" },
    { line: 0, message: "two targets write out/x.csv" },
    { line: 0, message: "ghost: no such step" },
  ]);
});

test("splits the command setting", () => {
  assert.deepEqual(splitCommand("redcliff"), ["redcliff", []]);
  assert.deepEqual(splitCommand("python3 -m redcliff.cli"), ["python3", ["-m", "redcliff.cli"]]);
  assert.deepEqual(splitCommand('"/Users/me/My Envs/bin/redcliff"'), ["/Users/me/My Envs/bin/redcliff", []]);
  assert.deepEqual(splitCommand("   "), ["redcliff", []]);
});
