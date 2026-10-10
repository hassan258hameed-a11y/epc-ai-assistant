/**
 * Put an arbitrary slice of the chat page's script into a minimal Tailwind page
 * and report whether the build still succeeds. Used to locate the token that
 * the class extractor cannot handle.
 *
 *   node _dev/tw/chunk.js <page.html> <fromLine> <toLine>
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..', '..');
const tw = fs.readFileSync(path.join(root, 'static/vendor/tailwind.js'), 'utf8');
const source = fs.readFileSync(process.argv[2], 'utf8');

const scripts = [...source.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)];
const main = scripts.reduce((a, b) => (b[1].length > a[1].length ? b : a));
const lines = main[1].split('\n');

const from = Number(process.argv[3] || 0);
const to = Number(process.argv[4] || lines.length);
const slice = lines.slice(from, to).join('\n');

const CONFIG = `tailwind.config = { darkMode:'class', theme:{ extend:{
  opacity: Object.fromEntries(Array.from({length:101},(_,i)=>[String(i),String(i/100)])),
  colors:{ brand:{500:'#6E51F0',600:'#5A38DC'}, accent:{500:'#12B3AA'},
           ink:{50:'#F7F8FC',100:'#EFF1F8',300:'#C9D0E3',400:'#98A2C0',500:'#6E789C',
                700:'#3F4765',800:'#272E48',900:'#171D33',950:'#0B1020'} } }}};`;

const html = `<!doctype html><html><head>
<style type="text/tailwindcss">@layer components { .probe { @apply px-4 py-2; } }</style>
</head><body><div class="probe flex p-4"></div>
<script>/*SLICE*/</` + `script></body></html>`;

const page = html.replace('/*SLICE*/', slice);

const dom = new JSDOM(page, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://x/' });
const w = dom.window;
const seen = [];
const note = (t) => { const s = String(t).split('\n')[0].slice(0, 200); if (!/production/.test(s)) seen.push(s); };
process.on('unhandledRejection', (r) => note((r && r.reason) || r));
process.on('uncaughtException', (e) => note(e && e.message));
w.console.warn = (...a) => note(a[0]);
w.console.error = (...a) => note(a[0]);

w.eval(tw);
w.eval(CONFIG);

setTimeout(() => {
  const css = [...w.document.querySelectorAll('style')]
    .filter((s) => !s.getAttribute('type')).map((s) => s.textContent).join('');
  const ok = /\.flex\{/.test(css);
  console.log(`lines ${from}..${to} (${slice.length} chars): ${ok ? 'OK' : 'BREAK'}  ${css.length}b`);
  seen.slice(0, 3).forEach((s) => console.log('   ' + s));
  process.exit(ok ? 0 : 1);
}, 4000);
