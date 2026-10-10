/**
 * Binary-search the chat page's inline JavaScript for the text that Tailwind's
 * class extractor turns into an invalid candidate and chokes on.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..', '..');
const tw = fs.readFileSync(path.join(root, 'static/vendor/tailwind.js'), 'utf8');
const source = fs.readFileSync(process.argv[2], 'utf8');

// The page's main script is the last inline <script> without src.
const scripts = [...source.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)];
const main = scripts.reduce((a, b) => (b[1].length > a[1].length ? b : a));
const lines = main[1].split('\n');

function withLines(from, to) {
  const kept = lines.slice(from, to).join('\n');
  return source.replace(main[0], '<script>\n' + kept + '\n</script>');
}

function healthy(html) {
  return new Promise((resolve) => {
    const dom = new JSDOM(html.replace(/<script src="\/static\/vendor\/tailwind\.js"><\/script>/, ''),
      { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://x/' });
    const w = dom.window;
    w.console.warn = () => {}; w.console.error = () => {};
    const swallow = () => {};
    process.on('unhandledRejection', swallow);
    try { w.eval(tw); } catch (_) {}
    w.document.querySelectorAll('script:not([src])').forEach((t) => {
      if (/tailwind\.config|epcai-theme/.test(t.textContent)) { try { w.eval(t.textContent); } catch (_) {} }
    });
    setTimeout(() => {
      process.removeListener('unhandledRejection', swallow);
      const css = [...w.document.querySelectorAll('style')]
        .filter((s) => !s.getAttribute('type')).map((s) => s.textContent).join('');
      resolve(/\.flex\{/.test(css));
    }, Number(process.env.TW_WAIT || 2200));
  });
}

(async () => {
  console.log(`main script: ${lines.length} lines`);
  console.log('empty script healthy? ', await healthy(withLines(0, 0)));
  console.log('full script healthy?  ', await healthy(withLines(0, lines.length)));

  // Narrow to the first prefix that breaks.
  let lo = 0, hi = lines.length;
  while (lo < hi) {
    const mid = Math.floor((lo + hi) / 2);
    const ok = await healthy(withLines(0, mid));
    process.stdout.write(`  lines 0..${mid}: ${ok ? 'ok' : 'BREAK'}\n`);
    if (ok) lo = mid + 1; else hi = mid;
  }

  console.log(`\nFirst breaking line index: ${lo - 1}`);
  for (let i = Math.max(0, lo - 3); i < Math.min(lines.length, lo + 2); i++) {
    console.log(`${String(i).padStart(4)}${i === lo - 1 ? ' >>' : '   '} ${lines[i]}`);
  }
})();
