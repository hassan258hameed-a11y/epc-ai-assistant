const fs=require('fs'), path=require('path'), {JSDOM}=require('jsdom');
const tw=fs.readFileSync(path.resolve(__dirname,'..','..','static/vendor/tailwind.js'),'utf8');
const rule = process.argv[2];
const useConfig = process.argv[3] !== 'noconfig';
const configScript = useConfig ? `
  tailwind.config = { darkMode:'class', theme:{ extend:{
    opacity: Object.fromEntries(Array.from({length:101},(_,i)=>[String(i),String(i/100)])),
    colors:{ brand:{500:'#6E51F0',600:'#5A38DC'}, accent:{500:'#12B3AA'}, ink:{50:'#F7F8FC',900:'#171D33',950:'#0B1020'} }
  }}};` : '';
const html = `<!doctype html><html><head>
<style type="text/tailwindcss">@layer components { .probe { @apply ${rule}; } }</style>
</head><body><div class="probe flex p-4"></div></body></html>`;
const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true,url:'http://x/'});
const w=dom.window; let err=null;
process.on('unhandledRejection', r => { if(!err) err = String((r&&r.reason)||r).split('\n')[0].slice(0,200); });
w.console.warn=()=>{}; w.console.error=(...a)=>{ if(!err) err = String(a[0]).split('\n')[0].slice(0,200); };
w.eval(tw);
if (configScript) w.eval(configScript);
setTimeout(()=>{
  const css=[...w.document.querySelectorAll('style')].filter(s=>!s.getAttribute('type')).map(s=>s.textContent).join('');
  const hasFlex = /\.flex\{/.test(css);
  const hasProbe = /\.probe\{/.test(css);
  console.log(`${hasFlex&&hasProbe?'OK   ':'BREAK'}  ${String(css.length).padStart(6)}b  flex:${hasFlex?'Y':'n'} probe:${hasProbe?'Y':'n'}  ${rule.slice(0,60)}` + (err?`  <- ${err}`:''));
  process.exit(0);
},3000);
