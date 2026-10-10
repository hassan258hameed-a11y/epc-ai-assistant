/**
 * Bisect a page's own @layer block: feed the Tailwind runtime progressively
 * larger prefixes of its rules and report the first rule that breaks the build.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..', '..');
const tw = fs.readFileSync(path.join(root, 'static/vendor/tailwind.js'), 'utf8');
const pagePath = process.argv[2];
const source = fs.readFileSync(pagePath, 'utf8');

// The chat page's own component block is the second tailwindcss style tag.
const blocks = [...source.matchAll(/<style type="text\/tailwindcss">([\s\S]*?)<\/style>/g)];
const target = blocks[Number(process.argv[3] || 1)];
const body = target[1];

// Split the @layer body into individual rules (a rule ends at a line whose
// trailing brace closes it — good enough for this hand-written CSS).
const inner = body.replace(/^\s*@layer\s+\w+\s*\{/, '').replace(/\}\s*$/, '');
const rules = inner.split(/\n(?=\s*[.:#a-zA-Z*][^\n]*\{)/).filter((r) => r.trim());

function build(count) {
  const kept = rules.slice(0, count).join('\n');
  const rebuilt = `<style type="text/tailwindcss">\n@layer components {\n${kept}\n}\n</style>`;
  return source.replace(target[0], rebuilt);
}

function compile(html) {
  return new Promise((resolve) => {
    const dom = new JSDOM(html.replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, ''),
      { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://x/' });
    const w = dom.window;
    let err = null;
    const grab = (e) => { if (!err) err = String((e && e.reason) || e).split('\n')[0].slice(0, 200); };
    process.once('unhandledRejection', grab);
    w.console.warn = () => {};
    w.console.error = (...a) => grab(a[0]);
    try { w.eval(tw); } catch (e) { grab(e); }
    w.document.querySelectorAll('script:not([src])').forEach((t) => { try { w.eval(t.textContent); } catch (_) {} });
    setTimeout(() => {
      const css = [...w.document.querySelectorAll('style')]
        .filter((s) => !s.getAttribute('type')).map((s) => s.textContent).join('');
      resolve({ ok: /\.flex\{/.test(css) || /\.btn-primary\{/.test(css), bytes: css.length, err });
    }, 2500);
  });
}

(async () => {
  console.log(`${rules.length} rules in block #${process.argv[3] || 1} of ${path.basename(pagePath)}\n`);
  for (let i = 0; i <= rules.length; i++) {
    const r = await compile(build(i));
    const label = i === 0 ? '(empty block)' : rules[i - 1].trim().split('{')[0].trim();
    console.log(`${r.ok ? 'OK  ' : 'BREAK'} ${String(i).padStart(2)} rules  ${String(r.bytes).padStart(6)}b  ${label.slice(0, 60)}`);
    if (!r.ok) {
      console.log('\nFirst failing rule:\n' + rules[i - 1]);
      if (r.err) console.log('\nreported: ' + r.err);
      process.exit(0);
    }
  }
  console.log('\nNo failure found in this block.');
})();
