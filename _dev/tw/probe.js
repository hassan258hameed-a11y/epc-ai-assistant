const fs = require('fs'); const path = require('path'); const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..', '..');
const tw = fs.readFileSync(path.join(root, 'static/vendor/tailwind.js'), 'utf8');
const page = process.argv[2];
const probes = process.argv.slice(3);
const html = fs.readFileSync(page, 'utf8').replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, '');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://x/' });
const w = dom.window;
w.console.warn = () => {}; w.console.error = (...a) => console.log('ERR:', String(a[0]).split('\n')[0].slice(0,200));
try { w.eval(tw); } catch (e) { console.log('load err', e.message); }
w.document.querySelectorAll('script:not([src])').forEach(t => { try { w.eval(t.textContent); } catch(_){} });
setTimeout(() => {
  const css = [...w.document.querySelectorAll('style')].filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('\n');
  console.log('CSS bytes:', css.length);
  probes.forEach(p => console.log('  ' + p.padEnd(22) + ':', css.includes(p)));
}, 3000);
