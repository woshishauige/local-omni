const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const acorn = require('internal/deps/acorn/acorn/dist/acorn');

const input = path.join(__dirname, '../native/_app/immutable/bundle.H5ytpYYP.js');
const source = fs.readFileSync(input, 'utf8');
const sourceHash = crypto.createHash('sha256').update(source).digest('hex');
if (sourceHash !== '67b3b101231de9d144bbb5d15683b5103ca6fdd8baaeb0cb167b03ba09414f37') {
  throw new Error('Native UI changed. Review the translation catalog before rebuilding.');
}
const ast = acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'module' });
const ranges = [[416000, 500000], [1448300, 1448600], [1453800, 1463000], [1920000, 2750000], [4100000, 4400000]];
const nodes = [];
function walk(node, parent) {
  if (!node || typeof node !== 'object') return;
  if (ranges.some(([start, end]) => node.start >= start && node.end <= end)) {
    if (node.type === 'Literal' && typeof node.value === 'string') {
      nodes.push({ id: node.start, type: 'literal', value: node.value,
        context: source.slice(Math.max(0, node.start - 65), node.end + 65) });
    } else if (node.type === 'TemplateLiteral') {
      nodes.push({ id: node.start, type: 'template', value: node.quasis.map((q, i) => q.value.cooked + (i < node.expressions.length ? `{${i}}` : '')).join(''),
        context: source.slice(Math.max(0, node.start - 65), node.end + 65) });
    }
  }
  for (const value of Object.values(node)) {
    if (Array.isArray(value)) value.forEach(child => walk(child, node));
    else if (value && typeof value === 'object') walk(value, node);
  }
}
walk(ast);
if (process.argv[2] !== 'build') {
  process.stdout.write(JSON.stringify({ sha256: crypto.createHash('sha256').update(source).digest('hex'), nodes }));
} else {
  const edits = JSON.parse(fs.readFileSync(path.join(__dirname, 'edits.json'), 'utf8'));
  const editsById = new Map(edits.map(edit => [edit.id, edit]));
  const byId = new Map(nodes.map(node => [node.id, node]));
  const replacements = [];
  const transformed = new Set();
  function collect(node) {
    if (!node || typeof node !== 'object') return;
    const edit = ['Literal', 'TemplateLiteral'].includes(node.type) ? editsById.get(node.start) : undefined;
    if (edit) {
      const original = byId.get(node.start);
      if (!original || original.value !== edit.source) throw new Error(`Source mismatch at ${node.start}`);
      transformed.add(node.start);
      if (node.type === 'Literal') {
        replacements.push({ start: node.start, end: node.end, text: JSON.stringify(edit.target) });
      } else if (node.type === 'TemplateLiteral') {
        const parts = edit.target.split(/\{(\d+)\}/);
        const indexes = parts.filter((_, i) => i % 2 === 1).map(Number);
        if (indexes.length !== node.expressions.length || indexes.some((v, i) => v !== i)) throw new Error(`Template expression order changed at ${node.start}`);
        node.quasis.forEach((quasi, i) => replacements.push({ start: quasi.start, end: quasi.end,
          text: parts[i * 2].replace(/\\/g, '\\\\').replace(/`/g, '\\`').replace(/\$\{/g, '\\${') }));
      } else throw new Error(`Invalid translation node at ${node.start}`);
    }
    const authPatterns = { 'access denied': 'access denied|访问被拒绝',
      'invalid api key': 'invalid api key|API\\s*密钥.{0,3}(?:无效|不正确)', 'unauthorized': 'unauthorized|未授权' };
    if (node.type === 'CallExpression' && node.start > 4370000 && node.end < 4390000 &&
        node.callee.type === 'MemberExpression' && node.callee.property.name === 'includes' &&
        authPatterns[node.arguments[0]?.value]) {
      replacements.push({ start: node.start, end: node.end,
        text: `/${authPatterns[node.arguments[0].value]}/i.test(${source.slice(node.callee.object.start, node.callee.object.end)})` });
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(collect);
      else if (value && typeof value === 'object') collect(value);
    }
  }
  collect(ast);
  if (transformed.size !== edits.length) throw new Error('Some translation edits did not match');
  let output = source;
  let previousStart = source.length;
  for (const replacement of replacements.sort((a, b) => b.start - a.start)) {
    if (replacement.end > previousStart) throw new Error('Overlapping translations');
    output = output.slice(0, replacement.start) + replacement.text + output.slice(replacement.end);
    previousStart = replacement.start;
  }
  acorn.parse(output, { ecmaVersion: 'latest', sourceType: 'module' });
  const destination = path.join(__dirname, '../native/_app/immutable/bundle.zh-CN.js');
  fs.writeFileSync(destination, output);
  console.log(JSON.stringify({ translatedNodes: transformed.size, sourceSha256: crypto.createHash('sha256').update(source).digest('hex'), outputBytes: Buffer.byteLength(output), destination }));
}
