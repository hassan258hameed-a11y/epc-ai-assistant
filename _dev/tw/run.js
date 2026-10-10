/**
 * Run the vendored Tailwind Play CDN against a rendered page under jsdom and
 * report whether it produced any CSS — and any error it raised while trying.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const pagePath = process.argv[2];
const root = path.resolve(__dirname, '..', '..');
const tailwindSrc = fs.readFileSync(path.join(root, 'static', 'vendor', 'tailwind.js'), 'utf8');

let html = fs.readFileSync(pagePath, 'utf8');
// Strip the <script src=...tailwind.js> tag; we inject the source directly.
html = html.replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, '');

const dom = new JSDOM(html, {
  runScripts: 'outside-only',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:8000/',
});

const { window } = dom;
const errors = [];
window.console.error = (...a) => errors.push('ERROR: ' + a.join(' '));
window.console.warn = (...a) => errors.push('WARN: ' + a.join(' '));

try {
  window.eval(tailwindSrc);
} catch (e) {
  errors.push('THROWN while loading tailwind: ' + e.message);
}

// Then run every inline script in document order (the config assignment).
const inline = [...window.document.querySelectorAll('script:not([src])')];
inline.forEach((tag, i) => {
  try { window.eval(tag.textContent); }
  catch (e) { errors.push(`THROWN in inline script #${i}: ${e.message}`); }
});

setTimeout(() => {
  const styles = [...window.document.querySelectorAll('style')];
  const generated = styles.filter((s) => !s.getAttribute('type'));
  const total = generated.reduce((n, s) => n + (s.textContent || '').length, 0);

  console.log('--- ' + path.basename(pagePath) + ' ---');
  console.log('inline scripts run   :', inline.length);
  console.log('<style> tags present :', styles.length);
  console.log('generated CSS bytes  :', total);

  const css = generated.map((s) => s.textContent).join('\n');
  for (const probe of ['.btn-primary', '.card', '.input', '.badge-brand', '.mesh', 'bg-brand-600', 'text-ink-900']) {
    console.log('  contains ' + probe.padEnd(14) + ':', css.includes(probe));
  }

  if (errors.length) {
    console.log('\nMESSAGES:');
    errors.slice(0, 12).forEach((e) => console.log('  ' + e.slice(0, 500)));
  } else {
    console.log('\nno errors reported');
  }
}, 2500);
