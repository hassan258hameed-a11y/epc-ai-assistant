const fs=require('fs'), path=require('path'), {JSDOM}=require('jsdom');
for (const p of process.argv.slice(2)) {
  const html = fs.readFileSync(p,'utf8');
  const dom = new JSDOM(html);
  const d = dom.window.document;
  const withClass = d.querySelectorAll('[class]').length;
  const all = d.querySelectorAll('*').length;
  console.log(path.basename(p).padEnd(24),
    'file:', String(html.length).padStart(6),
    '| elements:', String(all).padStart(5),
    '| with class:', String(withClass).padStart(5),
    '| body children:', d.body ? d.body.children.length : 'NO BODY');
}
