const fs=require('fs'), path=require('path'), {JSDOM}=require('jsdom');
const tw=fs.readFileSync(path.resolve(__dirname,'..','..','static/vendor/tailwind.js'),'utf8');
const n = Number(process.argv[2] || 1);

let blocks = '';
for (let i = 0; i < n; i++) {
  blocks += `<style type="text/tailwindcss">@layer components { .probe${i} { @apply px-4 py-2 font-bold; } }</style>\n`;
}
const uses = Array.from({length:n},(_,i)=>`<div class="probe${i}"></div>`).join('');
const html = `<!doctype html><html><head>${blocks}</head><body><div class="flex p-4">${uses}</div></body></html>`;

const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://x/'});
const w=dom.window;
w.console.warn=()=>{}; w.console.error=()=>{};
w.eval(tw);
w.eval("tailwind.config = { darkMode: 'class', theme: { extend: {} } };");
setTimeout(()=>{
  const css=[...w.document.querySelectorAll('style')].filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('');
  const found = Array.from({length:n},(_,i)=>new RegExp(`\.probe${i}\{`).test(css));
  console.log(`${n} block(s): ${String(css.length).padStart(6)}b  utilities:${/\.flex\{/.test(css)?'Y':'n'}  components found: ${found.map((f,i)=>f?i:'x').join(',')}`);
  process.exit(0);
},3000);
