const fs = require('fs'); const path = require('path'); const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..', '..');
const tw = fs.readFileSync(path.join(root, 'static/vendor/tailwind.js'), 'utf8');
const page = process.argv[2];
process.on('unhandledRejection', (r) => {
  const msg = (r && r.reason) || (r && r.message) || String(r);
  console.log('UNHANDLED:', String(msg).split('\n')[0].slice(0, 300));
});
const html = fs.readFileSync(page, 'utf8').replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, '');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://x/' });
const w = dom.window;
w.console.warn = (...a) => { const s=String(a[0]); if(!/production/.test(s)) console.log('WARN:', s.split('\n')[0].slice(0,300)); };
w.console.error = (...a) => console.log('ERR:', String(a[0]).split('\n')[0].slice(0,300));
try { w.eval(tw); } catch (e) { console.log('LOAD ERR:', e.message.split('\n')[0]); }
w.document.querySelectorAll('script:not([src])').forEach(t => { try { w.eval(t.textContent); } catch(_){} });
setTimeout(() => {
  const styles = [...w.document.querySelectorAll('style')];
  console.log('style tags:', styles.length, '| tailwindcss blocks:',
              styles.filter(s => s.getAttribute('type') === 'text/tailwindcss').length);
  const css = styles.filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('\n');
  console.log('CSS bytes:', css.length);
}, 4000);
